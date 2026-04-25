"""Unit tests for the repo-folder + repo-folder-prefix matching helpers.

Focused on the prefix-match semantics used by the --repo-folder-prefix flag
(introduced for git-worktree workflows): exact match, path and branch-name
separator boundaries, and the key false-positive case of `/tmp/myrepository`
not matching prefix `/tmp/myrepo`.
"""

from __future__ import annotations

import pytest

from multitrack import cwd_matches_prefix, cwd_matches_repo, repo_label_for


class TestCwdMatchesPrefix:
    def test_exact_match(self):
        assert cwd_matches_prefix("/tmp/repo", "/tmp/repo")

    @pytest.mark.parametrize("tail", ["/sub", "/deep/nested/sub", "-feature-x", "_hotfix", ".bak"])
    def test_allowed_separators(self, tail):
        assert cwd_matches_prefix(f"/tmp/repo{tail}", "/tmp/repo")

    @pytest.mark.parametrize(
        "cwd",
        [
            "/tmp/repository",  # letters continue the word
            "/tmp/repo2",  # digit continues the word
            "/tmp/repoXYZ",  # uppercase letter
        ],
    )
    def test_word_continuation_is_rejected(self, cwd):
        """Prefix `/tmp/repo` must not match `/tmp/repository` etc."""
        assert not cwd_matches_prefix(cwd, "/tmp/repo")

    def test_completely_unrelated_is_rejected(self):
        assert not cwd_matches_prefix("/tmp/other", "/tmp/repo")

    def test_empty_cwd(self):
        assert not cwd_matches_prefix("", "/tmp/repo")

    def test_prefix_is_substring_but_not_prefix(self):
        """'tmp/repo' appears in the cwd but not at the start — no match."""
        assert not cwd_matches_prefix("/home/alex/tmp/repo", "/tmp/repo")


class TestCwdMatchesRepo:
    def test_no_filters_matches_everything(self):
        assert cwd_matches_repo("/anywhere/at/all", [], [])

    def test_strict_folder_match(self):
        assert cwd_matches_repo("/tmp/repo/sub", ["/tmp/repo"], [])
        assert cwd_matches_repo("/tmp/repo", ["/tmp/repo"], [])

    def test_sibling_worktree_rejected_by_strict_folder(self):
        """Exactly the regression that motivated --repo-folder-prefix."""
        assert not cwd_matches_repo("/tmp/repo-feature", ["/tmp/repo"], [])

    def test_sibling_worktree_accepted_by_prefix(self):
        assert cwd_matches_repo("/tmp/repo-feature", [], ["/tmp/repo"])

    def test_prefix_also_matches_main_and_subdir(self):
        assert cwd_matches_repo("/tmp/repo", [], ["/tmp/repo"])
        assert cwd_matches_repo("/tmp/repo/nested/deep", [], ["/tmp/repo"])

    def test_prefix_rejects_word_continuation(self):
        assert not cwd_matches_repo("/tmp/repository", [], ["/tmp/repo"])

    def test_folders_and_prefixes_are_unioned(self):
        """Either kind matching is enough."""
        assert cwd_matches_repo("/tmp/alpha", ["/tmp/alpha"], ["/tmp/repo"])
        assert cwd_matches_repo("/tmp/repo-x", ["/tmp/alpha"], ["/tmp/repo"])
        assert not cwd_matches_repo("/tmp/other", ["/tmp/alpha"], ["/tmp/repo"])


class TestRepoLabelFor:
    def test_folder_match_returns_folder(self):
        assert repo_label_for("/tmp/repo/sub", ["/tmp/repo"], []) == "/tmp/repo"

    def test_prefix_match_returns_prefix_so_worktrees_group(self):
        """All worktrees sharing a prefix should land under the same chip."""
        label = "/tmp/repo"
        assert repo_label_for("/tmp/repo", [], [label]) == label
        assert repo_label_for("/tmp/repo-feature-x", [], [label]) == label
        assert repo_label_for("/tmp/repo-bugfix", [], [label]) == label

    def test_unmatched_cwd_falls_back_to_cwd(self):
        assert repo_label_for("/tmp/unrelated", ["/tmp/repo"], ["/other"]) == "/tmp/unrelated"

    def test_empty_cwd_is_labeled_unknown(self):
        assert repo_label_for("", [], []) == "(unknown repo)"

    def test_folder_wins_over_prefix_when_both_match(self):
        """Strict folder match is evaluated first, so its label takes precedence."""
        assert repo_label_for("/tmp/repo/sub", ["/tmp/repo/sub"], ["/tmp/repo"]) == "/tmp/repo/sub"
