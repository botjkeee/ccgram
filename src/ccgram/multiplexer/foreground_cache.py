"""Backend-neutral per-window memo of the resolved foreground command.

``observe.effective_window`` resolves a herdr window's live foreground
process (``pane_current_command`` empty, e.g. the agent exited and left a
bare shell) via ``tmux_manager.foreground``, which on herdr costs two CLI
spawns. The native field stays empty in that steady state, so without a
memo ``effective_window`` re-forks on every poll tick forever. This module
bounds that cost with a short per-``window_id`` memo (a few seconds) of the
last resolved command — separate from ``agent_status_cache`` (push-updated,
no TTL by design; this one is pull-only and time-bounded).

Pure module — depends on nothing but the stdlib, mirrors the flat-function
shape of ``agent_status_cache``. All access is from the single asyncio
event-loop thread, so a plain dict suffices.
"""

from __future__ import annotations

import time

# A few seconds is enough to collapse the per-tick (1s default) subprocess
# churn without letting a stale command survive long after it changes.
_TTL_SECONDS = 5.0

_cache: dict[str, tuple[float, str]] = {}


def lookup(window_id: str) -> tuple[bool, str]:
    """Return ``(warm, cmd)``; a cold or TTL-expired entry is ``(False, "")``."""
    entry = _cache.get(window_id)
    if entry is None:
        return False, ""
    ts, cmd = entry
    if time.monotonic() - ts >= _TTL_SECONDS:
        del _cache[window_id]
        return False, ""
    return True, cmd


def set_command(window_id: str, cmd: str) -> None:
    """Record the resolved foreground command (``""`` = none resolved)."""
    _cache[window_id] = (time.monotonic(), cmd)


def clear(window_id: str) -> None:
    """Drop the cached command for *window_id* (e.g. on window death)."""
    _cache.pop(window_id, None)


def reset() -> None:
    """Clear the whole cache (test isolation)."""
    _cache.clear()
