# SPDX-License-Identifier: AGPL-3.0-only
import tempfile
from pathlib import Path

from benchmark import project_identity_root

with tempfile.TemporaryDirectory() as directory:
    assert project_identity_root(directory) == str(Path(directory).resolve())
print("public checks passed")
