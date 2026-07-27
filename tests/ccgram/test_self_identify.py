"""Tests for the backend-neutral hook identity resolver (Task 6).

Table-driven over the four cases the design calls out: tmux env, herdr env,
neither, and nested-session rejection (the last exercised through
``hook._locate_primary_window`` since nested detection is provider-gated there).
"""

from __future__ import annotations

import pytest

from ccgram.multiplexer.self_identify import SelfIdentity, resolve_self_identity


def _fail_query(_pane_id: str):
    raise AssertionError("tmux_query must not run without $TMUX_PANE")


class TestResolveSelfIdentity:
    @pytest.mark.parametrize(
        ("env", "tmux_result", "herdr_query", "expected"),
        [
            (
                {"TMUX_PANE": "%0"},
                ("ccgram:@0", "@0", "project", "/dev/ttys012"),
                None,
                SelfIdentity(
                    "tmux", "ccgram:@0", "@0", "project", pane_tty="/dev/ttys012"
                ),
            ),
            # herdr: herdr_query resolves pane→tab; key and window_id use tab id
            (
                {"HERDR_PANE_ID": "w2:p1", "HERDR_SOCKET_PATH": "/tmp/herdr.sock"},
                None,
                lambda _pane: "w2:t1",
                SelfIdentity("herdr", "herdr:w2:t1", "w2:t1", ""),
            ),
            # herdr: no herdr_query → probe unavailable → None (symmetric with tmux)
            (
                {"HERDR_PANE_ID": "w2:p1", "HERDR_SOCKET_PATH": "/tmp/herdr.sock"},
                None,
                None,
                None,
            ),
            # herdr: herdr_query returns None (probe failure) → None (skip session_map write)
            (
                {"HERDR_PANE_ID": "w2:p1", "HERDR_SOCKET_PATH": "/tmp/herdr.sock"},
                None,
                lambda _pane: None,
                None,
            ),
            ({}, None, None, None),
            ({"TMUX_PANE": "%0"}, None, None, None),
        ],
        ids=[
            "tmux",
            "herdr-with-query",
            "herdr-no-query-fallback",
            "herdr-query-fail-fallback",
            "neither",
            "tmux-query-fail",
        ],
    )
    def test_resolution_table(self, env, tmux_result, herdr_query, expected) -> None:
        ident = resolve_self_identity(
            env,
            tmux_query=lambda _pane: tmux_result,
            herdr_query=herdr_query,
        )
        assert ident == expected

    def test_herdr_without_herdr_query_returns_none(self) -> None:
        # No herdr_query supplied → probe unavailable → None (skip session_map write).
        ident = resolve_self_identity(
            {"HERDR_PANE_ID": "w0:p0"}, tmux_query=_fail_query
        )
        assert ident is None

    def test_herdr_query_resolves_tab_id(self) -> None:
        ident = resolve_self_identity(
            {"HERDR_PANE_ID": "w0:p0"},
            tmux_query=_fail_query,
            herdr_query=lambda _pane: "w0:t1",
        )
        assert ident == SelfIdentity("herdr", "herdr:w0:t1", "w0:t1", "")

    def test_tmux_wins_when_both_present(self) -> None:
        # A herdr pane nested inside a tmux pane reports the outer tmux identity.
        env = {"TMUX_PANE": "%1", "HERDR_PANE_ID": "w2:p1"}
        ident = resolve_self_identity(
            env, tmux_query=lambda _pane: ("s:@1", "@1", "win", "/dev/ttys1")
        )
        assert ident is not None and ident.mux == "tmux"

    def test_neither_env_does_not_probe_tmux(self) -> None:
        assert resolve_self_identity({}, tmux_query=_fail_query) is None


def _tmux_ok(pane: str):
    return ("ccgram:@5", "@5", "app", "/dev/pts/3")


def test_stale_tmux_pane_falls_through_to_herdr() -> None:
    env = {"TMUX_PANE": "%1", "HERDR_PANE_ID": "w2:p1"}
    identity = resolve_self_identity(
        env,
        tmux_query=_tmux_ok,
        herdr_query=lambda p: "w2:t1",
        process_tty="/dev/pts/9",  # agent's tty != resolved pane's tty → stale
    )
    assert identity is not None and identity.mux == "herdr"
    assert identity.session_window_key == "herdr:w2:t1"


def test_stale_tmux_without_herdr_keeps_tmux_identity() -> None:
    # A stale-looking resolution must never DROP the hook on tmux-only setups.
    identity = resolve_self_identity(
        {"TMUX_PANE": "%1"},
        tmux_query=_tmux_ok,
        process_tty="/dev/pts/9",
    )
    assert identity is not None and identity.mux == "tmux"


def test_stale_tmux_with_failing_herdr_query_keeps_tmux_identity() -> None:
    env = {"TMUX_PANE": "%1", "HERDR_PANE_ID": "w2:p1"}
    identity = resolve_self_identity(
        env,
        tmux_query=_tmux_ok,
        herdr_query=lambda p: None,
        process_tty="/dev/pts/9",
    )
    assert identity is not None and identity.mux == "tmux"


def test_fresh_tmux_pane_still_wins() -> None:
    env = {"TMUX_PANE": "%1", "HERDR_PANE_ID": "w2:p1"}
    identity = resolve_self_identity(
        env,
        tmux_query=_tmux_ok,
        herdr_query=lambda p: "w2:t1",
        process_tty="/dev/pts/3",
    )
    assert identity is not None and identity.mux == "tmux"


def test_no_tty_evidence_keeps_tmux_priority() -> None:
    env = {"TMUX_PANE": "%1", "HERDR_PANE_ID": "w2:p1"}
    identity = resolve_self_identity(
        env,
        tmux_query=_tmux_ok,
        herdr_query=lambda p: "w2:t1",
        process_tty="",
    )
    assert identity is not None and identity.mux == "tmux"


def test_failed_tmux_probe_falls_through_to_herdr() -> None:
    env = {"TMUX_PANE": "%1", "HERDR_PANE_ID": "w2:p1"}
    identity = resolve_self_identity(
        env,
        tmux_query=lambda p: None,
        herdr_query=lambda p: "w2:t1",
    )
    assert identity is not None and identity.mux == "herdr"


def test_failed_tmux_probe_without_herdr_returns_none() -> None:
    identity = resolve_self_identity(
        {"TMUX_PANE": "%1"},
        tmux_query=lambda p: None,
    )
    assert identity is None


class TestLocatePrimaryWindowThroughResolver:
    """`_locate_primary_window` routes through the resolver and keeps the
    tmux nested-session guard intact."""

    def test_primary_tmux_claude_accepted(self, monkeypatch) -> None:
        monkeypatch.setenv("TMUX_PANE", "%0")
        # No herdr pane in this env: a stale-looking tty comparison (this test
        # process's real ancestor tty vs. the mocked pane_tty) must never fall
        # through to herdr when there is nothing to fall through to.
        monkeypatch.delenv("HERDR_PANE_ID", raising=False)
        monkeypatch.setattr(
            "ccgram.hook._resolve_window_id",
            lambda _pane: ("ccgram:@0", "@0", "project", "/dev/ttys012"),
        )
        monkeypatch.setattr("ccgram.hook._is_nested_session", lambda _tty: False)
        from ccgram.hook import _locate_primary_window

        assert _locate_primary_window("sid", "Stop", "claude") == (
            "ccgram:@0",
            "@0",
            "project",
        )

    def test_nested_tmux_claude_rejected(self, monkeypatch) -> None:
        monkeypatch.setenv("TMUX_PANE", "%0")
        monkeypatch.delenv("HERDR_PANE_ID", raising=False)
        monkeypatch.setattr(
            "ccgram.hook._resolve_window_id",
            lambda _pane: ("ccgram:@0", "@0", "project", "/dev/ttys012"),
        )
        monkeypatch.setattr("ccgram.hook._is_nested_session", lambda _tty: True)
        from ccgram.hook import _locate_primary_window

        assert _locate_primary_window("sid", "Stop", "claude") is None

    def test_no_env_returns_none(self, monkeypatch) -> None:
        monkeypatch.delenv("TMUX_PANE", raising=False)
        monkeypatch.delenv("HERDR_PANE_ID", raising=False)
        from ccgram.hook import _locate_primary_window

        assert _locate_primary_window("sid", "Stop", "claude") is None

    def test_herdr_pane_resolves_to_tab_id(self, monkeypatch) -> None:
        # herdr_query maps pane→tab; session_window_key and window_id use tab id.
        monkeypatch.delenv("TMUX_PANE", raising=False)
        monkeypatch.setenv("HERDR_PANE_ID", "w2:p1")
        monkeypatch.setattr(
            "ccgram.hook._resolve_herdr_tab_id",
            lambda _pane: "w2:t1",
        )
        from ccgram.hook import _locate_primary_window

        assert _locate_primary_window("sid", "Stop", "claude") == (
            "herdr:w2:t1",
            "w2:t1",
            "",
        )

    def test_herdr_pane_probe_failure_returns_none(self, monkeypatch) -> None:
        # probe returns None → resolve_self_identity returns None → hook skips write.
        monkeypatch.delenv("TMUX_PANE", raising=False)
        monkeypatch.setenv("HERDR_PANE_ID", "w2:p1")
        monkeypatch.setattr(
            "ccgram.hook._resolve_herdr_tab_id",
            lambda _pane: None,
        )
        from ccgram.hook import _locate_primary_window

        assert _locate_primary_window("sid", "Stop", "claude") is None
