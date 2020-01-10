# -*- coding: utf-8 -*-

import argparse
import base64
import csv
import io
import json
import os
import pickle
import re
import subprocess
import tempfile
from datetime import datetime
from logging import INFO, basicConfig, getLogger

import hglib
import joblib
import matplotlib
import numpy as np
import shap
from dateutil.relativedelta import relativedelta
from libmozdata import vcs_map
from libmozdata.phabricator import PhabricatorAPI
from scipy.stats import spearmanr

from bugbug import commit_features, db, repository, test_scheduling
from bugbug.utils import (
    download_and_load_model,
    download_check_etag,
    get_secret,
    retry,
    to_array,
    zstd_decompress,
)

basicConfig(level=INFO)
logger = getLogger(__name__)

URL = "https://community-tc.services.mozilla.com/api/index/v1/task/project.relman.bugbug.train_{model_name}.latest/artifacts/public/{file_name}"
PAST_BUGS_BY_FUNCTION_URL = "https://community-tc.services.mozilla.com/api/index/v1/task/project.relman.bugbug.past_bugs_by_function.latest/artifacts/public/past_bugs_by_function.pickle.zst"


# ------------------------------------------------------------------------------
# Copied from https://github.com/mozilla-conduit/lando-api/blob/4b583f9d773dfc8c3e8c39e3d3b7385568d744df/landoapi/commit_message.py

SPECIFIER = r"(?:r|a|sr|rs|ui-r)[=?]"
R_SPECIFIER = r"\br[=?]"
R_SPECIFIER_RE = re.compile(R_SPECIFIER)

LIST = r"[;,\/\\]\s*"

# Note that we only allows a subset of legal IRC-nick characters.
# Specifically, we do not allow [ \ ] ^ ` { | }
IRC_NICK = r"[a-zA-Z0-9\-\_]+"

# fmt: off
REVIEWERS_RE = re.compile(
    r"([\s\(\.\[;,])"                   # before "r" delimiter
    + r"(" + SPECIFIER + r")"           # flag
    + r"("                              # capture all reviewers
        + r"#?"                         # Optional "#" group reviewer prefix
        + IRC_NICK                      # reviewer
        + r"!?"                         # Optional "!" blocking indicator
        + r"(?:"                        # additional reviewers
            + LIST                      # delimiter
            + r"(?![a-z0-9\.\-]+[=?])"  # don"t extend match into next flag
            + r"#?"                     # Optional "#" group reviewer prefix
            + IRC_NICK                  # reviewer
            + r"!?"                     # Optional "!" blocking indicator
        + r")*"
    + r")?"
)
# fmt: on


def replace_reviewers(commit_description, reviewers):
    if not reviewers:
        reviewers_str = ""
    else:
        reviewers_str = "r=" + ",".join(reviewers)

    if commit_description == "":
        return reviewers_str

    commit_description = commit_description.splitlines()
    commit_summary = commit_description.pop(0)
    commit_description = "\n".join(commit_description)

    if not R_SPECIFIER_RE.search(commit_summary):
        commit_summary += " " + reviewers_str
    else:
        # replace the first r? with the reviewer list, and all subsequent
        # occurrences with a marker to mark the blocks we need to remove
        # later
        d = {"first": True}

        def replace_first_reviewer(matchobj):
            if R_SPECIFIER_RE.match(matchobj.group(2)):
                if d["first"]:
                    d["first"] = False
                    return matchobj.group(1) + reviewers_str
                else:
                    return "\0"
            else:
                return matchobj.group(0)

        commit_summary = re.sub(REVIEWERS_RE, replace_first_reviewer, commit_summary)

        # remove marker values as well as leading separators.  this allows us
        # to remove runs of multiple reviewers and retain the trailing
        # separator.
        commit_summary = re.sub(LIST + "\0", "", commit_summary)
        commit_summary = re.sub("\0", "", commit_summary)

    if commit_description == "":
        return commit_summary.strip()
    else:
        return commit_summary.strip() + "\n" + commit_description


# ------------------------------------------------------------------------------


class CommitClassifier(object):
    def __init__(
        self, model_name, gecko_path, git_repo_dir, method_defect_predictor_dir
    ):
        self.model_name = model_name
        self.repo_dir = gecko_path

        self.model = download_and_load_model(model_name)
        assert self.model is not None

        self.git_repo_dir = git_repo_dir
        if git_repo_dir:
            self.clone_git_repo("https://github.com/mozilla/gecko-dev", git_repo_dir)

        self.method_defect_predictor_dir = method_defect_predictor_dir
        if method_defect_predictor_dir:
            self.clone_git_repo(
                "https://github.com/lucapascarella/MethodDefectPredictor",
                method_defect_predictor_dir,
                "fa5269b959d8ddf7e97d1e92523bb64c17f9bbcd",
            )

        if model_name == "regressor":
            self.use_test_history = False

            model_data_X_path = f"{model_name}model_data_X"
            updated = download_check_etag(
                URL.format(model_name=model_name, file_name=f"{model_data_X_path}.zst")
            )
            if updated:
                zstd_decompress(model_data_X_path)
            assert os.path.exists(model_data_X_path), "Decompressed X dataset exists"

            model_data_y_path = f"{model_name}model_data_y"
            updated = download_check_etag(
                URL.format(model_name=model_name, file_name=f"{model_data_y_path}.zst")
            )
            if updated:
                zstd_decompress(model_data_y_path)
            assert os.path.exists(model_data_y_path), "Decompressed y dataset exists"

            self.X = to_array(joblib.load(model_data_X_path))
            self.y = to_array(joblib.load(model_data_y_path))

            past_bugs_by_function_path = "data/past_bugs_by_function.pickle"
            download_check_etag(
                PAST_BUGS_BY_FUNCTION_URL, path=f"{past_bugs_by_function_path}.zst"
            )
            zstd_decompress(past_bugs_by_function_path)
            assert os.path.exists(past_bugs_by_function_path)
            with open(past_bugs_by_function_path, "rb") as f:
                self.past_bugs_by_function = pickle.load(f)

        if model_name == "testselect":
            self.use_test_history = True
            assert db.download_support_file(
                test_scheduling.TEST_SCHEDULING_DB, test_scheduling.PAST_FAILURES_DB
            )
            self.past_failures_data = test_scheduling.get_past_failures()

            self.testfailure_model = download_and_load_model("testfailure")
            assert self.testfailure_model is not None

    def clone_git_repo(self, repo_url, repo_dir, rev="master"):
        logger.info(f"Cloning {repo_url}...")

        if not os.path.exists(repo_dir):
            retry(
                lambda: subprocess.run(
                    ["git", "clone", "--quiet", repo_url, repo_dir], check=True
                )
            )

        retry(
            lambda: subprocess.run(
                ["git", "pull", "--quiet", repo_url, "master"],
                cwd=repo_dir,
                capture_output=True,
                check=True,
            )
        )

        retry(
            lambda: subprocess.run(
                ["git", "checkout", rev], cwd=repo_dir, capture_output=True, check=True
            )
        )

    def update_commit_db(self):
        repository.clone(self.repo_dir)

        assert db.download(repository.COMMITS_DB, support_files_too=True)

        for commit in repository.get_commits():
            pass

        rev_start = "children({})".format(commit["node"])

        repository.download_commits(self.repo_dir, rev_start)

    def has_revision(self, hg, revision):
        if not revision:
            return False
        try:
            hg.identify(revision)
            return True
        except hglib.error.CommandError:
            return False

    def apply_phab(self, hg, diff_id):
        phabricator_api = PhabricatorAPI(
            api_key=get_secret("PHABRICATOR_TOKEN"), url=get_secret("PHABRICATOR_URL")
        )

        # Get the stack of patches
        stack = phabricator_api.load_patches_stack(diff_id)
        assert len(stack) > 0, "No patches to apply"

        # Find the first unknown base revision
        needed_stack = []
        revisions = {}
        for patch in reversed(stack):
            needed_stack.insert(0, patch)

            # Stop as soon as a base revision is available
            if self.has_revision(hg, patch.base_revision):
                logger.info(
                    f"Stopping at diff {patch.id} and revision {patch.base_revision}"
                )
                break

        if not needed_stack:
            logger.info("All the patches are already applied")
            return

        # Load all the diff revisions
        diffs = phabricator_api.search_diffs(diff_phid=[p.phid for p in stack])
        revisions = {
            diff["phid"]: phabricator_api.load_revision(
                rev_phid=diff["revisionPHID"], attachments={"reviewers": True}
            )
            for diff in diffs
        }

        # Update repo to base revision
        hg_base = needed_stack[0].base_revision
        if not self.has_revision(hg, hg_base):
            logger.warning("Missing base revision {} from Phabricator".format(hg_base))
            hg_base = "tip"

        if hg_base:
            hg.update(rev=hg_base, clean=True)
            logger.info(f"Updated repo to {hg_base}")

            if self.git_repo_dir:
                try:
                    self.git_base = vcs_map.mercurial_to_git(hg_base)
                    subprocess.run(
                        ["git", "checkout", "-b", "analysis_branch", self.git_base],
                        check=True,
                        cwd=self.git_repo_dir,
                    )
                    logger.info(f"Updated git repo to {self.git_base}")
                except Exception as e:
                    logger.info(f"Updating git repo to Mercurial {hg_base} failed: {e}")

        def load_user(phid):
            if phid.startswith("PHID-USER"):
                return phabricator_api.load_user(user_phid=phid)
            elif phid.startswith("PHID-PROJ"):
                # TODO: Support group reviewers somehow.
                logger.info(f"Skipping group reviewer {phid}")
            else:
                raise Exception(f"Unsupported reviewer {phid}")

        for patch in needed_stack:
            revision = revisions[patch.phid]

            message = "{}\n\n{}".format(
                revision["fields"]["title"], revision["fields"]["summary"]
            )

            author_name = None
            author_email = None

            if patch.commits:
                author_name = patch.commits[0]["author"]["name"]
                author_email = patch.commits[0]["author"]["email"]

            if author_name is None:
                author = load_user(revision["fields"]["authorPHID"])
                author_name = author["fields"]["realName"]
                # XXX: Figure out a way to know the email address of the author.
                author_email = author["fields"]["username"]

            reviewers = list(
                filter(
                    None,
                    (
                        load_user(reviewer["reviewerPHID"])
                        for reviewer in revision["attachments"]["reviewers"][
                            "reviewers"
                        ]
                    ),
                )
            )
            reviewers = set(reviewer["fields"]["username"] for reviewer in reviewers)

            if len(reviewers):
                message = replace_reviewers(message, reviewers)

            logger.info(
                f"Applying {patch.phid} from revision {revision['id']}: {message}"
            )

            hg.import_(
                patches=io.BytesIO(patch.patch.encode("utf-8")),
                message=message.encode("utf-8"),
                user=f"{author_name} <{author_email}>".encode("utf-8"),
            )

            if self.git_repo_dir:
                with tempfile.TemporaryDirectory() as tmpdirname:
                    temp_file = os.path.join(tmpdirname, "temp.patch")
                    with open(temp_file, "w") as f:
                        f.write(patch.patch)

                    subprocess.run(
                        ["git", "apply", "--3way", temp_file],
                        check=True,
                        cwd=self.git_repo_dir,
                    )
                    subprocess.run(
                        [
                            "git",
                            "-c",
                            f"user.name={author_name}",
                            "-c",
                            f"user.email={author_email}",
                            "commit",
                            "-am",
                            message,
                        ],
                        check=True,
                        cwd=self.git_repo_dir,
                    )

    def generate_feature_importance_data(self, probs, importance):
        X_shap_values = shap.TreeExplainer(self.model.clf).shap_values(self.X)

        pred_class = self.model.le.inverse_transform([probs[0].argmax()])[0]

        features = []
        for i, (val, feature_index, is_positive) in enumerate(
            importance["importances"]["classes"][pred_class][0]
        ):
            name = importance["feature_legend"][str(i + 1)]
            value = importance["importances"]["values"][0, int(feature_index)]

            shap.summary_plot(
                X_shap_values[:, int(feature_index)].reshape(self.X.shape[0], 1),
                self.X[:, int(feature_index)].reshape(self.X.shape[0], 1),
                feature_names=[""],
                plot_type="layered_violin",
                show=False,
            )
            matplotlib.pyplot.xlabel("Impact on model output")
            img = io.BytesIO()
            matplotlib.pyplot.savefig(img, bbox_inches="tight")
            matplotlib.pyplot.clf()
            img.seek(0)
            base64_img = base64.b64encode(img.read()).decode("ascii")

            X = self.X[:, int(feature_index)]
            y = self.y[X != 0]
            X = X[X != 0]
            spearman = spearmanr(X, y)

            buggy_X = X[y == 1]
            clean_X = X[y == 0]
            median = np.median(X)
            median_clean = np.median(clean_X)
            median_buggy = np.median(buggy_X)

            perc_buggy_values_higher_than_median = (
                buggy_X >= median
            ).sum() / buggy_X.shape[0]
            perc_buggy_values_lower_than_median = (
                buggy_X < median
            ).sum() / buggy_X.shape[0]
            perc_clean_values_higher_than_median = (
                clean_X > median
            ).sum() / clean_X.shape[0]
            perc_clean_values_lower_than_median = (
                clean_X <= median
            ).sum() / clean_X.shape[0]

            logger.info("Feature: {}".format(name))
            logger.info("Shap value: {}{}".format("+" if (is_positive) else "-", val))
            logger.info(f"spearman:  {spearman}")
            logger.info(f"value: {value}")
            logger.info(f"overall mean: {np.mean(X)}")
            logger.info(f"overall median: {np.median(X)}")
            logger.info(f"mean for y == 0: {np.mean(clean_X)}")
            logger.info(f"mean for y == 1: {np.mean(buggy_X)}")
            logger.info(f"median for y == 0: {np.median(clean_X)}")
            logger.info(f"median for y == 1: {np.median(buggy_X)}")
            logger.info(
                f"perc_buggy_values_higher_than_median: {perc_buggy_values_higher_than_median}"
            )
            logger.info(
                f"perc_buggy_values_lower_than_median: {perc_buggy_values_lower_than_median}"
            )
            logger.info(
                f"perc_clean_values_higher_than_median: {perc_clean_values_higher_than_median}"
            )
            logger.info(
                f"perc_clean_values_lower_than_median: {perc_clean_values_lower_than_median}"
            )

            features.append(
                {
                    "index": i + 1,
                    "name": name,
                    "shap": float(f'{"+" if (is_positive) else "-"}{val}'),
                    "value": importance["importances"]["values"][0, int(feature_index)],
                    "spearman": spearman,
                    "median": median,
                    "median_bug_introducing": median_buggy,
                    "median_clean": median_clean,
                    "perc_buggy_values_higher_than_median": perc_buggy_values_higher_than_median,
                    "perc_buggy_values_lower_than_median": perc_buggy_values_lower_than_median,
                    "perc_clean_values_higher_than_median": perc_clean_values_higher_than_median,
                    "perc_clean_values_lower_than_median": perc_clean_values_lower_than_median,
                    "plot": base64_img,
                }
            )

        # Group together features that are very similar to each other, so we can simplify the explanation
        # to users.
        attributes = ["Total", "Maximum", "Minimum", "Average"]
        already_added = set()
        feature_groups = []
        for i1, f1 in enumerate(features):
            if i1 in already_added:
                continue

            feature_groups.append([f1])

            for j, f2 in enumerate(features[i1 + 1 :]):
                i2 = j + i1 + 1

                f1_name = f1["name"]
                for attribute in attributes:
                    if f1_name.startswith(attribute):
                        f1_name = f1_name[len(attribute) + 1 :]
                        break

                f2_name = f2["name"]
                for attribute in attributes:
                    if f2_name.startswith(attribute):
                        f2_name = f2_name[len(attribute) + 1 :]
                        break

                if f1_name != f2_name:
                    continue

                already_added.add(i2)
                feature_groups[-1].append(f2)

        # Pick a representative example from each group.
        features = []
        for feature_group in feature_groups:
            shap_sum = sum(f["shap"] for f in feature_group)

            # Only select easily explainable features from the group.
            selected = [
                f
                for f in feature_group
                if (
                    f["shap"] > 0
                    and abs(f["value"] - f["median_bug_introducing"])
                    < abs(f["value"] - f["median_clean"])
                )
                or (
                    f["shap"] < 0
                    and abs(f["value"] - f["median_clean"])
                    < abs(f["value"] - f["median_bug_introducing"])
                )
            ]

            # If there are no easily explainable features in the group, select all features of the group.
            if len(selected) == 0:
                selected = feature_group

            def feature_sort_key(f):
                if f["shap"] > 0 and f["spearman"][0] > 0:
                    return f["perc_buggy_values_higher_than_median"]
                elif f["shap"] > 0 and f["spearman"][0] < 0:
                    return f["perc_buggy_values_lower_than_median"]
                elif f["shap"] < 0 and f["spearman"][0] > 0:
                    return f["perc_clean_values_lower_than_median"]
                elif f["shap"] < 0 and f["spearman"][0] < 0:
                    return f["perc_clean_values_higher_than_median"]

            feature = max(selected, key=feature_sort_key)
            feature["shap"] = shap_sum

            for attribute in attributes:
                if feature["name"].startswith(attribute):
                    feature["name"] = feature["name"][len(attribute) + 1 :].capitalize()
                    break

            features.append(feature)

        with open("importances.json", "w") as f:
            json.dump(features, f)

    def classify(self, item):
        """Classify a commit.

        Args:
            item (str): A revision or phabricator diff ID.
        """
        self.update_commit_db()

        with hglib.open(self.repo_dir) as hg:
            if self.has_revision(hg, item):
                patch_rev = item
            else:
                # Assume it's a phabricator diff ID.
                self.apply_phab(hg, item)
                patch_rev = hg.log(revrange="not public()")[0].node

            # Analyze patch.
            commits = repository.download_commits(
                self.repo_dir, rev_start=patch_rev.decode("utf-8"), save=False
            )

        # We use "clean" (or "dirty") commits as the background dataset for feature importance.
        # This way, we can see the features which are most important in differentiating
        # the current commit from the "clean" (or "dirty") commits.

        if not self.use_test_history:
            probs, importance = self.model.classify(
                commits[-1],
                probabilities=True,
                importances=True,
                background_dataset=lambda v: self.X[self.y != v],
                importance_cutoff=0.05,
            )

            self.generate_feature_importance_data(probs, importance)

            with open("probs.json", "w") as f:
                json.dump(probs[0].tolist(), f)

            if self.model_name == "regressor" and self.method_defect_predictor_dir:
                self.classify_methods(commits[-1])
        else:
            testfailure_probs = self.testfailure_model.classify(
                commits[-1], probabilities=True
            )

            logger.info(f"Test failure risk: {testfailure_probs[0][1]}")

            commit_data = commit_features.merge_commits(commits)

            push_num = self.past_failures_data["push_num"]

            # XXX: Consider using mozilla-central built-in rules to filter some of these out, e.g. SCHEDULES.
            # XXX: Consider using the runnable jobs artifact from the Gecko Decision task.
            all_tasks = self.past_failures_data["all_tasks"]

            # XXX: For now, only restrict to test-linux64 tasks.
            all_tasks = [
                t
                for t in all_tasks
                if t.startswith("test-linux64/") and "test-verify" not in t
            ]

            commit_tests = []
            for data in test_scheduling.generate_data(
                self.past_failures_data, commit_data, push_num, all_tasks, [], []
            ):
                if not data["name"].startswith("test-"):
                    continue

                commit_test = commit_data.copy()
                commit_test["test_job"] = data
                commit_tests.append(commit_test)

            probs = self.model.classify(commit_tests, probabilities=True)
            selected_indexes = np.argwhere(
                probs[:, 1] > float(get_secret("TEST_SELECTION_CONFIDENCE_THRESHOLD"))
            )[:, 0]
            selected_tasks = [
                commit_tests[i]["test_job"]["name"] for i in selected_indexes
            ]

            with open("failure_risk", "w") as f:
                f.write(
                    "1"
                    if testfailure_probs[0][1]
                    > float(get_secret("TEST_FAILURE_CONFIDENCE_THRESHOLD"))
                    else "0"
                )

            # It isn't worth running the build associated to the tests, if we only run three test tasks.
            if len(selected_tasks) < 3:
                selected_tasks = []

            with open("selected_tasks", "w") as f:
                f.writelines(f"{selected_task}\n" for selected_task in selected_tasks)

    def classify_methods(self, commit):
        # Get commit hash from 4 months before the analysis time.
        # The method-level analyzer needs 4 months of history.
        four_months_ago = datetime.utcnow() - relativedelta(months=4)
        p = subprocess.run(
            [
                "git",
                "rev-list",
                "-n",
                "1",
                "--until={}".format(four_months_ago.strftime("%Y-%m-%d")),
                "HEAD",
            ],
            check=True,
            capture_output=True,
            cwd=self.git_repo_dir,
        )

        stop_hash = p.stdout.decode().strip()

        # Run the method-level analyzer.
        subprocess.run(
            [
                "python3",
                "tester.py",
                "--repo",
                self.git_repo_dir,
                "--start",
                "HEAD",
                "--stop",
                stop_hash,
                "--output",
                os.path.abspath("method_level.csv"),
            ],
            cwd=self.method_defect_predictor_dir,
        )

        method_level_results = []
        try:
            with open("method_level.csv", "r") as f:
                reader = csv.DictReader(f)
                for item in reader:
                    item["past_bugs"] = []
                    method_level_results.append(item)
        except FileNotFoundError:
            # No methods were classified.
            pass

        for method_level_result in method_level_results:
            method_level_result_path = method_level_result["file_name"]
            if method_level_result_path not in self.past_bugs_by_function:
                continue

            for path, functions in commit["functions"].items():
                if method_level_result_path != path:
                    continue

                for function_name, _, _ in functions:
                    if function_name not in self.past_bugs_by_function[path]:
                        continue

                    if method_level_result["method_name"].endswith(function_name):
                        method_level_result["past_bugs"] = list(
                            self.past_bugs_by_function[path][function_name]["bugs"]
                        )

        with open("method_level.json", "w") as f:
            json.dump(method_level_results, f)


def main():
    description = "Classify a commit"
    parser = argparse.ArgumentParser(description=description)

    parser.add_argument("model", help="Which model to use for evaluation")
    parser.add_argument("gecko_path", help="Path to a Gecko repository. If no repository exists, "
                                           "it will be cloned to this location.")
    parser.add_argument("item", help="A revision or Phabricator diff ID to analyze.")
    parser.add_argument(
        "--git_repo_dir", help="Path where the git repository will be cloned."
    )
    parser.add_argument(
        "--method_defect_predictor_dir",
        help="Path where the git repository will be cloned.",
    )

    args = parser.parse_args()

    classifier = CommitClassifier(
        args.model, args.gecko_path, args.git_repo_dir, args.method_defect_predictor_dir
    )
    classifier.classify(args.item)


if __name__ == "__main__":
    main()
