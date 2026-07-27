"""Provider-owned resolution of a session id to its transcript file.

The multiplexer (herdr) reports which session is live as a bare id; each
provider owns the storage layout that maps that id to a file. Claude files
transcripts under ``~/.claude/projects/<cwd-with-dashes>/<id>.jsonl``, Codex
under ``~/.codex/sessions/YYYY/MM/DD/<name>-<ts>-<id>.jsonl`` — so the id is a
filename *suffix* there, under an unknown date directory.
"""

from pathlib import Path

import pytest

from ccgram.config import config
from ccgram.providers.claude import ClaudeProvider
from ccgram.providers.codex import CodexProvider
from ccgram.providers.gemini import GeminiProvider
from ccgram.providers.pi import PiProvider
from ccgram.providers.shell import ShellProvider


def test_codex_resolves_rollout_file_by_session_id(monkeypatch, tmp_path) -> None:
    day = tmp_path / ".codex" / "sessions" / "2026" / "07" / "27"
    day.mkdir(parents=True)
    sid = "019fa2c7-db03-73b1-ad98-2fc2a41acf8d"
    rollout = day / f"rollout-2026-07-27T08-53-54-{sid}.jsonl"
    rollout.write_text("{}\n")
    # A sibling session in the same day directory must not be picked up.
    other = "019fa2c6-2006-7661-88a7-21fec1cb7198"
    (day / f"rollout-2026-07-27T08-52-01-{other}.jsonl").write_text("{}\n")
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))

    resolved = CodexProvider().transcript_for_session_id(sid, "/home/u/proj")

    assert resolved == str(rollout)


def test_codex_ignores_the_cwd_hint(monkeypatch, tmp_path) -> None:
    """Codex ids are globally unique; a drifted cwd must not lose the file."""
    day = tmp_path / ".codex" / "sessions" / "2026" / "07" / "27"
    day.mkdir(parents=True)
    sid = "019fa2c7-db03-73b1-ad98-2fc2a41acf8d"
    rollout = day / f"rollout-2026-07-27T08-53-54-{sid}.jsonl"
    rollout.write_text("{}\n")
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))

    assert CodexProvider().transcript_for_session_id(sid, "") == str(rollout)


def test_codex_unknown_session_id_returns_none(monkeypatch, tmp_path) -> None:
    (tmp_path / ".codex" / "sessions").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))

    assert CodexProvider().transcript_for_session_id("ghost", "/x") is None


def test_codex_missing_sessions_dir_returns_none(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))

    assert CodexProvider().transcript_for_session_id("sid-1", "/x") is None


def test_claude_resolves_via_cwd_encoding(monkeypatch, tmp_path) -> None:
    projects = tmp_path / "projects"
    (projects / "-home-u-proj").mkdir(parents=True)
    transcript = projects / "-home-u-proj" / "sid-1.jsonl"
    transcript.write_text("{}\n")
    monkeypatch.setattr(config, "claude_projects_path", projects)

    resolved = ClaudeProvider().transcript_for_session_id("sid-1", "/home/u/proj")

    assert resolved == str(transcript)


def test_claude_glob_fallback_when_cwd_drifted(monkeypatch, tmp_path) -> None:
    projects = tmp_path / "projects"
    (projects / "-other-dir").mkdir(parents=True)
    transcript = projects / "-other-dir" / "sid-1.jsonl"
    transcript.write_text("{}\n")
    monkeypatch.setattr(config, "claude_projects_path", projects)

    resolved = ClaudeProvider().transcript_for_session_id("sid-1", "/home/u/proj")

    assert resolved == str(transcript)


def test_claude_unknown_session_id_returns_none(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(config, "claude_projects_path", tmp_path / "empty")

    assert ClaudeProvider().transcript_for_session_id("ghost", "/home/u/proj") is None


@pytest.mark.parametrize("provider_cls", [GeminiProvider, PiProvider, ShellProvider])
def test_providers_without_id_addressing_return_none(provider_cls) -> None:
    """Pi reports a full path instead; gemini and shell have no id lookup."""
    assert provider_cls().transcript_for_session_id("sid-1", "/x") is None


@pytest.mark.parametrize(
    "provider_cls", [ClaudeProvider, CodexProvider, GeminiProvider, PiProvider]
)
def test_empty_session_id_returns_none(provider_cls) -> None:
    assert provider_cls().transcript_for_session_id("", "/x") is None
