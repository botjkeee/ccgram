"""Benchmark the transcript-history read path with a synthetic Claude session.

This exercises ``SessionResolver.get_recent_messages`` exactly as the Mini App
and Telegram history handlers do, while replacing session lookup with a local,
deterministic transcript.  It never reads user data or contacts external
services.

Run from the repository root:

    uv run python scripts/benchmark_transcript_history.py

The benchmark clears the inherited environment before importing ccgram and
creates its temporary config and transcript beneath the repository root.  Its
stdout is a single JSON document suitable for comparing repeated runs.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import platform
import resource
import statistics
import structlog
import sys
import tempfile
import time
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

if TYPE_CHECKING:
    from ccgram.session_resolver import SessionResolver


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--messages", type=int, default=5_000)
    parser.add_argument("--warmup", type=int, default=3)
    parser.add_argument("--samples", type=int, default=30)
    return parser.parse_args()


def percentile(samples: list[float], fraction: float) -> float:
    if not samples:
        raise ValueError("percentile requires at least one sample")
    if not 0 <= fraction <= 1:
        raise ValueError("percentile fraction must be between 0 and 1")

    ordered = sorted(samples)
    position = (len(ordered) - 1) * fraction
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def summarize(samples: list[float]) -> dict[str, object]:
    return {
        "samples": [round(sample, 2) for sample in samples],
        "p50": round(statistics.median(samples), 2),
        "p95": round(percentile(samples, 0.95), 2),
        "min": round(min(samples), 2),
        "max": round(max(samples), 2),
        "mean": round(statistics.mean(samples), 2),
        "stdev": round(statistics.pstdev(samples), 2),
    }


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
        "cwd": "/synthetic/ccgram-benchmark",
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


async def benchmark(args: argparse.Namespace, workspace: Path) -> dict[str, object]:
    if args.messages < 1 or args.warmup < 0 or args.samples < 1:
        raise ValueError("messages/samples must be positive and warmup non-negative")

    from ccgram.providers.claude import ClaudeProvider
    from ccgram.session_resolver import ClaudeSession, SessionResolver

    transcript = workspace / "session.jsonl"
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
            "percentile_method": "linear (R-7)",
        },
        "wall_ms": summarize(wall_samples),
        "cpu_ms": summarize(cpu_samples),
        "environment": {
            "python": platform.python_version(),
            "platform": platform.platform(),
        },
    }


def run(args: argparse.Namespace) -> dict[str, object]:
    repository_root = Path(__file__).resolve().parents[1]
    previous_cwd = Path.cwd()
    with tempfile.TemporaryDirectory(
        prefix=".ccgram-transcript-benchmark-",
        dir=repository_root,
    ) as temp:
        workspace = Path(temp)
        benchmark_environment = {
            "ALLOWED_USERS": "0",
            "CCGRAM_DIR": str(workspace / "config"),
            "TELEGRAM_BOT_TOKEN": "synthetic-benchmark-token",
        }
        with patch.dict(os.environ, benchmark_environment, clear=True):
            os.chdir(workspace)
            try:
                structlog.configure(
                    logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
                    wrapper_class=structlog.make_filtering_bound_logger(
                        logging.WARNING
                    ),
                )
                return asyncio.run(benchmark(args, workspace))
            finally:
                os.chdir(previous_cwd)


def main() -> None:
    print(json.dumps(run(parse_args()), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
