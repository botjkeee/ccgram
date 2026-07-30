"""Benchmark the transcript-history read path with a synthetic Claude session.

This exercises ``SessionResolver.get_recent_messages`` exactly as the Mini App
and Telegram history handlers do, while replacing session lookup with a local,
deterministic transcript.  It never reads user data or contacts external
services.

Run from the repository root:

    uv run python scripts/benchmark_transcript_history.py
"""

from __future__ import annotations

import argparse
import asyncio
import json
import platform
import resource
import statistics
import tempfile
import time
from pathlib import Path
from unittest.mock import AsyncMock, patch

from ccgram.providers.claude import ClaudeProvider
from ccgram.session_resolver import ClaudeSession, SessionResolver


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--messages", type=int, default=5_000)
    parser.add_argument("--warmup", type=int, default=3)
    parser.add_argument("--samples", type=int, default=15)
    return parser.parse_args()


def percentile(samples: list[float], fraction: float) -> float:
    ordered = sorted(samples)
    index = min(len(ordered) - 1, int(len(ordered) * fraction))
    return ordered[index]


def transcript_line(index: int) -> str:
    role = "user" if index % 2 == 0 else "assistant"
    entry = {
        "type": role,
        "message": {
            "content": [
                {
                    "type": "text",
                    "text": f"synthetic operator message {index} " + "x" * 96,
                }
            ]
        },
        "timestamp": "2026-07-30T12:34:56.000Z",
        "sessionId": "benchmark-session",
        "cwd": "/tmp/ccgram-benchmark",
    }
    return json.dumps(entry, separators=(",", ":")) + "\n"


def write_transcript(path: Path, message_count: int) -> None:
    with path.open("w", encoding="utf-8") as handle:
        handle.writelines(transcript_line(index) for index in range(message_count))


async def read_once(
    resolver: SessionResolver, expected_count: int
) -> tuple[float, float]:
    before_cpu = resource.getrusage(resource.RUSAGE_SELF)
    started = time.perf_counter()
    messages, total = await resolver.get_recent_messages("@benchmark")
    wall_ms = (time.perf_counter() - started) * 1_000
    after_cpu = resource.getrusage(resource.RUSAGE_SELF)
    cpu_ms = (
        after_cpu.ru_utime
        + after_cpu.ru_stime
        - before_cpu.ru_utime
        - before_cpu.ru_stime
    ) * 1_000
    if total != expected_count or len(messages) != expected_count:
        raise RuntimeError(
            f"expected {expected_count} messages, got total={total}, "
            f"len={len(messages)}"
        )
    return wall_ms, cpu_ms


async def benchmark(args: argparse.Namespace) -> dict[str, object]:
    if args.messages < 1 or args.warmup < 0 or args.samples < 1:
        raise ValueError("messages/samples must be positive and warmup non-negative")

    with tempfile.TemporaryDirectory(prefix="ccgram-transcript-benchmark-") as temp:
        transcript = Path(temp) / "session.jsonl"
        write_transcript(transcript, args.messages)
        session = ClaudeSession(
            session_id="benchmark-session",
            summary="benchmark",
            message_count=args.messages,
            file_path=str(transcript),
        )
        resolver = SessionResolver()
        provider = ClaudeProvider()

        with (
            patch.object(
                resolver,
                "resolve_session_for_window",
                AsyncMock(return_value=session),
            ),
            patch(
                "ccgram.session_resolver.get_provider_for_window",
                return_value=provider,
            ),
            patch(
                "ccgram.session_resolver.identity_state.get_provider_name",
                return_value="claude",
            ),
        ):
            for _ in range(args.warmup):
                await read_once(resolver, args.messages)

            wall_samples: list[float] = []
            cpu_samples: list[float] = []
            for _ in range(args.samples):
                wall_ms, cpu_ms = await read_once(resolver, args.messages)
                wall_samples.append(wall_ms)
                cpu_samples.append(cpu_ms)

        return {
            "workload": {
                "provider": "claude",
                "messages": args.messages,
                "bytes": transcript.stat().st_size,
                "warmup": args.warmup,
                "samples": args.samples,
            },
            "wall_ms": {
                "p50": round(statistics.median(wall_samples), 2),
                "p95": round(percentile(wall_samples, 0.95), 2),
                "min": round(min(wall_samples), 2),
                "max": round(max(wall_samples), 2),
                "mean": round(statistics.mean(wall_samples), 2),
                "stdev": round(statistics.pstdev(wall_samples), 2),
            },
            "cpu_ms": {
                "p50": round(statistics.median(cpu_samples), 2),
                "p95": round(percentile(cpu_samples, 0.95), 2),
            },
            "environment": {
                "python": platform.python_version(),
                "platform": platform.platform(),
            },
        }


def main() -> None:
    args = parse_args()
    print(json.dumps(asyncio.run(benchmark(args)), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
