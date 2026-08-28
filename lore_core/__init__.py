# SPDX-License-Identifier: AGPL-3.0-only
"""lore_core -- the importable core of lore's Hermes-pattern memory.

Extracted from the monolithic bin/lore.py (Phase 1 slice 1 of the lore-tui
plan, 2026-08-22) so the marketplace plugin's CLI shim (bin/lore.py) and the
future lore-tui daemon import the same code -- one source of truth, byte-
identical CLI behavior. Internal distribution codename: engram-042953b4f8c8.

Submodules, bottom of the dependency graph first (each imports only from
modules earlier in this list, plus one deliberate deferred exception noted
in deriver.py's docstring):

    config     env-derived constants + dependency-free helpers
    gate       write gate + provenance ledger (ISSUE #43)
    scrub      secret scrubbing (the ingest choke point)
    store      tier 2: session index (SQLite FTS5, transcript parsing)
    memory     tier 1: curated core memory (USER.md / MEMORY.md)
    filemap    project file map (path — purpose rows, pull-on-demand)
    beliefs    belief store: insert/supersede, outcomes ledger, calibration
    graph      traversal over the belief graph (adjacency, paths, communities)
    deriver    tier 3 deriver role: digest, review prompt, worker/jobfile
    dreamer    tier 3 dreamer role: belief reconciliation, promotions
    dialectic  `lore ask` / `lore consult` evidence-gathering
    pending    staged proposals: list/approve/reject/archive
    context    memory snapshot rendering + SessionStart/refresh/MOTD

Off that graph, importing nothing from the package and imported by
nothing in it:

    version    which version this copy is, plugin manifest or wheel metadata

This package re-exports every public name from every submodule (see each
module's own __all__) as the package's public surface -- import from
`lore_core` directly, or from the specific submodule for a narrower
dependency footprint.
"""

from .config import *  # noqa: F401,F403
from .gate import *  # noqa: F401,F403
from .scrub import *  # noqa: F401,F403
from .store import *  # noqa: F401,F403
from .memory import *  # noqa: F401,F403
from .filemap import *  # noqa: F401,F403
from .beliefs import *  # noqa: F401,F403
from .graph import *  # noqa: F401,F403
from .deriver import *  # noqa: F401,F403
from .dreamer import *  # noqa: F401,F403
from .dialectic import *  # noqa: F401,F403
from .pending import *  # noqa: F401,F403
from .context import *  # noqa: F401,F403

from . import config as _config
from . import gate as _gate
from . import scrub as _scrub
from . import store as _store
from . import memory as _memory
from . import filemap as _filemap
from . import beliefs as _beliefs
from . import graph as _graph
from . import deriver as _deriver
from . import dreamer as _dreamer
from . import dialectic as _dialectic
from . import pending as _pending
from . import context as _context

__all__ = [
    *_config.__all__,
    *_gate.__all__,
    *_scrub.__all__,
    *_store.__all__,
    *_memory.__all__,
    *_filemap.__all__,
    *_beliefs.__all__,
    *_graph.__all__,
    *_deriver.__all__,
    *_dreamer.__all__,
    *_dialectic.__all__,
    *_pending.__all__,
    *_context.__all__,
]

# Deliberately outside __all__: a dunder is not part of a star-import's
# surface. It is here so that `lore_core.__version__` answers the way every
# other package on the machine does -- DOXA's /about reads exactly this,
# and so does anything else that wants to know which LORE it got without
# knowing whether it came from a plugin checkout or a wheel.
from .version import resolve_version as _resolve_version  # noqa: E402

__version__ = _resolve_version()
