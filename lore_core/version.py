"""lore_core.version -- ONE version, whichever carrier this copy arrived in.

``.claude-plugin/plugin.json`` is the source of truth. It has to be: it is
the file Claude Code reads to decide which build of the plugin is
installed, and a library that declared its own version separately would
give one machine two answers the first time somebody forgot to edit both.
Everything else derives.

* **A plugin checkout** (the manifest is right there, one directory above
  the package) reads it directly. This is the copy DOXA's bootstrap puts
  on ``sys.path`` and the copy ``bin/lore.py`` imports, and it must never
  answer "unknown" -- the file that declares the version is on disk.
* **An installed distribution** (``pip install lore-core``) has no plugin
  manifest -- only ``lore_core/`` is packaged -- so it reads the wheel
  metadata that was BUILT from that same manifest
  (``pyproject.toml``'s ``[tool.hatch.version]`` reads the identical
  file at build time).

Order matters, and it is the same order DOXA's ``doxa/version.py`` uses
for the same reason: the checkout wins. If a machine has the plugin at
0.36.0 and an older ``lore-core`` wheel in its environment, whatever is
EXECUTING is the checkout, and that is what the version has to say.
"""

from __future__ import annotations

import json
from pathlib import Path

DIST_NAME = "lore-core"

__all__ = ["DIST_NAME", "plugin_manifest_path", "resolve_version"]


def plugin_manifest_path() -> "Path | None":
    """``.claude-plugin/plugin.json`` for the plugin tree this package sits
    inside, or None when it does not sit inside one (an installed wheel).

    Identified by a manifest that actually declares THIS plugin -- a
    manifest belonging to some other plugin that happens to be an ancestor
    directory is not ours."""
    manifest = Path(__file__).resolve().parent.parent / ".claude-plugin" / "plugin.json"
    if not manifest.is_file():
        return None
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("name") != "lore":
        return None
    return manifest


def _from_manifest() -> "str | None":
    manifest = plugin_manifest_path()
    if manifest is None:
        return None
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    version = data.get("version") if isinstance(data, dict) else None
    return str(version) if version else None


def _from_metadata() -> "str | None":
    try:
        from importlib.metadata import version

        return str(version(DIST_NAME))
    except Exception:  # noqa: BLE001 -- PackageNotFoundError and any importlib oddity
        return None


def resolve_version() -> str:
    """The version string every surface shows. Plugin manifest first,
    installed metadata second; "unknown" only for a copy that is BOTH not
    a plugin checkout and not an installed distribution -- a loose
    directory on ``sys.path``, which is a broken install, not a supported
    way to run."""
    return _from_manifest() or _from_metadata() or "unknown"
