import asyncio
import json
import threading
from unittest.mock import AsyncMock

import pytest

import ccgram.session_resolver as session_resolver_module
from ccgram.providers.claude import ClaudeProvider
from ccgram.session_resolver import ClaudeSession, SessionResolver


def _entry(role: str, text: str) -> str:
    return (
        json.dumps(
            {
                "type": role,
                "message": {"content": [{"type": "text", "text": text}]},
                "timestamp": "2026-07-30T12:34:56.000Z",
            }
        )
        + "\n"
    )


async def test_get_recent_messages_uses_one_worker_call(tmp_path, monkeypatch) -> None:
    transcript = tmp_path / "session.jsonl"
    transcript.write_text(
        "".join(
            _entry("user" if index % 2 == 0 else "assistant", f"message {index}")
            for index in range(500)
        ),
        encoding="utf-8",
    )
    session = ClaudeSession("session-id", "summary", 500, str(transcript))
    resolver = SessionResolver()
    monkeypatch.setattr(
        resolver,
        "resolve_session_for_window",
        AsyncMock(return_value=session),
    )
    monkeypatch.setattr(
        "ccgram.session_resolver.get_provider_for_window",
        lambda _window_id, provider_name=None: ClaudeProvider(),
    )
    monkeypatch.setattr(
        "ccgram.session_resolver.identity_state.get_provider_name",
        lambda _window_id: "claude",
    )

    original_to_thread = asyncio.to_thread
    calls = 0

    async def counting_to_thread(function, /, *args, **kwargs):
        nonlocal calls
        calls += 1
        return await original_to_thread(function, *args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", counting_to_thread)

    messages, total = await resolver.get_recent_messages("@1")

    assert total == 500
    assert [message["text"] for message in messages[:2]] == [
        "message 0",
        "message 1",
    ]
    assert calls == 1


async def test_get_recent_messages_preserves_byte_ranges(tmp_path, monkeypatch) -> None:
    lines = [
        _entry("user", "first — unicode"),
        _entry("assistant", "second"),
        _entry("user", "third"),
    ]
    transcript = tmp_path / "session.jsonl"
    transcript.write_text("".join(lines), encoding="utf-8")
    session = ClaudeSession("session-id", "summary", 3, str(transcript))
    resolver = SessionResolver()
    monkeypatch.setattr(
        resolver,
        "resolve_session_for_window",
        AsyncMock(return_value=session),
    )
    monkeypatch.setattr(
        "ccgram.session_resolver.get_provider_for_window",
        lambda _window_id, provider_name=None: ClaudeProvider(),
    )
    monkeypatch.setattr(
        "ccgram.session_resolver.identity_state.get_provider_name",
        lambda _window_id: "claude",
    )

    start_byte = len(lines[0].encode())
    end_byte = start_byte + len(lines[1].encode())
    messages, total = await resolver.get_recent_messages(
        "@1",
        start_byte=start_byte,
        end_byte=end_byte,
    )

    assert total == 1
    assert [message["text"] for message in messages] == ["second"]


async def test_get_recent_messages_stops_worker_after_cancellation(
    tmp_path, monkeypatch
) -> None:
    transcript = tmp_path / "session.jsonl"
    transcript.write_text(
        "".join(_entry("user", f"message {index}") for index in range(100)),
        encoding="utf-8",
    )
    session = ClaudeSession("session-id", "summary", 100, str(transcript))
    resolver = SessionResolver()
    provider = ClaudeProvider()
    first_parse_started = threading.Event()
    finish_first_parse = threading.Event()
    worker_finished = threading.Event()
    original_parse = provider.parse_transcript_line
    original_read = session_resolver_module._read_transcript_entries
    calls = 0

    def tracked_read(*args, **kwargs):
        try:
            return original_read(*args, **kwargs)
        finally:
            worker_finished.set()

    def blocking_parse(line: str):
        nonlocal calls
        calls += 1
        if calls == 1:
            first_parse_started.set()
            finish_first_parse.wait(timeout=1)
        return original_parse(line)

    monkeypatch.setattr(
        resolver,
        "resolve_session_for_window",
        AsyncMock(return_value=session),
    )
    monkeypatch.setattr(
        "ccgram.session_resolver.get_provider_for_window",
        lambda _window_id, provider_name=None: provider,
    )
    monkeypatch.setattr(
        "ccgram.session_resolver.identity_state.get_provider_name",
        lambda _window_id: "claude",
    )
    monkeypatch.setattr(provider, "parse_transcript_line", blocking_parse)
    monkeypatch.setattr(
        session_resolver_module,
        "_read_transcript_entries",
        tracked_read,
    )

    task = asyncio.create_task(resolver.get_recent_messages("@1"))
    assert await asyncio.to_thread(first_parse_started.wait, 1)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    finish_first_parse.set()
    assert await asyncio.to_thread(worker_finished.wait, 1)

    assert calls == 1
