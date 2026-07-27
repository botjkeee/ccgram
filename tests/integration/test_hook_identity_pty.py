"""PTY-level test for the real hook runtime shape (Task 10).

Mirrors exactly how Claude Code spawns command hooks: an agent process
sitting on a pty (session leader on the pane's terminal) spawns the hook
detached via ``setsid`` with piped stdio, so the hook has NO controlling tty.
The identity resolver must therefore read tty evidence from the ancestor
chain (``hook._ancestor_tty``), not from the hook process itself.
"""

from __future__ import annotations

import json
import os
import pty
import subprocess
import sys
from pathlib import Path

import pytest

SRC_DIR = Path(__file__).resolve().parents[2] / "src"


@pytest.mark.skipif(not Path("/proc").exists(), reason="needs /proc")
def test_detached_hook_resolves_herdr_identity_under_stale_tmux(tmp_path) -> None:
    """agent-on-pty → setsid-detached hook child with piped stdio (the real
    Claude Code spawn shape) must resolve the herdr identity, not stale tmux."""
    probe = tmp_path / "probe.py"
    probe.write_text(
        "import json, sys\n"
        f"sys.path.insert(0, {str(SRC_DIR)!r})\n"
        "from ccgram.hook import _ancestor_tty\n"
        "from ccgram.multiplexer.self_identify import resolve_self_identity\n"
        "identity = resolve_self_identity(\n"
        "    {'TMUX_PANE': '%1', 'HERDR_PANE_ID': 'w2:p1'},\n"
        "    tmux_query=lambda p: ('ccgram:@5', '@5', 'app', '/dev/pts/999'),\n"
        "    herdr_query=lambda p: 'w2:t1',\n"
        "    process_tty=_ancestor_tty(),\n"
        ")\n"
        "print(json.dumps({'mux': identity.mux if identity else None}))\n"
    )
    controller, follower = pty.openpty()
    # sh stands in for the agent (stdin = pane pty); it spawns the probe via
    # setsid — detached, piped stdio — exactly like Claude Code spawns hooks.
    proc = subprocess.run(
        ["sh", "-c", f"setsid {sys.executable} {probe}"],
        stdin=follower,
        capture_output=True,
        text=True,
        timeout=30,
    )
    os.close(controller), os.close(follower)
    assert json.loads(proc.stdout) == {"mux": "herdr"}
