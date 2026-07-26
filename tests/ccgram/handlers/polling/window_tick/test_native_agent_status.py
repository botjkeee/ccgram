"""Unit tests for the native-agent-status gap-fill in ``observe``.

``_native_agent_status`` synthesizes a busy ``StatusUpdate`` from a backend's
native agent state (herdr) when terminal scraping yielded nothing. It is gated
on ``capabilities.native_agent_status`` and only surfaces ``working`` /
``blocked``; ``idle`` / ``done`` / ``unknown`` return None so the existing
activity-based idle/done logic stays in control.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ccgram.handlers.polling.window_tick import observe
from ccgram.handlers.polling.window_tick.observe import _native_agent_status
from ccgram.handlers.polling.polling_types import is_shell_prompt
from ccgram.multiplexer import agent_status_cache
from ccgram.multiplexer.base import AgentStatus, ForegroundInfo, WindowRef


@pytest.fixture(autouse=True)
def _clear_status_cache():
    # The push-status cache is a process-global; isolate every test so the
    # subprocess-fallback tests see a cold cache regardless of run order.
    agent_status_cache.reset()
    yield
    agent_status_cache.reset()


def _fake_mux(native: bool, status: AgentStatus | None) -> MagicMock:
    mux = MagicMock()
    mux.capabilities = SimpleNamespace(native_agent_status=native)
    mux.agent_status = AsyncMock(return_value=status)
    return mux


def _herdr_like_caps() -> SimpleNamespace:
    """Capabilities double exposing only what ``effective_window`` reads."""
    return SimpleNamespace(native_agent_status=True)


async def test_returns_none_when_backend_lacks_native_status() -> None:
    mux = _fake_mux(native=False, status=AgentStatus(state="working"))
    with patch("ccgram.handlers.polling.window_tick.observe.tmux_manager", mux):
        assert await _native_agent_status("w2:t1") is None
    mux.agent_status.assert_not_awaited()  # gated before the call


async def test_working_state_becomes_busy_status() -> None:
    mux = _fake_mux(
        native=True,
        status=AgentStatus(state="working", agent="codex", custom_status="indexing"),
    )
    with patch("ccgram.handlers.polling.window_tick.observe.tmux_manager", mux):
        status = await _native_agent_status("w2:t1")
    assert status is not None
    assert status.raw_text == "indexing"  # custom_status preferred
    assert status.is_interactive is False


async def test_working_without_custom_status_uses_default_label() -> None:
    mux = _fake_mux(native=True, status=AgentStatus(state="working", agent="codex"))
    with patch("ccgram.handlers.polling.window_tick.observe.tmux_manager", mux):
        status = await _native_agent_status("w2:t1")
    assert status is not None
    assert status.raw_text == "working"


async def test_blocked_state_surfaces_waiting() -> None:
    mux = _fake_mux(native=True, status=AgentStatus(state="blocked", agent="claude"))
    with patch("ccgram.handlers.polling.window_tick.observe.tmux_manager", mux):
        status = await _native_agent_status("w2:t1")
    assert status is not None
    assert status.raw_text == "waiting for input"


@pytest.mark.parametrize("state", ["idle", "done", "unknown"])
async def test_idle_done_unknown_yield_none(state: str) -> None:
    mux = _fake_mux(native=True, status=AgentStatus(state=state, agent="claude"))
    with patch("ccgram.handlers.polling.window_tick.observe.tmux_manager", mux):
        assert await _native_agent_status("w2:t1") is None


async def test_none_native_status_yields_none() -> None:
    mux = _fake_mux(native=True, status=None)
    with patch("ccgram.handlers.polling.window_tick.observe.tmux_manager", mux):
        assert await _native_agent_status("w2:t1") is None


async def test_cache_hit_skips_subprocess() -> None:
    # A warm push cache is read synchronously; the subprocess agent_status()
    # call is skipped (the per-tick subprocess the event stream replaces).
    mux = _fake_mux(native=True, status=AgentStatus(state="idle"))
    agent_status_cache.set_status(
        "w2:t1", AgentStatus(state="working", agent="codex", custom_status="linking")
    )
    with patch("ccgram.handlers.polling.window_tick.observe.tmux_manager", mux):
        status = await _native_agent_status("w2:t1")
    assert status is not None
    assert status.raw_text == "linking"  # from the cache, not the subprocess
    mux.agent_status.assert_not_awaited()


def _herdr_like_mux() -> MagicMock:
    """A native_agent_status mux double that counts ``agent_status`` calls."""
    mux = MagicMock()
    mux.capabilities = SimpleNamespace(native_agent_status=True)
    mux.agent_status_calls = 0

    async def _agent_status(window_id: str) -> AgentStatus | None:
        mux.agent_status_calls += 1
        return AgentStatus(state="working", agent="codex")

    mux.agent_status = _agent_status
    return mux


async def test_known_no_agent_skips_subprocess_fallback() -> None:
    agent_status_cache.set_status("w2:t1", None)  # negative marker
    fake_mux = _herdr_like_mux()
    with patch("ccgram.handlers.polling.window_tick.observe.tmux_manager", fake_mux):
        assert await _native_agent_status("w2:t1") is None
    assert fake_mux.agent_status_calls == 0  # no backend lookup on warm-none


async def test_cold_cache_falls_back_to_subprocess() -> None:
    mux = _fake_mux(native=True, status=AgentStatus(state="working", agent="codex"))
    with patch("ccgram.handlers.polling.window_tick.observe.tmux_manager", mux):
        status = await _native_agent_status("w2:t1")
    assert status is not None
    assert status.raw_text == "working"
    mux.agent_status.assert_awaited_once()  # cold cache → one subprocess call


class _FakeMux:
    """Minimal multiplexer double (native_agent_status backend)."""

    def __init__(self, fg_argv: list[str] | None):
        self.capabilities = _herdr_like_caps()
        self._fg_argv = fg_argv

    async def foreground(self, window_id: str):
        if self._fg_argv is None:
            return None
        return ForegroundInfo(pid=1, pgid=1, argv=self._fg_argv, cwd="/x", tty="")


async def test_effective_window_fills_command_from_foreground(monkeypatch) -> None:
    w = WindowRef(
        window_id="w2:t1", window_name="app", cwd="/x", pane_current_command=""
    )
    # The multiplexer proxy has __slots__ = () — patch the MODULE BINDING, not
    # proxy attributes (idiom above: tmux_manager patched via monkeypatch.setattr).
    monkeypatch.setattr(observe, "tmux_manager", _FakeMux(["/bin/zsh"]))
    out = await observe.effective_window("w2:t1", w)
    assert out.pane_current_command == "zsh"
    assert is_shell_prompt(out.pane_current_command) is True


async def test_effective_window_normalizes_login_shell_argv(monkeypatch) -> None:
    # herdr login-mode panes report argv0 "-zsh" (portable-pty prepends "-").
    w = WindowRef(
        window_id="w2:t1", window_name="app", cwd="/x", pane_current_command=""
    )
    monkeypatch.setattr(observe, "tmux_manager", _FakeMux(["-zsh"]))
    out = await observe.effective_window("w2:t1", w)
    assert out.pane_current_command == "zsh"


async def test_effective_window_keeps_agent_label(monkeypatch) -> None:
    w = WindowRef(
        window_id="w2:t1", window_name="app", cwd="/x", pane_current_command="claude"
    )
    monkeypatch.setattr(observe, "tmux_manager", _FakeMux(None))
    assert (await observe.effective_window("w2:t1", w)) is w
