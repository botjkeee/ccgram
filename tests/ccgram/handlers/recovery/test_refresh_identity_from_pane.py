"""Regression: _refresh_identity_from_pane's empty-command guard is backend-split.

herdr fills ``pane_current_command`` with the agent label, which goes empty
the moment the agent exits — unlike tmux, where an empty command is a
transient read. Before this fix the guard bailed on any empty command,
so on herdr the provider never re-detected the shell and the approval flow
never engaged. tmux keeps today's early-out.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from ccgram.handlers.recovery.transcript_discovery import _refresh_identity_from_pane
from ccgram.multiplexer.base import WindowRef
from ccgram.window_state_ports import identity_state


def _identity() -> identity_state.IdentityProjection:
    return identity_state.IdentityProjection(
        window_id="w2:t1",
        cwd="/x",
        session_id="",
        transcript_path=None,
        provider_name="claude",
        window_name="agent",
        approval_mode="default",
    )


async def test_tmux_empty_command_keeps_early_out() -> None:
    identity = _identity()
    w = WindowRef(
        window_id="w2:t1", window_name="app", cwd="/x", pane_current_command=""
    )
    caps = SimpleNamespace(native_agent_status=False)
    with (
        patch(
            "ccgram.handlers.recovery.transcript_discovery.tmux_manager",
            SimpleNamespace(capabilities=caps),
        ),
        patch(
            "ccgram.handlers.recovery.transcript_discovery._detect_and_apply_provider",
            new_callable=AsyncMock,
        ) as mock_detect,
    ):
        result = await _refresh_identity_from_pane(
            "w2:t1", identity, w, client=None, chat_id=0, thread_id=0
        )
    assert result == (identity, False)
    mock_detect.assert_not_awaited()


async def test_herdr_empty_command_proceeds_to_redetect() -> None:
    identity = _identity()
    w = WindowRef(
        window_id="w2:t1", window_name="app", cwd="/x", pane_current_command=""
    )
    caps = SimpleNamespace(native_agent_status=True)
    with (
        patch(
            "ccgram.handlers.recovery.transcript_discovery.tmux_manager",
            SimpleNamespace(capabilities=caps),
        ),
        patch(
            "ccgram.handlers.recovery.transcript_discovery._detect_and_apply_provider",
            new_callable=AsyncMock,
        ) as mock_detect,
        patch(
            "ccgram.handlers.recovery.transcript_discovery.get_cached_foreground_pgid",
            return_value=0,
        ),
        patch.object(identity_state, "get_identity", return_value=identity),
    ):
        result = await _refresh_identity_from_pane(
            "w2:t1", identity, w, client=None, chat_id=0, thread_id=0
        )
    mock_detect.assert_awaited_once()
    assert result == (identity, False)
