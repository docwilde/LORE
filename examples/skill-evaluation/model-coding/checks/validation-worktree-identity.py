# SPDX-License-Identifier: AGPL-3.0-only
import importlib.util
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("solution", "/workspace/benchmark.py")
solution = importlib.util.module_from_spec(spec)
spec.loader.exec_module(solution)


def git(cwd, *args):
    return subprocess.run(["git", "-c", "user.name=Benchmark", "-c", "user.email=benchmark@invalid", *args],
                          cwd=cwd, check=True, capture_output=True, text=True, timeout=3)


class IdentityChecks(unittest.TestCase):
    def test_real_linked_worktree_and_main_nested_cwds(self):
        with tempfile.TemporaryDirectory(prefix="identity space ") as directory:
            base = Path(directory)
            main = base / "main"
            main.mkdir()
            git(main, "init", "--quiet")
            git(main, "commit", "--quiet", "--allow-empty", "-m", "fixture")
            linked = base / "linked"
            git(main, "worktree", "add", "--quiet", "-b", "fixture-branch", str(linked))
            for cwd in (main, linked, main / "nested/deeper", linked / "nested/deeper"):
                cwd.mkdir(parents=True, exist_ok=True)
                self.assertEqual(solution.project_identity_root(str(cwd)), str(main.resolve()))
            self.assertEqual(solution.project_root(str(linked)), str(linked.resolve()))

    def test_relative_common_directory_is_relative_to_cwd(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cwd = root / "nested"
            cwd.mkdir()
            value = subprocess.CompletedProcess([], 0, str(root) + "\n../.git\n", "")
            with patch.object(solution.subprocess, "run", return_value=value) as run:
                self.assertEqual(solution.project_identity_root(str(cwd)), str(root))
                self.assertIn("--git-common-dir", run.call_args.args[0])
                self.assertGreater(run.call_args.kwargs["timeout"], 0)
                self.assertLessEqual(run.call_args.kwargs["timeout"], 5)

    def test_separate_git_directory_keeps_toplevel(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            checkout = root / "checkout"
            checkout.mkdir()
            git(checkout, "init", "--quiet", "--separate-git-dir", str(root / "metadata"))
            self.assertEqual(solution.project_identity_root(str(checkout)), str(checkout))

    def test_malformed_output_and_errors_use_existing_fallback(self):
        for result in (subprocess.CompletedProcess([], 1, "", ""),
                       subprocess.CompletedProcess([], 0, "one-line\n", "")):
            with (patch.object(solution, "project_root", return_value="fallback") as fallback,
                  patch.object(solution.subprocess, "run", return_value=result)):
                self.assertEqual(solution.project_identity_root("/missing"), "fallback")
                fallback.assert_called_once_with("/missing")
        for error in (OSError("missing"), subprocess.TimeoutExpired("git", 5)):
            with (patch.object(solution, "project_root", return_value="fallback"),
                  patch.object(solution.subprocess, "run", side_effect=error)):
                self.assertEqual(solution.project_identity_root("/missing"), "fallback")
