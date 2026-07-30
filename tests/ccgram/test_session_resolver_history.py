import asyncio
import json
from unittest.mock import AsyncMock

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
