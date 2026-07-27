"""Unit tests for herdr_events.open_socket_stream against a real unix socket."""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path

import pytest

from ccgram.multiplexer.herdr_events import SUBSCRIBED, open_socket_stream

SUBS = [{"type": "tab.closed"}]


class _Server:
    """One-connection unix-socket server with a scripted response."""

    def __init__(self, lines: list[bytes], *, keep_open: bool = False) -> None:
        self.lines = lines
        self.keep_open = keep_open
        self.received: bytes = b""
        self.path = Path(tempfile.mkdtemp()) / "s.sock"  # short path: AF_UNIX limit
        self._stop = asyncio.Event()

    async def __aenter__(self) -> "_Server":
        async def handle(reader, writer):
            self.received = await reader.readline()
            for line in self.lines:
                writer.write(line)
            await writer.drain()
            if self.keep_open:
                # Cancellable wait: on Python 3.14 wait_closed() blocks until
                # handlers finish, so a bare sleep would stall teardown.
                await self._stop.wait()
            writer.close()

        self._srv = await asyncio.start_unix_server(handle, path=str(self.path))
        return self

    async def __aexit__(self, *exc) -> None:
        self._stop.set()
        self._srv.close()
        await self._srv.wait_closed()


# The REAL herdr 0.7.5 ack shape (verified in the herdr sources):
OK_ACK = (
    json.dumps(
        {"id": "ccgram-events", "result": {"type": "subscription_started"}}
    ).encode()
    + b"\n"
)


async def test_subscribe_request_framing_and_sentinel_order() -> None:
    ok_ack = OK_ACK
    event = (
        json.dumps({"event": "tab.closed", "data": {"tab_id": "w1:t1"}}).encode()
        + b"\n"
    )
    async with _Server([ok_ack, event]) as srv:
        got = [obj async for obj in open_socket_stream(str(srv.path), SUBS)]
    request = json.loads(srv.received)
    assert request == {
        "id": "ccgram-events",
        "method": "events.subscribe",
        "params": {"subscriptions": SUBS},
    }
    assert got[0] is SUBSCRIBED and got[1]["event"] == "tab.closed"


async def test_error_ack_raises_instead_of_yielding_sentinel() -> None:
    err_ack = (
        json.dumps(
            {"id": "ccgram-events", "error": {"code": "bad", "message": "unsupported"}}
        ).encode()
        + b"\n"
    )
    async with _Server([err_ack], keep_open=True) as srv:
        stream = open_socket_stream(str(srv.path), SUBS)
        with pytest.raises(OSError, match="rejected"):
            await anext(stream)


async def test_eof_before_ack_raises() -> None:
    async with _Server([]) as srv:
        stream = open_socket_stream(str(srv.path), SUBS)
        with pytest.raises(OSError):
            await anext(stream)


@pytest.mark.parametrize("ack", [b"{}\n", b"42\n", b'"ok"\n'])
async def test_wrong_shape_ack_raises(ack: bytes) -> None:
    # {} / scalars are not a subscription ack — must not read as success.
    async with _Server([ack], keep_open=True) as srv:
        stream = open_socket_stream(str(srv.path), SUBS)
        with pytest.raises(OSError, match="ack"):
            await anext(stream)


async def test_malformed_lines_skipped_and_eof_returns() -> None:
    async with _Server([OK_ACK, b"not json\n", b"\n"]) as srv:
        got = [obj async for obj in open_socket_stream(str(srv.path), SUBS)]
    assert got == [SUBSCRIBED]
