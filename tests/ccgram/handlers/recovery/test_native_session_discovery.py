"""Positive-path coverage for the consult-first native agent-session discovery
flow shipped in ef0b5ac.

Every existing transcript-discovery test pins ``native_agent_session = False``,
so the multiplexer-first branch (``_native_session_transcript`` ->
``_transcript_for_session_id`` cwd encoding + glob fallback, pi
``"<ts>_<id>"`` stem parsing, ``_register_native_session`` dedupe) has had no
positive-path coverage until now.

Follows ``test_transcript_discovery_key.py``'s stubbing idiom: ``tmux_manager``
and ``session_map_sync`` are module-level proxies with ``__slots__ = ()``, so
tests must rebind the whole ``transcript_discovery.<name>`` module attribute
rather than mutate the proxy instance's own attributes. ``config`` has no
module-level binding in ``transcript_discovery`` (imported lazily inside
functions), so the singleton itself is patched via
``from ccgram.config import config``.
"""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from ccgram.config import config
from ccgram.handlers.recovery import transcript_discovery
from ccgram.multiplexer.base import AgentSessionRef
from ccgram.window_state_ports import identity_state


def _stub_native_session(monkeypatch, ref: AgentSessionRef | None) -> MagicMock:
    """Rebind ``transcript_discovery.tmux_manager`` to a native-session backend.

    Rebinds the module attribute rather than setting attributes on the real
    proxy instance, which has ``__slots__ = ()`` and forwards via
    ``__getattr__`` only.
    """
    stub = MagicMock()
    stub.capabilities.native_agent_session = True
    stub.agent_session = AsyncMock(return_value=ref)
    stub.find_window_by_id = AsyncMock(return_value=None)
    monkeypatch.setattr(transcript_discovery, "tmux_manager", stub)
    return stub


def _stub_session_map_sync(monkeypatch, calls: list) -> MagicMock:
    """Rebind ``transcript_discovery.session_map_sync``, recording both writes."""
    stub = MagicMock()
    stub.register_hookless_session = lambda **kw: calls.append(("register", kw))
    stub.write_hookless_session_map = lambda **kw: calls.append(("write", kw))
    monkeypatch.setattr(transcript_discovery, "session_map_sync", stub)
    return stub


def _identity_with(
    *,
    window_id: str = "w2:t1",
    provider_name: str = "claude",
    session_id: str = "",
    cwd: str = "/proj",
    transcript_path: Path | None = None,
) -> identity_state.IdentityProjection:
    return identity_state.IdentityProjection(
        window_id=window_id,
        provider_name=provider_name,
        session_id=session_id,
        cwd=cwd,
        transcript_path=transcript_path,
        window_name="agent",
        approval_mode="default",
    )


async def test_path_kind_registers_pi_transcript(monkeypatch, tmp_path) -> None:
    transcript = tmp_path / "1234_abcd.jsonl"
    transcript.write_text("{}\n")
    _stub_native_session(
        monkeypatch, AgentSessionRef(kind="path", value=str(transcript), agent="pi")
    )

    native = await transcript_discovery._native_session_transcript("w2:t1", "/proj")

    assert native == (transcript, "abcd")  # pi stem: "<ts>_<session id>"


async def test_id_kind_resolves_via_cwd_encoding(monkeypatch, tmp_path) -> None:
    projects = tmp_path / "projects"
    (projects / "-home-u-proj").mkdir(parents=True)
    transcript = projects / "-home-u-proj" / "sid-1.jsonl"
    transcript.write_text("{}\n")
    monkeypatch.setattr(config, "claude_projects_path", projects)
    _stub_native_session(
        monkeypatch, AgentSessionRef(kind="id", value="sid-1", agent="claude")
    )

    native = await transcript_discovery._native_session_transcript(
        "w2:t1", "/home/u/proj"
    )

    assert native == (transcript, "sid-1")


async def test_id_kind_glob_fallback_when_cwd_drifted(monkeypatch, tmp_path) -> None:
    projects = tmp_path / "projects"
    (projects / "-other-dir").mkdir(parents=True)
    transcript = projects / "-other-dir" / "sid-1.jsonl"
    transcript.write_text("{}\n")
    monkeypatch.setattr(config, "claude_projects_path", projects)
    _stub_native_session(
        monkeypatch, AgentSessionRef(kind="id", value="sid-1", agent="claude")
    )

    # cwd encodes to "-home-u-proj", which doesn't exist — the direct lookup
    # misses and discovery must fall back to the glob.
    native = await transcript_discovery._native_session_transcript(
        "w2:t1", "/home/u/proj"
    )

    assert native == (transcript, "sid-1")


async def test_missing_transcript_falls_through(monkeypatch, tmp_path) -> None:
    # Isolate the projects path too — otherwise the glob reads the REAL
    # ~/.claude/projects of the machine running the suite.
    monkeypatch.setattr(config, "claude_projects_path", tmp_path / "empty")
    _stub_native_session(
        monkeypatch, AgentSessionRef(kind="id", value="ghost", agent="claude")
    )

    assert (
        await transcript_discovery._native_session_transcript("w2:t1", "/proj") is None
    )


async def test_register_native_session_dedupes_recorded_path(
    monkeypatch, tmp_path
) -> None:
    calls: list = []
    # Guard BOTH write paths: a dedupe regression must not reach the real
    # file-locked write in a thread.
    _stub_session_map_sync(monkeypatch, calls)

    identity = _identity_with(transcript_path=tmp_path / "t.jsonl")  # Path, not str
    await transcript_discovery._register_native_session(
        "w2:t1", identity, (tmp_path / "t.jsonl", "sid"), cwd="/proj"
    )

    assert calls == []  # already recorded -> no duplicate write on either path


async def test_consult_first_beats_stale_hook_transcript(monkeypatch, tmp_path) -> None:
    """End-to-end consult-first order through discover_and_register_transcript:
    a stale hook transcript_path must lose to the native session, and the
    legacy fallback discovery must not run at all.
    """
    fresh = tmp_path / "fresh.jsonl"
    fresh.write_text("{}\n")
    _stub_native_session(
        monkeypatch, AgentSessionRef(kind="path", value=str(fresh), agent="pi")
    )

    stale = tmp_path / "stale.jsonl"  # never written — the hook's stale pointer
    identity = _identity_with(
        provider_name="claude", cwd="/proj", transcript_path=stale
    )
    mock_ws = MagicMock()
    mock_ws.window_states = {
        "w2:t1": MagicMock(
            provider_name=identity.provider_name,
            session_id=identity.session_id,
            cwd=identity.cwd,
            transcript_path=str(identity.transcript_path),
            window_name=identity.window_name,
            approval_mode=identity.approval_mode,
        )
    }
    monkeypatch.setattr(identity_state, "window_store", mock_ws)

    calls: list = []
    _stub_session_map_sync(monkeypatch, calls)

    fallback_spy = AsyncMock()
    monkeypatch.setattr(
        transcript_discovery, "_find_and_register_transcript", fallback_spy
    )

    await transcript_discovery.discover_and_register_transcript("w2:t1")

    assert len(calls) == 2  # register_hookless_session + write_hookless_session_map
    assert all(kw["transcript_path"] == str(fresh) for _name, kw in calls)
    fallback_spy.assert_not_awaited()
