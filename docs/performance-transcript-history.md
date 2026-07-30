# Transcript history performance

## Result

Reading a 5,000-message Claude transcript through
`SessionResolver.get_recent_messages()` is about 93% faster than the merged
baseline at `984b9a7`. The workload is the same path used by Telegram history,
`/last`, and the Mini App transcript endpoint; only session discovery is mocked
to keep the benchmark local and deterministic.

The measured candidate was `4291e50`. Later changes in this performance series
only add tests and this report; they do not change the measured production path.

| Metric | Baseline | Candidate | Reduction |
| --- | ---: | ---: | ---: |
| Wall p50 | 780.54 ms | 48.14 ms | 93.83% |
| Wall p95 | 863.05 ms | 62.34 ms | 92.78% |
| CPU p50 | 737.80 ms | 47.82 ms | 93.52% |
| CPU p95 | 778.67 ms | 62.34 ms | 91.99% |

Each table value is the median of three independent benchmark runs. Each run
used 3 warmups and 30 measured samples. The fixture contains 5,000 messages and
1,516,390 bytes. Percentiles use linear interpolation (R-7).

## Reproduction

Run the candidate benchmark from the repository root:

```sh
uv run python scripts/benchmark_transcript_history.py
```

To run the identical benchmark against the exact merged baseline without
switching branches or creating another worktree, extract its `src` tree into
this repository:

```sh
test ! -e .baseline-transcript-history-984b9a7
mkdir .baseline-transcript-history-984b9a7
git archive 984b9a7 src | tar -x -C .baseline-transcript-history-984b9a7
PYTHONPATH="$PWD/.baseline-transcript-history-984b9a7/src" \
  uv run python scripts/benchmark_transcript_history.py
rm -r -- .baseline-transcript-history-984b9a7
```

Repeat each command three times, alternating candidate and baseline runs to
limit machine-load drift. Standard output is one JSON document containing all
30 wall and CPU samples, p50, p95, min, max, mean, and population standard
deviation. The benchmark clears inherited configuration, uses no user
transcripts or network services, and removes its temporary directory.

The reported measurements were collected on CPython 3.14.4,
Linux 7.0.0-1008-gcp x86_64 with glibc 2.43:

| Run | Version | Wall p50 / p95 (ms) | Wall stdev | CPU p50 / p95 (ms) | CPU stdev |
| ---: | --- | ---: | ---: | ---: | ---: |
| 1 | candidate | 48.76 / 58.14 | 6.69 | 48.75 / 58.16 | 6.72 |
| 1 | baseline | 780.54 / 834.57 | 37.96 | 737.80 / 778.67 | 28.10 |
| 2 | candidate | 48.14 / 64.38 | 7.13 | 47.82 / 63.97 | 7.01 |
| 2 | baseline | 794.33 / 875.85 | 79.30 | 760.34 / 816.14 | 49.71 |
| 3 | candidate | 47.16 / 62.34 | 6.93 | 47.15 / 62.34 | 6.93 |
| 3 | baseline | 737.82 / 863.05 | 55.05 | 710.40 / 770.49 | 36.40 |

## Cause and regression detection

The baseline used `aiofiles` for `tell()` and `readline()` inside the loop.
Those operations each cross the event-loop executor boundary, so the cost grew
with transcript line count. A profile of 20,000 messages recorded 20,003
executor calls. The correction performs the same seek, range checks, line
parsing, ordering, and provider parsing inside one `asyncio.to_thread()` call.

The regression test below checks that causal invariant without a timing
threshold, so normal scheduler variance cannot make it flaky:

```sh
uv run pytest -q \
  tests/ccgram/test_session_resolver_history.py::test_get_recent_messages_uses_one_worker_call
```

Byte-range behavior is covered separately at exact, empty, partial-line, and
past-EOF boundaries, including UTF-8 content. Cancellation is also covered:
the worker stops after its current line when its awaiting request is cancelled.

## Repository check limitation

The focused tests, unit and integration suites, type checking, dependency
checking, formatting, and build pass for this change. The repository-wide Ruff
command still reports the pre-existing `C901` complexity 11 in
`HerdrManager.watch_events`; the same finding is present at baseline `984b9a7`.
That unrelated code is intentionally not changed as part of this performance
correction.
