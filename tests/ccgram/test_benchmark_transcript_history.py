"""Regression tests for the transcript-history benchmark harness."""

import json
import os
import runpy
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Callable, cast

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
BENCHMARK_GLOBALS = runpy.run_path(
    str(REPOSITORY_ROOT / "scripts" / "benchmark_transcript_history.py")
)
percentile = cast(
    Callable[[list[float], float], float], BENCHMARK_GLOBALS["percentile"]
)


def test_percentile_interpolates_instead_of_selecting_the_maximum() -> None:
    assert percentile([1.0, 2.0, 3.0, 100.0], 0.95) == pytest.approx(85.45)
    assert percentile([42.0], 0.95) == 42.0


def test_benchmark_is_hermetic_and_emits_one_json_document() -> None:
    temporary_pattern = ".ccgram-transcript-benchmark-*"
    directories_before = set(REPOSITORY_ROOT.glob(temporary_pattern))
    with tempfile.TemporaryDirectory(
        prefix=".benchmark-host-environment-",
        dir=REPOSITORY_ROOT,
    ) as host_environment_root:
        environment = {
            "ALLOWED_USERS": "invalid-host-value",
            "CCGRAM_DIR": str(Path(host_environment_root) / "config"),
            "CCGRAM_LOG_LEVEL": "DEBUG",
            "HOME": str(Path(host_environment_root) / "home"),
            "PATH": os.environ.get("PATH", ""),
            "TELEGRAM_BOT_TOKEN": "host-token",
        }

        completed = subprocess.run(
            [
                sys.executable,
                "scripts/benchmark_transcript_history.py",
                "--messages",
                "8",
                "--warmup",
                "0",
                "--samples",
                "3",
            ],
            cwd=REPOSITORY_ROOT,
            env=environment,
            check=True,
            capture_output=True,
            text=True,
        )

    result = json.loads(completed.stdout)
    assert completed.stderr == ""
    assert result["workload"] == {
        "bytes": 2404,
        "messages": 8,
        "percentile_method": "linear (R-7)",
        "provider": "claude",
        "samples": 3,
        "warmup": 0,
    }
    assert result["wall_ms"].keys() == result["cpu_ms"].keys()
    assert len(result["wall_ms"]["samples"]) == 3
    assert len(result["cpu_ms"]["samples"]) == 3
    assert set(REPOSITORY_ROOT.glob(temporary_pattern)) == directories_before
