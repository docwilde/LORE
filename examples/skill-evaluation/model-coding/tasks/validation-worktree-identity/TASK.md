# A linked worktree shares its main repository's memory identity

Repair `project_identity_root` in `benchmark.py`. Using `--show-toplevel`
directly assigns each linked worktree its own ephemeral memory store. A linked
worktree, including a cwd nested inside it, must identify the main worktree.
An ordinary repository still identifies its own root from nested directories.

Ask Git for the toplevel and common Git directory with a bounded subprocess
call. Interpret a relative common directory against the supplied cwd. If the
common directory is named `.git` and its parent exists, use that parent's
resolved absolute path. Unusual layouts such as `--separate-git-dir` keep the
reported toplevel. On Git error, malformed output, missing cwd or timeout,
fall back to the existing `project_root` helper. Nonrepositories keep their
resolved input path. Do not change `project_root`, which defines file-map paths.

Change only `benchmark.py`. Python 3.11+ standard library and Git are available.
Run `python3 test_public.py`.
