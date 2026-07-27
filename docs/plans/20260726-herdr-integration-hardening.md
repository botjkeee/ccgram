# Herdr Integration Hardening — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix the 15 verified defects in ccgram's Herdr multiplexer integration found by the 2026-07-26 adversarial review: destructive reconciliation driven by a discovery filter, listing failures masked as "no windows", untranslated key tokens, broken agent-exit detection, stale push-status cache, dead event subscriptions, and the matching test-coverage gaps.

**Architecture:** The core move is separating two listing consumers that today share one filtered surface: `list_windows_for_reconciliation()` becomes the unfiltered liveness truth (safe for destructive prune), while discovery keeps the internal-label filter via a new `WindowRef.internal` flag. Everything else is point fixes inside the existing multiplexer seam — no new modules, no contract-breaking changes (one additive dataclass field, one additive optional parameter).

**Tech Stack:** Python 3.14, uv, pytest (asyncio_mode=auto), ruff, pyright. Herdr 0.7.5 (protocol 17) live at `$HERDR_SOCKET_PATH` on this machine.

> **Revision 2 (2026-07-26):** reviewed by Codex against the codebase and the herdr 0.7.5 sources; 12 of its 13 findings survived independent adversarial verification and are folded in below. The 13th (reorder Tasks 3/4) was refuted — see Out of scope.

## Global Constraints

- Repo: `/home/ffs/projects/ccgram`. Work on a feature branch off `main`.
- Unit gate per task: `make test` (runs `pytest -m "not integration and not e2e"`). Full gate at the end: `make check`.
- Live herdr contract tests (`-m herdr`) create and close scratch tabs in the user's LIVE herdr session. Run them only where a task's verify step says so: `uv run pytest tests/integration/test_herdr_contract.py -m herdr -v`.
- Conventional commits (`fix(herdr): …`, `test(herdr): …`). NEVER add an agent name as co-author.
- F1 boundary: nothing outside `multiplexer/**`, `bootstrap.py`, `main.py` may import a concrete backend. All fixes below respect this; new consumer logic gates on `capabilities`, never on backend name.
- In-function imports need a `# Lazy:` comment (enforced by `make lint` → `scripts/lint_lazy_imports.py`).
- Match surrounding docstring/comment style; herdr JSON shapes stay private to `multiplexer/herdr.py`.

## Background: the two systemic defects (read before Task 1)

**Filter placement.** `_INTERNAL_LABEL_RE = ^(__.*__|fm-.*)$` (herdr.py:102) is applied inside `list_windows_for_reconciliation()` (herdr.py:459-462). But that listing is the *destructive-cleanup truth*: `session_monitor._monitor_loop` (session_monitor.py:432-439) computes `live_window_ids` from it and calls `prune_session_map()`, which deletes session_map entries AND persisted `WindowState`s for every absent id (session_map.py:432-474). So a bound tab whose workspace or tab label matches `fm-*`/`__*__` (repo named `fm-app`, worktree branch slug `fm-…`, a user rename) is pruned as dead within one 2s cycle: transcript monitoring dies while send still works. Startup `resolve_stale_ids` (session.py:212) uses the same listing and can't recover the binding. Meanwhile the hook-driven adoption path (`_detect_and_cleanup_changes`) has NO label check at all, so fm-* crewmate tabs running Claude still auto-create Telegram topics — the exact noise bb0c530 meant to stop — and then get pruned again, in a loop.

**Failure masked as empty.** `list_windows()` degrades every failure to `[]` (herdr.py:417-419). The 1s status poll builds its lookup from it (polling_coordinator.py:99), and `tick_window` treats a missing window as death (window_tick/__init__.py:77-81) → one CLI timeout mass-fires "Session ended" banners + recovery keyboards in every bound topic and permanently freezes their status via `is_dead_notified`. Two variants of the same class: `_tab_list` returns `[]` (not `None`) when the JSON shape drifts (herdr.py:302-307), and `_workspace_labels` returns `{}` both for "no workspaces" and "call failed" (herdr.py:319-332).

---

### Task 1: `WindowRef.internal` — unfiltered reconciliation, filtered discovery

**Files:**
- Modify: `src/ccgram/multiplexer/base.py:24-37` (WindowRef)
- Modify: `src/ccgram/multiplexer/herdr.py:417-470` (`list_windows`, `list_windows_for_reconciliation`)
- Test: `tests/ccgram/test_herdr_backend.py`

**Interfaces:**
- Produces: `WindowRef.internal: bool = False` — True for backend-internal windows (self-hosting `__*__` and FirstMate-crewmate `fm-*` labels). Liveness consumers (prune, status tick, resolve_stale_ids) MUST still count internal windows; discovery consumers MUST skip them. tmux never sets it.
- Produces: `HerdrManager.list_windows_for_reconciliation()` now returns **all** tabs (internal ones marked); `HerdrManager.list_windows()` returns only non-internal refs. `find_window_by_id` is unchanged (already bypasses the filter).

- [ ] **Step 1: Write the failing tests** (append to `tests/ccgram/test_herdr_backend.py`, reuse `FakeHerdr`/`_manager` and the fixture style of `test_list_windows_filters_internal_workspace_label`)

```python
def _labelled_listing(tab_label: str, ws_label: str) -> FakeHerdr:
    tab_list = json.dumps(
        {
            "result": {
                "tabs": [
                    {"label": "app", "tab_id": "w1:t1", "workspace_id": "w1", "cwd": "/a"},
                    {"label": tab_label, "tab_id": "w2:t1", "workspace_id": "w2", "cwd": "/b"},
                ],
                "type": "tab_list",
            }
        }
    )
    ws_list = json.dumps(
        {
            "result": {
                "workspaces": [
                    {"workspace_id": "w1", "label": "myproject", "cwd": "/a"},
                    {"workspace_id": "w2", "label": ws_label, "cwd": "/b"},
                ],
                "type": "workspace_list",
            }
        }
    )
    empty_panes = json.dumps({"result": {"panes": [], "type": "pane_list"}})
    return (
        FakeHerdr()
        .on("tab", "list", out=tab_list)
        .on("pane", "list", out=empty_panes)
        .on("workspace", "list", out=ws_list)
    )


async def test_reconciliation_listing_includes_internal_tabs_marked() -> None:
    # fm-* tab label: present in the reconciliation listing, marked internal.
    fake = _labelled_listing(tab_label="fm-task-42", ws_label="crew")
    refs = await _manager(fake).list_windows_for_reconciliation()
    assert refs is not None
    by_id = {r.window_id: r for r in refs}
    assert "w2:t1" in by_id, "internal tab must stay visible to liveness consumers"
    assert by_id["w2:t1"].internal is True
    assert by_id["w1:t1"].internal is False


async def test_reconciliation_listing_marks_internal_workspace_label() -> None:
    fake = _labelled_listing(tab_label="agent", ws_label="fm-crew")
    refs = await _manager(fake).list_windows_for_reconciliation()
    assert refs is not None
    assert {r.window_id: r.internal for r in refs} == {"w1:t1": False, "w2:t1": True}


async def test_list_windows_still_filters_fm_tabs() -> None:
    # Discovery surface keeps today's behavior: fm-*/__*__ never surface.
    fake = _labelled_listing(tab_label="fm-task-42", ws_label="crew")
    wins = await _manager(fake).list_windows()
    assert {w.window_id for w in wins} == {"w1:t1"}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/ccgram/test_herdr_backend.py -k "internal_tabs_marked or internal_workspace_label or filters_fm_tabs" -v`
Expected: FAIL — `WindowRef` has no field `internal` / `w2:t1` missing from reconciliation listing.

- [ ] **Step 3: Implement**

In `base.py` add the field with its contract line:

```python
@dataclass
class WindowRef:
    """Neutral representation of a multiplexer window (tmux window / herdr pane).

    Field names match the existing ``TmuxWindow`` fields so Task 2 call-site
    migration is mechanical.

    ``internal`` marks backend-internal windows (herdr self-hosting ``__*__``
    and FirstMate-crewmate ``fm-*`` labels): discovery/topic surfaces must skip
    them, but liveness consumers (prune, status tick, startup re-resolution)
    must still count them — an internal window is alive, just not a topic.
    """

    window_id: str
    window_name: str
    cwd: str
    pane_current_command: str = ""
    pane_tty: str = ""
    pane_width: int = 0
    pane_height: int = 0
    internal: bool = False
```

In `herdr.py` replace the skip with a mark (docstrings updated accordingly):

```python
    async def list_windows(self) -> list[WindowRef]:
        """List discovery-eligible windows (internal labels filtered).

        Degrades an unavailable herdr server to an empty list — user-facing
        best-effort surface. Destructive consumers must use
        ``list_windows_for_reconciliation`` instead.
        """
        refs = await self.list_windows_for_reconciliation()
        return [r for r in refs or [] if not r.internal]
```

and in `list_windows_for_reconciliation`, replace lines 458-462:

```python
            # Mark __*__ / fm-* workspace or tab labels as internal instead of
            # skipping: this listing is the liveness truth for destructive
            # prune, so internal tabs must stay visible here. ``list_windows``
            # applies the actual discovery filter.
            internal = bool(
                _INTERNAL_LABEL_RE.match(workspace_label)
                or _INTERNAL_LABEL_RE.match(tab_label)
            )
```

and pass it through `_to_window_ref` — extend the helper with an `internal: bool = False` keyword parameter that forwards to `WindowRef(..., internal=internal)`.

- [ ] **Step 4: Run the herdr backend suite**

Run: `uv run pytest tests/ccgram/test_herdr_backend.py tests/ccgram/test_multiplexer_contract.py tests/ccgram/test_multiplexer_base.py -v`
Expected: PASS (existing `test_list_windows_filters_internal_*` keep passing — they call `list_windows`; `test_reconciliation_listing_returns_none_on_tab_list_failure` unchanged).

- [ ] **Step 5: Commit**

```bash
git add src/ccgram/multiplexer/base.py src/ccgram/multiplexer/herdr.py tests/ccgram/test_herdr_backend.py
git commit -m "fix(herdr): keep internal tabs in reconciliation listing, filter only discovery"
```

---

### Task 2: session_monitor — prune on all windows, discover on non-internal, gate hook adoption

**Files:**
- Modify: `src/ccgram/session_monitor.py:279-326` (`_detect_and_cleanup_changes`), `:410-446` (`_monitor_loop`)
- Test: `tests/ccgram/test_session_monitor.py`

**Interfaces:**
- Consumes: `WindowRef.internal` from Task 1.
- Produces: `_detect_and_cleanup_changes(raw=None, discovery_windows: list | None = None)` — when `discovery_windows` is a list, hook-driven adoption only fires for window ids present in it; `[]` blocks adoption for the cycle; `None` keeps legacy behavior (existing tests/other callers unchanged).

- [ ] **Step 1: Write the failing tests** (in `tests/ccgram/test_session_monitor.py`, following its existing fixture idioms for `SessionMonitor` and `_new_window_callback` capture; read the file first and reuse its helpers)

Two behaviors to pin:

```python
async def test_adoption_skips_windows_absent_from_discovery(monkeypatch) -> None:
    """A session_map entry for an internal (fm-*) tab must not create a topic."""
    monitor = SessionMonitor(state_file=...)  # per existing fixture idiom
    events: list = []

    async def capture(event):
        events.append(event)

    monitor.set_new_window_callback(capture)
    session_lifecycle.initialize({})
    # NB: _load_current_session_map strips the "herdr:" prefix (parse_session_map),
    # so current_map keys are BARE tab ids — the discovery gate compares them
    # to WindowRef.window_id.
    current = {
        "w9:t1": {"session_id": "sid-1", "cwd": "/x", "window_name": ""},
    }
    monkeypatch.setattr(
        monitor, "_load_current_session_map", AsyncMock(return_value=current)
    )
    # Window exists (hook wrote the entry) but is NOT in the discovery listing.
    await monitor._detect_and_cleanup_changes(discovery_windows=[])
    assert events == []


async def test_adoption_fires_for_discovered_windows(monkeypatch) -> None:
    """Same entry adopts normally when its window is discovery-eligible."""
    ...  # mirror of the test above with
    # discovery_windows=[WindowRef(window_id="w9:t1", window_name="app", cwd="/x")]
    # assert len(events) == 1 and events[0].window_id == "w9:t1"
```

And the monitor-loop wiring test: internal windows count as live for prune but not for discovery — pin by asserting `prune_session_map` receives the full id set while `_emit_unbound_window_events` receives only non-internal refs (monkeypatch both, drive one loop iteration or extract the reconciliation block if the file's tests already do so).

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/ccgram/test_session_monitor.py -k adoption -v`
Expected: FAIL — `_detect_and_cleanup_changes` doesn't accept `discovery_windows` / adoption event still fires.

- [ ] **Step 3: Implement**

In `_detect_and_cleanup_changes`, extend the signature and gate `adoption_windows` right after it is assembled (after line 299):

```python
    async def _detect_and_cleanup_changes(
        self,
        raw: dict | None = None,
        discovery_windows: list | None = None,
    ) -> dict[str, dict[str, str]]:
        """Reconcile session_map; clean up replaced/removed sessions; fire new-window events.

        ``discovery_windows`` (the non-internal live listing) gates hook-driven
        adoption: a session_map entry whose window is not discovery-eligible —
        internal ``fm-*``/``__*__`` labels, or not (yet) visible in the listing —
        must not create a topic. ``None`` skips the gate (legacy callers);
        ``[]`` blocks adoption for this cycle (listing unavailable). A window
        skipped only because the listing lagged one cycle is retried by
        ``_emit_known_unbound_window_events`` on the next poll.
        """
```

```python
        if discovery_windows is not None:
            discovery_ids = {w.window_id for w in discovery_windows}
            adoption_windows = {
                wid: details
                for wid, details in adoption_windows.items()
                if wid in discovery_ids
            }
```

In `_monitor_loop`, move the listing before the reconcile call and split the two views (replaces lines 430-446):

```python
                all_windows = await list_windows_for_reconciliation(tmux_manager)
                discovery_windows = (
                    None
                    if all_windows is None
                    else [w for w in all_windows if not w.internal]
                )
                current_map = await self._detect_and_cleanup_changes(
                    raw_session_map,
                    discovery_windows=[] if all_windows is None else discovery_windows,
                )

                if all_windows is None:
                    logger.warning(
                        "Multiplexer listing unavailable; skipping window reconciliation"
                    )
                else:
                    live_window_ids = {w.window_id for w in all_windows}
                    session_map_sync.prune_session_map(live_window_ids)
                    known_window_ids = set(current_map.keys())
                    discovery_ids = {w.window_id for w in discovery_windows}
                    await self._emit_unbound_window_events(
                        discovery_windows, known_window_ids
                    )
                    await self._emit_known_unbound_window_events(
                        current_map, discovery_ids
                    )
```

`session.py resolve_stale_ids` needs NO code change: the Task 1 listing change already makes it see internal windows for prune and re-resolution, and `sync_display_names` is safe on the FULL list — it only updates pre-existing entries, so internal windows can never gain display names, while a bound tab renamed to `fm-*` keeps receiving rename-sync. Do not filter it.

Recommended regression test: a bound tab whose label changes to `fm-*` keeps its binding, window state, and display-name sync, and no new topic is created for it.

- [ ] **Step 4: Run the affected suites**

Run: `uv run pytest tests/ccgram/test_session_monitor.py tests/ccgram/test_session.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/ccgram/session_monitor.py tests/ccgram/test_session_monitor.py
git commit -m "fix(herdr): stop pruning live internal tabs and adopting crewmate tabs as topics"
```

---

### Task 3: status poll loop — reliable listing, no mass-death on failure

**Files:**
- Modify: `src/ccgram/handlers/polling/polling_coordinator.py:74-120`
- Test: `tests/ccgram/handlers/polling/test_polling_coordinator.py` (the file that owns the `status_poll_loop` harness — NOT test_status_polling.py, which never touches the loop)

**Interfaces:**
- Consumes: `list_windows_for_reconciliation(backend)` from `multiplexer.reconciliation`, `WindowRef.internal`.

- [ ] **Step 1: Write the failing test** (in `test_polling_coordinator.py`, extending its `_patch_loop_deps`/`_run_loop_once` harness. That harness currently mocks `tmux_manager.list_windows` (~line 80) and drives its error/backoff cases off it (~lines 188, 338, 366, 395): rewire those to the new seam — patch `polling_coordinator.list_windows_for_reconciliation` directly, or give the mocked manager an `AsyncMock` of that method — keeping the exception→backoff path distinct from the new None→skip path. The import-allowlist test needs no change: `...multiplexer.reconciliation` already passes its `..multiplexer` prefix rule.)

```python
async def test_poll_skips_tick_when_listing_unavailable(monkeypatch) -> None:
    """A failed listing must not tick windows (and thus not mass-declare death)."""
    ticked: list[str] = []

    async def fake_reconciliation(_backend):
        return None

    async def fake_tick(bot, user_id, thread_id, wid, w, runtime=None):
        ticked.append(wid)

    monkeypatch.setattr(
        polling_coordinator,
        "list_windows_for_reconciliation",
        fake_reconciliation,
    )
    monkeypatch.setattr(polling_coordinator.window_tick, "tick_window", fake_tick)
    # drive exactly one loop iteration per the file's existing pattern
    ...
    assert ticked == []
```

Plus the parity assertion: when the listing returns a bound window marked `internal=True`, `tick_window` still receives its `WindowRef` (bound fm-* topics keep live status).

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/ccgram/handlers/polling/test_polling_coordinator.py -k listing_unavailable -v`
Expected: FAIL — coordinator has no `list_windows_for_reconciliation` attribute / windows ticked with `None`.

- [ ] **Step 3: Implement** (in `status_poll_loop`; add the import `from ...multiplexer.reconciliation import list_windows_for_reconciliation` next to the existing multiplexer import)

```python
            refs = await list_windows_for_reconciliation(tmux_manager)
            if refs is None:
                # A failed listing is not "zero windows": ticking with an empty
                # lookup would dead-banner every bound topic (see 02eeb16 for
                # the monitor-side fix of the same class).
                log_throttled(
                    logger,
                    "status-poll:listing",
                    "Multiplexer listing unavailable; skipping status tick",
                )
                await asyncio.sleep(poll_interval)
                continue
            window_lookup = {w.window_id: w for w in refs}
            unbound_eligible = [w for w in refs if not w.internal]

            await run_periodic_tasks(client, refs, timers)
            await _tick_bound_windows(bot, window_lookup)
            await run_lifecycle_tasks(client, unbound_eligible)
```

(`window_lookup` and `run_periodic_tasks` deliberately get the FULL list: a bound fm-* topic must resolve its window, and the periodic path's `prune_stale_state`/`sync_display_names` are state-sync for existing entries — internal windows are alive and must not be pruned out. Only `run_lifecycle_tasks` — the unbound-window TTL, a discovery-shaped surface — gets the filtered list, preserving today's behavior. Note: `polling_coordinator.py` is exactly 120 lines and `test_polling_coordinator.py:319-323` pins `<= 120`; this change adds a few lines — raise that ceiling to 140 in the same commit. The invariant keeps the coordinator thin; a listing-safety guard is legitimate growth.)

- [ ] **Step 4: Run the polling suite**

Run: `uv run pytest tests/ccgram/handlers/polling/ -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/ccgram/handlers/polling/polling_coordinator.py tests/ccgram/handlers/polling/test_polling_coordinator.py
git commit -m "fix(polling): use reliable listing so a herdr hiccup cannot dead-banner every topic"
```

---

### Task 4: `_tab_list` / `_workspace_labels` shape guards + protocol 17

**Files:**
- Modify: `src/ccgram/multiplexer/herdr.py:78-82` (protocols), `:302-332` (`_tab_list`, `_workspace_labels`), `:421-470` (`list_windows_for_reconciliation` labels), `:481-505` (`find_window_by_id` labels)
- Test: `tests/ccgram/test_herdr_backend.py`

**Interfaces:**
- Produces: `_workspace_labels() -> dict[str, str] | None` — `{}` ONLY for "workspace addressing unsupported" (CLI exit 2) or a genuinely empty list; `None` for transient failure. `list_windows_for_reconciliation` returns `None` when labels are `None`; `find_window_by_id` degrades `None` to `{}`.

- [ ] **Step 1: Write the failing tests**

```python
async def test_tab_list_shape_drift_returns_none() -> None:
    # exit 0 + valid JSON but a renamed key must read as "unavailable", not "no tabs".
    drifted = json.dumps({"result": {"tab_items": [], "type": "tab_list"}})
    fake = FakeHerdr().on("tab", "list", out=drifted)
    assert await _manager(fake).list_windows_for_reconciliation() is None


async def test_workspace_list_transient_failure_makes_listing_unavailable() -> None:
    fake = (
        FakeHerdr()
        .on("tab", "list", out=TAB_LIST)
        .on("pane", "list", out=PANE_LIST)
        .on("workspace", "list", rc=1, err="socket error")
    )
    assert await _manager(fake).list_windows_for_reconciliation() is None


async def test_workspace_list_unsupported_degrades_to_empty_labels() -> None:
    # Older herdr without workspace addressing: CLI syntax errors exit 2.
    fake = (
        FakeHerdr()
        .on("tab", "list", out=TAB_LIST)
        .on("pane", "list", out=PANE_LIST)
        .on("workspace", "list", rc=2, err="unknown subcommand")
    )
    refs = await _manager(fake).list_windows_for_reconciliation()
    assert refs is not None and len(refs) == 2  # tabs listed, labels degrade


# Protocol 17: NO new positive test — test_ensure_session_accepts_supported_protocol
# already parametrizes over sorted(HERDR_SUPPORTED_PROTOCOLS) and picks 17 up
# automatically once the set changes. BUT the unverified-protocol test
# (test_herdr_backend.py:1573) currently parametrizes [13, 17, "17", None, []]:
# remove integer 17 and replace it with a still-unsupported integer (18); keep
# the string "17" case (non-int stays unverified). Without this edit the suite
# contradicts itself.


async def test_workspace_labels_malformed_shapes_return_none() -> None:
    # The contract promises None for EVERY unintelligible workspace answer:
    # non-JSON stdout, an "error" payload, a missing "workspaces" key.
    for out in (
        "not json",
        json.dumps({"error": {"code": 1}}),
        json.dumps({"result": {"type": "workspace_list"}}),
    ):
        fake = (
            FakeHerdr()
            .on("tab", "list", out=TAB_LIST)
            .on("pane", "list", out=PANE_LIST)
            .on("workspace", "list", out=out)
        )
        assert await _manager(fake).list_windows_for_reconciliation() is None
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/ccgram/test_herdr_backend.py -k "shape_drift or workspace or unverified_protocol" -v`
Expected: FAIL — the shape/transient/unsupported/malformed cases are red; the unverified-protocol parametrization stays green until Step 3 flips both it and the supported set together.

- [ ] **Step 3: Implement**

```python
HERDR_SUPPORTED_PROTOCOLS = frozenset({14, 15, 16, 17})
```

```python
    async def _tab_list(self) -> list[dict] | None:
        """Return raw tab dicts, or None when ``tab list`` is unavailable.

        An unintelligible result (missing/renamed ``tabs`` key — e.g. an
        unverified protocol whose shape drifted) is also None: reconciliation
        must see "listing unavailable", never an affirmative "zero tabs".
        """
        result = await self._call_json(["tab", "list"])
        if result is None:
            return None
        tabs = result.get("tabs")
        if not isinstance(tabs, list):
            logger.debug("herdr tab list returned unexpected shape")
            return None
        return [t for t in tabs if isinstance(t, dict) and t.get("tab_id")]
```

```python
    async def _workspace_labels(self) -> dict[str, str] | None:
        """Map every ``workspace_id`` → its label (one ``workspace list`` call).

        ``{}`` only when the command is unsupported (older server, CLI exit 2 —
        the adaptive label degrades to the agent name alone) or the list is
        genuinely empty. ``None`` on transient failure (socket down, timeout,
        error payload, drifted shape) so reconciliation reports "unavailable"
        instead of silently dropping the ``__*__`` workspace protection and
        churning every topic title.
        """
        rc, out, err = await self._run(["workspace", "list"])
        if rc == 2:
            return {}
        if rc != 0:
            logger.debug("herdr workspace list failed", rc=rc, err=err.strip())
            return None
        try:
            payload = json.loads(out)
        except (json.JSONDecodeError, ValueError):
            return None
        if not isinstance(payload, dict) or "error" in payload:
            return None
        result = payload.get("result")
        workspaces = result.get("workspaces") if isinstance(result, dict) else None
        if not isinstance(workspaces, list):
            return None
        return {
            w.get("workspace_id", ""): w.get("label", "")
            for w in workspaces
            if isinstance(w, dict) and w.get("workspace_id")
        }
```

In `list_windows_for_reconciliation` (after the tab list):

```python
        workspace_labels = await self._workspace_labels()
        if workspace_labels is None:
            return None
```

In `find_window_by_id` (bound-window resolution must not fail over labels):

```python
        workspace_labels = await self._workspace_labels() or {}
```

- [ ] **Step 4: Run the backend suite, then verify protocol 17 live**

Run: `uv run pytest tests/ccgram/test_herdr_backend.py -v`
Expected: PASS.
Run: `uv run pytest tests/integration/test_herdr_contract.py -m herdr -v` (live server here is protocol 17 — this is the verification that 17 belongs in the supported set).
Expected: PASS (scratch tabs created and closed by the suite itself).

- [ ] **Step 5: Commit**

```bash
git add src/ccgram/multiplexer/herdr.py tests/ccgram/test_herdr_backend.py
git commit -m "fix(herdr): never mistake a failed or drifted listing for an empty one; verify protocol 17"
```

---

### Task 5: translate tmux key tokens to herdr's kitty-style names

**Files:**
- Modify: `src/ccgram/multiplexer/herdr.py:104-109` (`_KEY_ALIASES`), `:658-670` (`_send_to`)
- Test: `tests/ccgram/test_herdr_backend.py`, `tests/integration/test_herdr_contract.py`

**Interfaces:**
- Produces: module-private `_translate_key(token: str) -> str`. Callers across the seam keep sending tmux vocabulary (`C-c`, `Escape`, `M-Enter`, `Up`…) — translation is entirely inside the herdr backend.

Context: herdr validates ALL keys before writing any bytes — one rejected token fails the whole `pane send-keys` call. Per the herdr 0.7.5 sources, `parse_key_combo` already accepts the legacy `C-c` alias and case-insensitive `Escape`/`Enter`/`Tab`/`Space`/arrow names — so Stop/Ctrl-C and Esc are NOT broken today. The genuinely rejected tmux tokens are `C-d`, `C-z`, `C-y`, every `M-*` form, and `BTab`: the EOF/Susp/YOLO toolbar buttons (toolbar_config.py:173-179) and the Pi follow-up `M-Enter` (window_ops.py:84) are silently dropped on herdr. The translation layer still canonicalizes the whole vocabulary to lowercase kitty-style names (`esc` per `herdr pane send-keys --help`) so future herdr versions can tighten aliases without breaking ccgram. herdr 0.7.5 has NO `home`/`end` key names — do not emit them.

- [ ] **Step 1: Write the failing unit tests**

```python
@pytest.mark.parametrize(
    ("token", "expected"),
    [
        ("C-c", "ctrl+c"),
        ("C-d", "ctrl+d"),
        ("C-z", "ctrl+z"),
        ("C-y", "ctrl+y"),
        ("M-Enter", "alt+enter"),
        ("Escape", "esc"),
        ("Enter", "enter"),
        ("Tab", "tab"),
        ("BSpace", "backspace"),
        ("Space", "space"),
        ("Up", "up"),
        ("Down", "down"),
        ("Left", "left"),
        ("Right", "right"),
        ("ctrl+c", "ctrl+c"),  # already-native names pass through
        ("x", "x"),            # plain characters pass through
    ],
)
async def test_send_keys_translates_tmux_tokens(token: str, expected: str) -> None:
    fake = (
        FakeHerdr()
        .on("pane", "list", out=PANE_LIST_FOR_FIND)
        .on("pane", "send-keys", out="")
    )
    ok = await _manager(fake).send("w2:t1", token, enter=False, literal=False)
    assert ok is True
    call = fake.sent("pane", "send-keys")
    assert call is not None and call[3:] == [expected]


async def test_send_keys_appended_enter_is_translated() -> None:
    fake = (
        FakeHerdr()
        .on("pane", "list", out=PANE_LIST_FOR_FIND)
        .on("pane", "send-keys", out="")
    )
    await _manager(fake).send("w2:t1", "Down", enter=True, literal=False)
    call = fake.sent("pane", "send-keys")
    assert call is not None and call[3:] == ["down", "enter"]
```

(Delete/adjust the old pass-through assertions in `test_send_special_keys_uses_send_keys` and `test_send_keys_appends_enter_when_requested` — they pinned the buggy vocabulary.)

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/ccgram/test_herdr_backend.py -k "translates_tmux or appended_enter" -v`
Expected: FAIL — tokens pass through untranslated.

- [ ] **Step 3: Implement**

```python
# The send-keys path uses tmux key vocabulary ("Up"/"C-c"/"M-Enter"/…); herdr
# validates every key before writing bytes and expects kitty-style lowercase
# names, so one untranslated token drops the whole call. Named keys map via
# the table; ``C-``/``M-`` prefixes map to ``ctrl+``/``alt+``. Unknown tokens
# (plain characters, already-native names) pass through.
_KEY_ALIASES: Mapping[str, str] = {
    "BSpace": "backspace",
    "Space": "space",
    "Escape": "esc",
    "Enter": "enter",
    "Tab": "tab",
    "BTab": "shift+tab",
    "Up": "up",
    "Down": "down",
    "Left": "left",
    "Right": "right",
}


def _translate_key(token: str) -> str:
    """Map one tmux key token to herdr's kitty-style name."""
    if token in _KEY_ALIASES:
        return _KEY_ALIASES[token]
    if len(token) > 2 and token[1] == "-" and token[0] in ("C", "M"):
        mod = "ctrl" if token[0] == "C" else "alt"
        rest = token[2:]
        return f"{mod}+{_KEY_ALIASES.get(rest, rest.lower() if len(rest) > 1 else rest)}"
    return token
```

In `_send_to`:

```python
        if not literal:
            keys = [_translate_key(tok) for tok in text.split() if tok]
            if enter:
                keys.append("enter")
```

- [ ] **Step 4: Add the live contract test and run both gates**

Append to `tests/integration/test_herdr_contract.py` (reuse its create/kill scratch-tab pattern from `test_create_send_capture_kill_roundtrip`):

```python
async def test_send_keys_vocabulary_accepted_live(herdr, tmp_path) -> None:
    """Every translated key name must pass herdr's pre-write validation."""
    ok, _msg, _name, window_id = await herdr.create_window(
        str(tmp_path), window_name="ccgram-keys-test", start_agent=False
    )
    assert ok
    try:
        # Foreground sink that survives C-c/C-d (cat restarts): control bytes
        # must not terminate the scratch shell mid-test.
        await herdr.send(window_id, "while true; do cat > /dev/null; done", enter=True)
        await asyncio.sleep(0.5)
        # C-z LAST: it suspends the sink loop, leaving a bare prompt behind.
        for token in ("Escape", "Enter", "Tab", "BSpace", "Up", "Down", "Left",
                      "Right", "M-Enter", "C-c", "C-d", "C-y", "C-z"):
            assert await herdr.send(window_id, token, enter=False, literal=False), token
    finally:
        # The shell may already be gone — cleanup must not mask the assertion.
        with contextlib.suppress(Exception):
            await herdr.kill_window(window_id)
```

Run: `uv run pytest tests/ccgram/test_herdr_backend.py -v` → PASS.
Run: `uv run pytest tests/integration/test_herdr_contract.py -m herdr -k vocabulary -v` → PASS. If a specific name is rejected live, the accepted spelling wins: fix the table entry, not the test.

- [ ] **Step 5: Commit**

```bash
git add src/ccgram/multiplexer/herdr.py tests/ccgram/test_herdr_backend.py tests/integration/test_herdr_contract.py
git commit -m "fix(herdr): translate tmux key tokens so Ctrl-C/Esc/arrows actually deliver"
```

---

### Task 6: agent-exit detection — effective pane command fallback

**Files:**
- Modify: `src/ccgram/multiplexer/herdr.py:397-415` (`_representative_pane` — focused-pane authority)
- Modify: `src/ccgram/handlers/polling/window_tick/observe.py` (new helper), `src/ccgram/handlers/polling/window_tick/__init__.py:49-56,64-105` (import + `tick_window`)
- Modify: `src/ccgram/handlers/recovery/transcript_discovery.py:100-116` (`_refresh_identity_from_pane` guard)
- Test: `tests/ccgram/test_herdr_backend.py`, `tests/ccgram/handlers/polling/window_tick/test_native_agent_status.py` (the real observe-test home — add new tests beside it), `tests/ccgram/handlers/recovery/`

Context: herdr fills `WindowRef.pane_current_command` with the agent label, which becomes `""` the moment the agent exits — while tmux reports the live shell (`zsh`). Consequences on herdr: `is_shell_prompt` is never True (decide kernel never sees "agent exited"), and `_refresh_identity_from_pane` bails on the empty command (transcript_discovery.py:114), so the provider never flips to shell — the next Telegram message runs in the interactive shell with no approval flow.

Multi-pane gap: `_representative_pane` (herdr.py:404-413) substitutes a NEIGHBOR pane's agent when the focused pane's agent is empty — so in a `/split` agent-team tab the focused agent's exit leaves `pane_current_command` = the neighbor's label and the empty-command fallback below never fires, while sends keep routing to the focused shell pane (`_active_pane`). Fix the representative contract first: when a focused pane exists, its agent is authoritative (empty stays empty); the neighbor fallback applies only when NO pane is focused. Deliberate side effect (state it in the commit): a multi-pane tab whose focused pane runs a bare shell stops surfacing in `is_agent_topic_window` discovery — consistent with where sends actually go.

**Interfaces:**
- Produces: `observe.effective_window(window_id: str, w: WindowRef) -> WindowRef` (async) — returns `w` unchanged unless the backend has `native_agent_status` AND `w.pane_current_command == ""`; then re-populates the command from `multiplexer.foreground()` (basename of `argv[0]` with the login-shell `-` stripped, `""` when no foreground).
- Produces: `HerdrManager._representative_pane` — focused-pane authority: a focused pane's empty agent stays empty; neighbor fallback only when no pane is focused.

- [ ] **Step 1: Write the failing tests**

```python
class _FakeMux:
    """Minimal multiplexer double (native_agent_status backend)."""

    def __init__(self, fg_argv: list[str] | None):
        self.capabilities = _herdr_like_caps()  # reuse the caps helper from test_native_agent_status.py
        self._fg_argv = fg_argv

    async def foreground(self, window_id: str):
        if self._fg_argv is None:
            return None
        return ForegroundInfo(pid=1, pgid=1, argv=self._fg_argv, cwd="/x", tty="")


async def test_effective_window_fills_command_from_foreground(monkeypatch) -> None:
    w = WindowRef(window_id="w2:t1", window_name="app", cwd="/x", pane_current_command="")
    # The multiplexer proxy has __slots__ = () — patch the MODULE BINDING, not
    # proxy attributes (idiom: test_native_agent_status.py:40).
    monkeypatch.setattr(observe, "tmux_manager", _FakeMux(["/bin/zsh"]))
    out = await observe.effective_window("w2:t1", w)
    assert out.pane_current_command == "zsh"
    assert is_shell_prompt(out.pane_current_command) is True


async def test_effective_window_normalizes_login_shell_argv(monkeypatch) -> None:
    # herdr login-mode panes report argv0 "-zsh" (portable-pty prepends "-").
    w = WindowRef(window_id="w2:t1", window_name="app", cwd="/x", pane_current_command="")
    monkeypatch.setattr(observe, "tmux_manager", _FakeMux(["-zsh"]))
    out = await observe.effective_window("w2:t1", w)
    assert out.pane_current_command == "zsh"


async def test_effective_window_keeps_agent_label(monkeypatch) -> None:
    w = WindowRef(window_id="w2:t1", window_name="app", cwd="/x", pane_current_command="claude")
    monkeypatch.setattr(observe, "tmux_manager", _FakeMux(None))
    assert (await observe.effective_window("w2:t1", w)) is w
```

Backend test (in `test_herdr_backend.py`) pinning the representative contract:

```python
async def test_representative_pane_follows_focused_pane() -> None:
    """A focused agentless pane must NOT inherit a neighbor's agent label."""
    panes = [
        {"pane_id": "w2:p1", "tab_id": "w2:t1", "focused": True, "cwd": "/x"},
        {"pane_id": "w2:p2", "tab_id": "w2:t1", "focused": False, "agent": "claude"},
    ]
    agent, _cwd = HerdrManager._representative_pane(panes, "/x")
    assert agent == ""
```

Wiring test (this is what catches the user-visible defect end-to-end): drive `tick_window` with a bound herdr-like window whose `pane_current_command == ""` and a `_FakeMux` foreground of `["/bin/zsh"]`; monkeypatch `discover_and_register_transcript` and `_update_status` to capture their `_window` kwarg and assert BOTH received the effective ref with `pane_current_command == "zsh"`.

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/ccgram/handlers/polling/ -k effective_window -v`
Expected: FAIL — `observe` has no `effective_window`.

- [ ] **Step 3: Implement**

First the representative contract in `herdr.py` (replace `_representative_pane`, lines 397-415):

```python
    @staticmethod
    def _representative_pane(tab_panes: list[dict], tab_cwd: str) -> tuple[str, str]:
        """Return ``(agent, cwd)`` for the representative pane in *tab_panes*.

        The focused pane is authoritative when present: its empty agent stays
        empty (the pane dropped to a shell — sends route there via
        ``_active_pane``, so the label must not borrow a neighbor's agent).
        The first-non-empty fallback applies only when no pane is focused.
        """
        focused = next((p for p in tab_panes if p.get("focused")), None)
        if focused:
            agent = focused.get("display_agent") or focused.get("agent", "")
            return agent, focused.get("cwd", "") or tab_cwd
        for pane in tab_panes:
            candidate = pane.get("display_agent") or pane.get("agent", "")
            if candidate:
                return candidate, pane.get("cwd", "") or tab_cwd
        return "", tab_cwd
```

Then in `observe.py`:

```python
async def effective_window(window_id: str, w: "TmuxWindow") -> "TmuxWindow":
    """Fill an empty ``pane_current_command`` from the live foreground process.

    On ``native_agent_status`` backends (herdr) the field carries the agent
    label and goes empty when the agent exits — unlike tmux, which reports the
    shell. Shell-exit detection (``is_shell_prompt``) and provider re-detection
    both key off this field, so resolve the foreground once when it is empty.
    """
    if w.pane_current_command or not tmux_manager.capabilities.native_agent_status:
        return w
    fg = await tmux_manager.foreground(window_id)
    cmd = ""
    if fg and fg.argv:
        # lstrip("-"): login shells report argv0 "-zsh" (cf. shell_infra.py:170).
        cmd = fg.argv[0].rsplit("/", 1)[-1].lstrip("-")
    if not cmd:
        return w
    return replace(w, pane_current_command=cmd)
```

(`from dataclasses import replace` at module top.) Add `effective_window` to the `from .observe import (...)` block in `window_tick/__init__.py` (lines 49-56) — without it the call below NameErrors. Then in `tick_window`, immediately after the `window is None` death branch:

```python
    window = await effective_window(window_id, window)
```

so both `discover_and_register_transcript(_window=window)` and `_update_status(_window=window)` see the effective ref. In `transcript_discovery._refresh_identity_from_pane`, split the guard so a live window with an empty command no longer aborts provider re-detection (the empty-command foreground fallback inside `detect_provider_from_pane` then classifies the pane):

```python
    if w is None:
        return identity, False
    if not w.pane_current_command and not tmux_manager.capabilities.native_agent_status:
        # tmux: an empty command is a transient read — keep today's early-out.
        return identity, False
```

- [ ] **Step 4: Run the affected suites**

Run: `uv run pytest tests/ccgram/test_herdr_backend.py tests/ccgram/handlers/polling/ tests/ccgram/handlers/recovery/ -v`
Expected: PASS (existing `test_list_windows_uses_focused_pane_as_representative` may need its fixture adjusted to keep a non-empty focused agent — the contract it pins is unchanged for that case).

- [ ] **Step 5: Commit**

```bash
git add src/ccgram/multiplexer/herdr.py src/ccgram/handlers/polling/window_tick/observe.py src/ccgram/handlers/polling/window_tick/__init__.py src/ccgram/handlers/recovery/transcript_discovery.py tests/ccgram/test_herdr_backend.py tests/ccgram/handlers/polling/window_tick/test_native_agent_status.py
git commit -m "fix(herdr): detect agent exit to shell so provider flips and approval flow engages"
```

---

### Task 7: evict stale push-status cache entries on reprime

**Files:**
- Modify: `src/ccgram/multiplexer/agent_status_cache.py` (three-state), `src/ccgram/multiplexer/herdr.py:1053-1090` (`watch_events` reprime), `src/ccgram/multiplexer/base.py` (MuxEvent doc), `src/ccgram/event_stream_monitor.py:114-119` (`_dispatch`), `src/ccgram/handlers/polling/window_tick/observe.py:118-126` (`_native_agent_status`)
- Test: `tests/ccgram/test_herdr_backend.py`, `tests/ccgram/handlers/polling/window_tick/test_native_agent_status.py`, the EventStreamMonitor test file (locate with `grep -rl EventStreamMonitor tests/`)

Context: `agent_status_cache` is evicted only on window death. The stream reconnects (server hiccup, and on every bound-set change via `_run_until_set_change`), and pushes lost in the gap are gone; the reprime only yields when `agent_status()` returns non-None (herdr.py:1073-1074), so an entry for an agent that exited during the gap serves stale "working"/"blocked" forever — `observe._native_agent_status` skips the corrective live read exactly when the cache is warm.

**Interfaces:**
- Produces: `agent_status_cache` becomes three-state — absent = cold; entry `None` = "known: no agent" (negative marker); entry `AgentStatus` = live status. New `lookup(window_id) -> tuple[bool, AgentStatus | None]` (`(warm, status)`); `set_status` accepts `AgentStatus | None`; `clear`/`reset` unchanged.
- Produces: during reprime, `watch_events` yields `MuxEvent(kind="agent_status", …, status=None)` for every watched window with no live agent (including windows whose tab resolved no pane), reading the ALREADY-SUBSCRIBED pane directly. `EventStreamMonitor._dispatch` writes `event.status` through (None becomes the negative marker); `window_died` still evicts via `clear`.
- Consumes: `observe._native_agent_status` switches to `lookup()` — a warm negative marker returns None WITHOUT the subprocess fallback (that fallback is exactly the churn this task removes; cf. the TTL rejection in Out of scope).

- [ ] **Step 1: Write the failing tests**

In `test_herdr_backend.py`, extend the `_stream_of`-driven watch_events test set (reuse the collect idiom of `test_watch_events_reprimes_filters_and_streams`):

```python
async def test_watch_events_reprime_yields_none_status_for_agentless_pane() -> None:
    """Reprime must emit status=None when the subscribed pane has no agent."""
    # pane resolves, but its `pane get` payload carries no "agent_status"
    ...  # fixture: PANE_GET variant without "agent_status"; collect the first
    # reprime batch and assert an agent_status event with status is None


async def test_watch_events_reprime_reads_subscribed_pane_not_refocused_one() -> None:
    """After a focus flip, reprime must read the pane the stream subscribed."""
    ...  # two panes p1 (focused at subscribe) and p2; before the reprime the
    # fake runner flips focus to p2 with a different agent_status; assert the
    # reprime event carries p1's status and exactly one `pane get w2:p1` call
    # was recorded (agent_status(window_id) would re-resolve to p2 and cost 2 calls)
```

In the EventStreamMonitor tests:

```python
async def test_dispatch_none_status_writes_negative_marker() -> None:
    agent_status_cache.set_status("w2:t1", AgentStatus(state="working", agent="claude", custom_status=""))
    monitor = EventStreamMonitor(FakeTelegramClient(), lambda: {"w2:t1"})
    await monitor._dispatch(MuxEvent(kind="agent_status", window_id="w2:t1", pane_id="w2:p1", status=None))
    assert agent_status_cache.lookup("w2:t1") == (True, None)  # warm negative, NOT cold
```

In `test_native_agent_status.py`:

```python
async def test_known_no_agent_skips_subprocess_fallback(monkeypatch) -> None:
    agent_status_cache.set_status("w2:t1", None)  # negative marker
    fake_mux = _herdr_like_mux()  # per the file's existing double
    monkeypatch.setattr(observe, "tmux_manager", fake_mux)
    assert await observe._native_agent_status("w2:t1") is None
    assert fake_mux.agent_status_calls == 0  # no backend lookup on warm-none
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/ccgram -k "reprime_yields_none or reads_subscribed_pane or negative_marker or skips_subprocess" -v`
(one combined `-k`: pytest keeps only the LAST `-k` flag when several are given)
Expected: FAIL.

- [ ] **Step 3: Implement**

Three-state `agent_status_cache` (values become `AgentStatus | None`; absence stays cold):

```python
_cache: dict[str, AgentStatus | None] = {}


def set_status(window_id: str, status: AgentStatus | None) -> None:
    """Record the latest push-reported status; ``None`` = "known: no agent".

    The negative marker is refreshed only by pushes on subscribed panes: an
    agent started later in a pane that was NOT subscribed at connect time
    keeps the marker until the next stream restart — acceptable, since the
    stream restarts on every bound-set change.
    """
    _cache[window_id] = status


def lookup(window_id: str) -> tuple[bool, AgentStatus | None]:
    """Return ``(warm, status)``: cold cache is ``(False, None)``; a warm
    ``(True, None)`` means "no agent — do not fork a fallback lookup"."""
    if window_id in _cache:
        return True, _cache[window_id]
    return False, None
```

(`get_status`/`clear`/`reset` keep their signatures; `get_status` returns the entry or None as before.) `observe._native_agent_status` (observe.py:118-126) switches to the three-state read:

```python
    warm, native = agent_status_cache.lookup(window_id)
    if not warm:
        native = await tmux_manager.agent_status(window_id)
```

`watch_events` reprime block (replace lines 1071-1081) — read the SUBSCRIBED pane directly; `agent_status(window_id)` would re-resolve the active pane, which after a focus change primes the cache from a different pane than the stream watches (and costs two CLI calls):

```python
                        backoff = _STREAM_BACKOFF_BASE
                        primed: set[str] = set()
                        for pane_id, window_id in pane_to_window.items():
                            primed.add(window_id)
                            pane = await self._pane_get(pane_id) or {}
                            state = (pane.get("agent_status") or "").strip()
                            # status=None is the negative marker: the agent left
                            # while the stream was down; a stale cached "working"
                            # must not outlive the reconnect.
                            yield MuxEvent(
                                kind="agent_status",
                                window_id=window_id,
                                pane_id=pane_id,
                                status=AgentStatus(
                                    state=state,
                                    agent=(pane.get("agent") or "").strip(),
                                    custom_status=(pane.get("custom_status") or "").strip(),
                                )
                                if state
                                else None,
                            )
                        for window_id in ids:
                            if window_id not in primed:
                                yield MuxEvent(
                                    kind="agent_status",
                                    window_id=window_id,
                                    pane_id="",
                                    status=None,
                                )
                        continue
```

`event_stream_monitor._dispatch`:

```python
    async def _dispatch(self, event: MuxEvent) -> None:
        if event.kind == "agent_status":
            # None flows through as the negative marker, NOT an eviction —
            # an evicted (cold) entry would re-fork the subprocess fallback
            # every tick for agentless panes.
            agent_status_cache.set_status(event.window_id, event.status)
        elif event.kind == "window_died":
            agent_status_cache.clear(event.window_id)
            await self._notify_dead(event.window_id)
```

Document on `MuxEvent` (base.py): `status=None` on an `agent_status` event means "no agent present in the watched pane".

- [ ] **Step 4: Run**

Run: `uv run pytest tests/ccgram -k "event_stream or watch_events or native_agent_status" -v`
Expected: PASS (the existing reprime test asserting non-None yields needs its expectation extended, not weakened: same panes, now every watched window yields exactly one reprime event).

- [ ] **Step 5: Commit**

```bash
git add src/ccgram/multiplexer/agent_status_cache.py src/ccgram/multiplexer/herdr.py src/ccgram/multiplexer/base.py src/ccgram/event_stream_monitor.py src/ccgram/handlers/polling/window_tick/observe.py tests/ccgram/test_herdr_backend.py tests/ccgram/handlers/polling/window_tick/test_native_agent_status.py
git commit -m "fix(herdr): three-state push-status cache, reprime from the subscribed pane"
```

---

### Task 8: fail the stream on a rejected subscribe + real socket tests for `open_socket_stream`

**Files:**
- Modify: `src/ccgram/multiplexer/herdr_events.py:77-84`
- Test: new `tests/ccgram/test_herdr_events_socket.py`

Context: an error ack (or EOF before ack) still yields `SUBSCRIBED`, so `watch_events` resets backoff to 1s on every attempt (hot loop) or sits on a silently-dead subscription; and `open_socket_stream` — the only long-lived socket reader — has zero unit tests (every test injects a fake opener).

**Interfaces:**
- Produces: `open_socket_stream` raises `OSError` on: EOF before ack, non-JSON ack, ack carrying an `error` payload. The sentinel is yielded only after a successful ack. `watch_events` already catches `OSError` → its exponential backoff finally engages (reset happens only on the sentinel).

- [ ] **Step 1: Write the failing tests** (new file; a real unix-socket server per test)

```python
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
    json.dumps({"id": "ccgram-events", "result": {"type": "subscription_started"}}).encode()
    + b"\n"
)


async def test_subscribe_request_framing_and_sentinel_order() -> None:
    ok_ack = OK_ACK
    event = json.dumps({"event": "tab.closed", "data": {"tab_id": "w1:t1"}}).encode() + b"\n"
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
        json.dumps({"id": "ccgram-events", "error": {"code": "bad", "message": "unsupported"}}).encode()
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
```

Also extend `test_translate_event_maps_and_filters` (in `test_herdr_backend.py`) with the alternate name forms the code claims to match: `pane_agent_status_changed` (underscore) and `tab_closed` vs `tab.closed`.

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/ccgram/test_herdr_events_socket.py -v`
Expected: `test_error_ack_*` and `test_eof_before_ack_*` FAIL (sentinel yielded today); framing/malformed tests PASS (pinning current behavior).

- [ ] **Step 3: Implement** (replace herdr_events.py lines 77-84)

```python
        # First line is the subscription ack — herdr 0.7.5 sends
        # {"id": "ccgram-events", "result": {"type": "subscription_started"}}
        # (or {"id": …, "error": {…}}). A missing, rejected, or unintelligible
        # ack must NOT look like a live subscription: raise so watch_events'
        # reconnect backoff engages (the sentinel is what resets backoff)
        # instead of hot-looping or sitting on a dead stream.
        ack = await reader.readline()
        if not ack:
            raise OSError("herdr events.subscribe: connection closed before ack")
        try:
            payload = json.loads(ack)
        except ValueError as exc:
            raise OSError("herdr events.subscribe: non-JSON ack") from exc
        if not isinstance(payload, dict):
            raise OSError("herdr events.subscribe: unexpected ack shape")
        if "error" in payload:
            raise OSError(f"herdr events.subscribe rejected: {payload['error']}")
        if "result" not in payload:
            raise OSError("herdr events.subscribe: unexpected ack shape")
        yield SUBSCRIBED
```

(`contextlib` import may become unused in that function — keep it only if still used by the `finally` block, which it is.)

- [ ] **Step 4: Run**

Run: `uv run pytest tests/ccgram/test_herdr_events_socket.py tests/ccgram/test_herdr_backend.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/ccgram/multiplexer/herdr_events.py tests/ccgram/test_herdr_events_socket.py tests/ccgram/test_herdr_backend.py
git commit -m "fix(herdr): reject failed event subscriptions so reconnect backoff engages"
```

---

### Task 9: enforce the `window_id` containment guard in pane-direct ops

**Files:**
- Modify: `src/ccgram/multiplexer/herdr.py:641-656` (`send_to_pane`), `:1093-1106` (`capture_pane_by_id`)
- Test: `tests/ccgram/test_herdr_backend.py`

Context: base.py documents `window_id` as "cross-window access prevention" and tmux enforces it (tmux.py:806-811, 870-889); herdr ignores it (`# noqa: ARG002`), so stale/crafted Telegram callback data `ks:ent:<own-tab>|<foreign-pane>` delivers keystrokes into (or captures) a pane of another tab. Exposed callers: interactive_callbacks, status_bar_actions, screenshot/live-view captures (miniapp validates independently).

- [ ] **Step 1: Write the failing tests**

```python
async def test_send_to_pane_rejects_pane_outside_window() -> None:
    fake = (
        FakeHerdr()
        .on("pane", "list", out=PANE_LIST)  # w1:p1 in w1:t1, w2:p2 in w2:t2
        .on("pane", "send-text", out="")
    )
    ok = await _manager(fake).send_to_pane(
        "w1:p1", "hi", enter=False, window_id="w2:t2"
    )
    assert ok is False
    assert fake.sent("pane", "send-text") is None  # nothing was delivered


async def test_capture_pane_by_id_rejects_pane_outside_window() -> None:
    fake = (
        FakeHerdr()
        .on("pane", "list", out=PANE_LIST)
        .on("pane", "read", out="secret")
    )
    text = await _manager(fake).capture_pane_by_id("w1:p1", window_id="w2:t2")
    assert text is None
    assert fake.sent("pane", "read") is None


async def test_send_to_pane_allows_pane_inside_window() -> None:
    fake = (
        FakeHerdr()
        .on("pane", "list", out=PANE_LIST)
        .on("pane", "send-text", out="")
    )
    assert await _manager(fake).send_to_pane(
        "w1:p1", "hi", enter=False, window_id="w1:t1"
    ) is True
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/ccgram/test_herdr_backend.py -k "outside_window or inside_window" -v`
Expected: rejection tests FAIL (guard absent).

- [ ] **Step 3: Implement**

```python
    async def _pane_in_tab(self, pane_id: str, tab_id: str) -> bool:
        """True when *pane_id* belongs to *tab_id* (containment guard)."""
        panes = await self._panes_for_tab(tab_id)
        return any(p.get("pane_id") == pane_id for p in panes)
```

In `send_to_pane` (drop the `# noqa: ARG002`):

```python
        if window_id is not None and not await self._pane_in_tab(pane_id, window_id):
            logger.debug(
                "send_to_pane rejected: pane %s not in window %s", pane_id, window_id
            )
            return False
        return await self._send_to(pane_id, text, enter=enter, literal=literal)
```

In `capture_pane_by_id` (same guard, returning `None`), with the docstring noting parity with tmux's "validate pane belongs to the window before capture".

- [ ] **Step 4: Run**

Run: `uv run pytest tests/ccgram/test_herdr_backend.py tests/ccgram/test_multiplexer_contract.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/ccgram/multiplexer/herdr.py tests/ccgram/test_herdr_backend.py
git commit -m "fix(herdr): enforce window containment for pane-direct send and capture"
```

---

### Task 10: hook identity — innermost multiplexer wins (stale `TMUX_PANE` guard)

**Files:**
- Modify: `src/ccgram/multiplexer/self_identify.py`, `src/ccgram/hook.py:1144-1148` (first call site), `src/ccgram/hook.py:~1208` (second `resolve_self_identity` call site — route both through one helper), `src/ccgram/hook.py:705-721` (`_resolve_window_id` error handling)
- Test: `tests/ccgram/test_self_identify.py`, plus a PTY-level test in `tests/integration/`

Context: herdr panes inherit the server's env unsanitized (verified live from `/proc` environs). When the herdr server was launched inside tmux, every agent pane carries a stale `TMUX_PANE` pointing at the pane hosting herdr; the tmux probe succeeds, so on a herdr deployment ALL hooks write one shared `tmux:@N` key — instant notifications, approval UI, and topic-creation waits all silently break. Also: when the outer tmux later dies, the tmux probe fails and hooks are dropped entirely instead of falling through to herdr.

CRITICAL runtime facts (verified empirically on this machine): Claude Code spawns command hooks with `detached: true` (setsid) — the hook process has NO controlling tty and `open("/dev/tty")` raises ENXIO; and even in an attached child, `os.ttyname()` on a `/dev/tty` fd returns the alias `"/dev/tty"`, never `/dev/pts/N`. So the tty evidence must come from the ANCESTOR chain (the agent process sitting on the pane's pty), not from the hook process itself. On macOS (no `/proc`) the walk returns `""` and today's tmux-first behavior is preserved — documented limitation.

**Interfaces:**
- Produces: `resolve_self_identity(env, *, tmux_query, herdr_query=None, process_tty: str = "")` — `process_tty` is the originating agent's tty (from `_ancestor_tty()`), `""` = no evidence. Rules: a fresh tmux resolution (tty matches, or no evidence) wins as today; a STALE tmux resolution falls through to herdr ONLY when the herdr identity actually resolves — otherwise the tmux identity is returned (a stale-looking resolution must never DROP the hook; that would regress tmux-only setups). A failed tmux probe with `HERDR_PANE_ID` set falls through to herdr instead of returning None.
- Produces: `hook._ancestor_tty() -> str` — nearest ancestor's terminal via the `/proc` PPID walk.
- Produces: one shared `hook._resolve_hook_identity()` helper used by BOTH call sites (1144 and ~1208) so they can never diverge.

- [ ] **Step 1: Write the failing tests** (pure table tests, injected queries)

```python
def _tmux_ok(pane: str):
    return ("ccgram:@5", "@5", "app", "/dev/pts/3")


def test_stale_tmux_pane_falls_through_to_herdr() -> None:
    env = {"TMUX_PANE": "%1", "HERDR_PANE_ID": "w2:p1"}
    identity = resolve_self_identity(
        env, tmux_query=_tmux_ok, herdr_query=lambda p: "w2:t1",
        process_tty="/dev/pts/9",  # agent's tty != resolved pane's tty → stale
    )
    assert identity is not None and identity.mux == "herdr"
    assert identity.session_window_key == "herdr:w2:t1"


def test_stale_tmux_without_herdr_keeps_tmux_identity() -> None:
    # A stale-looking resolution must never DROP the hook on tmux-only setups.
    identity = resolve_self_identity(
        {"TMUX_PANE": "%1"}, tmux_query=_tmux_ok, process_tty="/dev/pts/9",
    )
    assert identity is not None and identity.mux == "tmux"


def test_stale_tmux_with_failing_herdr_query_keeps_tmux_identity() -> None:
    env = {"TMUX_PANE": "%1", "HERDR_PANE_ID": "w2:p1"}
    identity = resolve_self_identity(
        env, tmux_query=_tmux_ok, herdr_query=lambda p: None,
        process_tty="/dev/pts/9",
    )
    assert identity is not None and identity.mux == "tmux"


def test_fresh_tmux_pane_still_wins() -> None:
    env = {"TMUX_PANE": "%1", "HERDR_PANE_ID": "w2:p1"}
    identity = resolve_self_identity(
        env, tmux_query=_tmux_ok, herdr_query=lambda p: "w2:t1",
        process_tty="/dev/pts/3",
    )
    assert identity is not None and identity.mux == "tmux"


def test_no_tty_evidence_keeps_tmux_priority() -> None:
    env = {"TMUX_PANE": "%1", "HERDR_PANE_ID": "w2:p1"}
    identity = resolve_self_identity(
        env, tmux_query=_tmux_ok, herdr_query=lambda p: "w2:t1", process_tty="",
    )
    assert identity is not None and identity.mux == "tmux"


def test_failed_tmux_probe_falls_through_to_herdr() -> None:
    env = {"TMUX_PANE": "%1", "HERDR_PANE_ID": "w2:p1"}
    identity = resolve_self_identity(
        env, tmux_query=lambda p: None, herdr_query=lambda p: "w2:t1",
    )
    assert identity is not None and identity.mux == "herdr"


def test_failed_tmux_probe_without_herdr_returns_none() -> None:
    identity = resolve_self_identity(
        {"TMUX_PANE": "%1"}, tmux_query=lambda p: None,
    )
    assert identity is None
```

And the PTY-level integration test mirroring the REAL hook runtime (new file `tests/integration/test_hook_identity_pty.py`, `@pytest.mark.skipif(not Path("/proc").exists(), reason="needs /proc")`):

```python
def test_detached_hook_resolves_herdr_identity_under_stale_tmux(tmp_path) -> None:
    """agent-on-pty → setsid-detached hook child with piped stdio (the real
    Claude Code spawn shape) must resolve the herdr identity, not stale tmux."""
    probe = tmp_path / "probe.py"
    probe.write_text(
        "import json, sys\n"
        f"sys.path.insert(0, {str(SRC_DIR)!r})\n"
        "from ccgram.hook import _ancestor_tty\n"
        "from ccgram.multiplexer.self_identify import resolve_self_identity\n"
        "identity = resolve_self_identity(\n"
        "    {'TMUX_PANE': '%1', 'HERDR_PANE_ID': 'w2:p1'},\n"
        "    tmux_query=lambda p: ('ccgram:@5', '@5', 'app', '/dev/pts/999'),\n"
        "    herdr_query=lambda p: 'w2:t1',\n"
        "    process_tty=_ancestor_tty(),\n"
        ")\n"
        "print(json.dumps({'mux': identity.mux if identity else None}))\n"
    )
    controller, follower = pty.openpty()
    # sh stands in for the agent (stdin = pane pty); it spawns the probe via
    # setsid — detached, piped stdio — exactly like Claude Code spawns hooks.
    proc = subprocess.run(
        ["sh", "-c", f"setsid {sys.executable} {probe}"],
        stdin=follower, capture_output=True, text=True, timeout=30,
    )
    os.close(controller), os.close(follower)
    assert json.loads(proc.stdout) == {"mux": "herdr"}
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/ccgram/test_self_identify.py -v`
Expected: stale/failed-probe fall-through tests FAIL.

- [ ] **Step 3: Implement**

Restructure `resolve_self_identity` (docstring updated: "innermost multiplexer wins; tmux wins unless its resolution is provably stale AND a herdr identity resolves"):

```python
    tmux_pane = env.get("TMUX_PANE", "")
    herdr_pane = env.get("HERDR_PANE_ID", "")

    def _herdr_identity() -> SelfIdentity | None:
        if not herdr_pane or herdr_query is None:
            return None
        tab_id = herdr_query(herdr_pane)
        if tab_id is None:
            return None
        return SelfIdentity(
            mux="herdr",
            session_window_key=f"herdr:{tab_id}",
            window_id=tab_id,
            window_name="",
        )

    if tmux_pane:
        resolved = tmux_query(tmux_pane)
        if resolved is not None:
            session_window_key, window_id, window_name, pane_tty = resolved
            # A herdr pane inherits the server's env unsanitized: a TMUX_PANE
            # from the tmux session HOSTING herdr resolves to the host pane's
            # tty, not the agent's. When the originating agent's tty disagrees
            # AND a herdr identity resolves, the innermost multiplexer wins.
            # Without a usable herdr fallback the tmux identity stands — a
            # stale-looking resolution must never drop the hook.
            stale = bool(process_tty) and bool(pane_tty) and process_tty != pane_tty
            if stale and (herdr := _herdr_identity()) is not None:
                return herdr
            return SelfIdentity(
                mux="tmux",
                session_window_key=session_window_key,
                window_id=window_id,
                window_name=window_name,
                pane_tty=pane_tty,
            )
        if not herdr_pane:
            return None
        # tmux probe failed (server gone, binary missing) but a herdr pane id
        # exists — fall through instead of dropping the hook.

    return _herdr_identity() if herdr_pane else None
```

In `hook.py`, add the ancestor-tty helper (NOT `/dev/tty` — the hook is detached, see Context) and one shared resolution helper for BOTH call sites (1144-1148 and the second `resolve_self_identity` call at ~1208):

```python
def _ancestor_tty() -> str:
    """Terminal device of the nearest ancestor that has one.

    Claude Code spawns hooks detached (setsid): the hook itself has no
    controlling tty (/dev/tty raises ENXIO), so walk the PPID chain via /proc
    and take the first ancestor whose stdin is a pty — the agent process
    sitting on the pane's terminal. Returns "" when /proc is unavailable
    (macOS) or no ancestor has one; the resolver then keeps today's
    tmux-first behavior.
    """
    pid = os.getppid()
    for _ in range(10):
        if pid <= 1:
            return ""
        try:
            tty = os.readlink(f"/proc/{pid}/fd/0")
        except OSError:
            tty = ""
        if tty.startswith("/dev/pts/") or (
            tty.startswith("/dev/tty") and tty != "/dev/tty"
        ):
            return tty
        try:
            with open(f"/proc/{pid}/stat") as fh:
                pid = int(fh.read().rpartition(")")[2].split()[1])
        except (OSError, ValueError, IndexError):
            return ""
    return ""


def _resolve_hook_identity() -> SelfIdentity | None:
    """Single resolution path for every hook identity consumer."""
    return resolve_self_identity(
        os.environ,
        tmux_query=_resolve_window_id,
        herdr_query=_resolve_herdr_tab_id,
        process_tty=_ancestor_tty(),
    )
```

Replace the direct `resolve_self_identity(...)` calls at BOTH call sites with `_resolve_hook_identity()`. Also widen the except in `_resolve_window_id` (hook.py:705-721) to `(subprocess.TimeoutExpired, OSError)` — today a missing `tmux` binary raises `FileNotFoundError` out of the hook instead of degrading to the herdr fallback this task introduces.

- [ ] **Step 4: Run**

Run: `uv run pytest tests/ccgram/test_self_identify.py tests/ccgram/test_hook*.py tests/integration/test_hook_pipeline.py tests/integration/test_hook_identity_pty.py -v`
Expected: PASS (tmux-only environments are behavior-identical: without `HERDR_PANE_ID` a stale-looking resolution still returns the tmux identity, and a failed probe still returns None).

- [ ] **Step 5: Commit**

```bash
git add src/ccgram/multiplexer/self_identify.py src/ccgram/hook.py tests/ccgram/test_self_identify.py tests/integration/test_hook_identity_pty.py
git commit -m "fix(hook): stop stale inherited TMUX_PANE from hijacking herdr hook identity"
```

---

### Task 11: resolve the event-stream socket from `herdr status` + audible connect failure

**Files:**
- Modify: `src/ccgram/multiplexer/herdr.py:359-395` (`ensure_session`), `:1085-1090` (stream error logging)
- Modify: `docs/guides.md` (~line 288, the `$HERDR_SOCKET_PATH` claim)
- Test: `tests/ccgram/test_herdr_backend.py`

Context: with `$HERDR_SOCKET_PATH` unset, CLI subprocess calls work (the binary finds its own socket) but `open_socket_stream("")` raises EINVAL on every attempt at debug level — the push stream is permanently dead and guides.md's "leave it unset" advice is wrong for the stream. `herdr status --json` exposes the server's socket as `server.socket` (verified live on 0.7.5).

- [ ] **Step 1: Write the failing tests**

```python
async def test_ensure_session_adopts_socket_from_status() -> None:
    status = json.dumps(
        {"server": {"running": True, "protocol": 17, "compatible": True,
                    "socket": "/run/herdr/herdr.sock"}}
    )
    fake = FakeHerdr().on("status", "--json", out=status)
    mgr = HerdrManager(socket_path=None, runner=fake)
    mgr._socket_path = ""  # simulate unset env regardless of test environment
    await mgr.ensure_session()
    assert mgr._socket_path == "/run/herdr/herdr.sock"


async def test_ensure_session_keeps_explicit_socket() -> None:
    status = json.dumps(
        {"server": {"running": True, "protocol": 17, "compatible": True,
                    "socket": "/run/other.sock"}}
    )
    fake = FakeHerdr().on("status", "--json", out=status)
    mgr = HerdrManager(socket_path="/tmp/mine.sock", runner=fake)
    await mgr.ensure_session()
    assert mgr._socket_path == "/tmp/mine.sock"


async def test_stream_connect_failure_warns_once_then_debug(caplog) -> None:
    """First stream failure is a WARNING, repeats are debug, success resets."""
    ...  # inject a stream_opener that raises OSError; drive two reconnect
    # iterations of watch_events (patch asyncio.sleep to advance instantly);
    # assert exactly one WARNING record; then let the opener succeed (yield
    # SUBSCRIBED) and fail again — a second WARNING proves the reset.
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/ccgram/test_herdr_backend.py -k "adopts_socket or keeps_explicit or warns_once" -v`
Expected: FAIL (adopts_socket, warns_once) / PASS (keeps_explicit — pins current behavior).

- [ ] **Step 3: Implement**

At the end of `ensure_session` (after the `server` dict is validated):

```python
        if not self._socket_path:
            # The CLI resolves its own default socket, but the push event
            # stream connects directly — adopt the server-reported path so
            # "leave $HERDR_SOCKET_PATH unset" works for the stream too.
            sock = server.get("socket")
            if isinstance(sock, str) and sock:
                self._socket_path = sock
```

In `watch_events`, make a persistently-failing connect audible (warning once, then debug; reset on a successful subscribe). Add `self._stream_warned = False` in `__init__`, set `self._stream_warned = False` next to `backoff = _STREAM_BACKOFF_BASE` in the sentinel branch, and replace the except body:

```python
            except OSError as exc:
                if not self._stream_warned:
                    logger.warning(
                        "herdr event stream unavailable (will keep retrying): %s", exc
                    )
                    self._stream_warned = True
                else:
                    logger.debug("herdr event stream error: %s", exc)
```

In `docs/guides.md`, rewrite the `$HERDR_SOCKET_PATH` sentence to match reality: unset → ccgram adopts the socket path reported by `herdr status` at startup for the push stream; set → targets a specific server.

- [ ] **Step 4: Run**

Run: `uv run pytest tests/ccgram/test_herdr_backend.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/ccgram/multiplexer/herdr.py docs/guides.md tests/ccgram/test_herdr_backend.py
git commit -m "fix(herdr): resolve event-stream socket from herdr status when env is unset"
```

---

### Task 12: off-load the projects scan from the event loop

**Files:**
- Modify: `src/ccgram/session_monitor.py:181-183`
- Test: `tests/ccgram/test_session_monitor.py`

- [ ] **Step 1: Write the failing test**

```python
import threading  # NB: the test file imports no threading today — add it


async def test_fallback_scan_runs_off_event_loop(monkeypatch) -> None:
    seen_threads: list = []

    def fake_scan(active_cwds):
        seen_threads.append(threading.current_thread())
        return []

    monitor = SessionMonitor(state_file=...)  # per existing fixture idiom
    monkeypatch.setattr(monitor, "_scan_projects_sync", fake_scan)
    monkeypatch.setattr(monitor, "_get_active_cwds", AsyncMock(return_value={"/x"}))
    current = {"w1:t1": {"session_id": "sid", "transcript_path": "/nope"}}
    await monitor.check_for_updates(current)
    # asyncio_mode="auto" runs the loop on the main thread, so this is a
    # valid off-loop assertion.
    assert seen_threads and seen_threads[0] is not threading.main_thread()
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/ccgram/test_session_monitor.py -k off_event_loop -v`
Expected: FAIL (scan runs on the main thread today).

- [ ] **Step 3: Implement** (session_monitor.py:183; the sibling transcript I/O already uses `asyncio.to_thread` — transcript_reader.py:172, 320)

```python
            sessions = (
                await asyncio.to_thread(self._scan_projects_sync, active_cwds)
                if active_cwds
                else []
            )
```

- [ ] **Step 4: Run**

Run: `uv run pytest tests/ccgram/test_session_monitor.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/ccgram/session_monitor.py tests/ccgram/test_session_monitor.py
git commit -m "fix(monitor): run the projects fallback scan off the event loop"
```

---

### Task 13: close the F2 contract-test gap

**Files:**
- Modify: `tests/ccgram/test_multiplexer_contract.py:25-48`

- [ ] **Step 1: Extend CONTRACT_METHODS** (all three exist and are async on both backends today)

```python
CONTRACT_METHODS = (
    "ensure_session",
    "list_windows",
    "list_windows_for_reconciliation",
    "list_workspaces",
    "agent_session",
    ...  # existing entries unchanged
)
```

- [ ] **Step 2: Run the contract suite**

Run: `uv run pytest tests/ccgram/test_multiplexer_contract.py -v`
Expected: PASS on both backends. If a backend fails, that IS the finding — fix the backend, not the list.

- [ ] **Step 3: Commit**

```bash
git add tests/ccgram/test_multiplexer_contract.py
git commit -m "test(multiplexer): pin reconciliation, workspace, and agent-session methods in F2"
```

---

### Task 14: positive-path tests for native agent-session discovery (ef0b5ac)

**Files:**
- Create: `tests/ccgram/handlers/recovery/test_native_session_discovery.py`

Context: the consult-first discovery flow (`_native_session_transcript` → `_transcript_for_session_id` cwd `/`→`-` encoding + glob fallback, pi `"<ts>_<id>"` stem parsing, `_register_native_session` dedupe) has zero positive-path tests — every existing test pins `native_agent_session = False`. Read `tests/ccgram/handlers/recovery/test_transcript_discovery_key.py` first and reuse its stubbing idiom for `tmux_manager` and `session_map_sync`.

- [ ] **Step 1: Write the tests** (target the module functions directly; monkeypatch `transcript_discovery.tmux_manager` and `transcript_discovery.session_map_sync` as module bindings. NB: `transcript_discovery` has NO module-level `config` attribute — config is imported lazily inside functions — so patch the singleton: `from ccgram.config import config; monkeypatch.setattr(config, "claude_projects_path", ...)`, matching test_transcript_discovery_key.py's idiom)

```python
async def test_path_kind_registers_pi_transcript(monkeypatch, tmp_path) -> None:
    transcript = tmp_path / "1234_abcd.jsonl"
    transcript.write_text("{}\n")
    _stub_native_session(monkeypatch, AgentSessionRef(kind="path", value=str(transcript), agent="pi"))
    native = await transcript_discovery._native_session_transcript("w2:t1", "/proj")
    assert native == (transcript, "abcd")  # pi stem: "<ts>_<session id>"


async def test_id_kind_resolves_via_cwd_encoding(monkeypatch, tmp_path) -> None:
    projects = tmp_path / "projects"
    (projects / "-home-u-proj").mkdir(parents=True)
    transcript = projects / "-home-u-proj" / "sid-1.jsonl"
    transcript.write_text("{}\n")
    monkeypatch.setattr(config, "claude_projects_path", projects)  # the SINGLETON
    _stub_native_session(monkeypatch, AgentSessionRef(kind="id", value="sid-1", agent="claude"))
    native = await transcript_discovery._native_session_transcript("w2:t1", "/home/u/proj")
    assert native == (transcript, "sid-1")


async def test_id_kind_glob_fallback_when_cwd_drifted(monkeypatch, tmp_path) -> None:
    ...  # transcript under projects/"-other-dir"/sid-1.jsonl; cwd="/home/u/proj" → still found


async def test_missing_transcript_falls_through(monkeypatch, tmp_path) -> None:
    # Isolate the projects path too — otherwise the glob reads the REAL
    # ~/.claude/projects of the machine running the suite.
    monkeypatch.setattr(config, "claude_projects_path", tmp_path / "empty")
    _stub_native_session(monkeypatch, AgentSessionRef(kind="id", value="ghost", agent="claude"))
    assert await transcript_discovery._native_session_transcript("w2:t1", "/proj") is None


async def test_register_native_session_dedupes_recorded_path(monkeypatch, tmp_path) -> None:
    calls: list = []
    monkeypatch.setattr(
        transcript_discovery.session_map_sync, "register_hookless_session",
        lambda **kw: calls.append(kw),
    )
    # Guard BOTH write paths: a dedupe regression must not reach the real
    # file-locked write in a thread.
    monkeypatch.setattr(
        transcript_discovery.session_map_sync, "write_hookless_session_map",
        lambda **kw: calls.append(kw),
    )
    identity = _identity_with(transcript_path=tmp_path / "t.jsonl")  # Path, not str
    await transcript_discovery._register_native_session(
        "w2:t1", identity, (tmp_path / "t.jsonl", "sid"), cwd="/proj"
    )
    assert calls == []  # already recorded → no duplicate write on either path


async def test_consult_first_beats_stale_hook_transcript(monkeypatch, tmp_path) -> None:
    """End-to-end consult-first order through discover_and_register_transcript:
    a stale hook transcript_path must lose to the native session, and the
    legacy fallback discovery must not run at all."""
    fresh = tmp_path / "fresh.jsonl"
    fresh.write_text("{}\n")
    _stub_native_session(monkeypatch, AgentSessionRef(kind="path", value=str(fresh), agent="pi"))
    ...  # identity: provider set, transcript_path=tmp_path/"stale.jsonl" (does
    # not exist); stub register_hookless_session/write_hookless_session_map to
    # record calls; monkeypatch _find_and_register_transcript with a spy;
    # run discover_and_register_transcript(window_id); assert both write stubs
    # got the fresh path and the fallback spy was NOT called
```

(`_stub_native_session` sets `capabilities.native_agent_session=True` and `agent_session` to return the ref; `_identity_with` builds the `IdentityProjection` per the neighboring test file's helper.)

- [ ] **Step 2: Run**

Run: `uv run pytest tests/ccgram/handlers/recovery/test_native_session_discovery.py -v`
Expected: PASS. Any failure here is a real regression in the ef0b5ac flow — investigate the code, don't bend the test.

- [ ] **Step 3: Commit**

```bash
git add tests/ccgram/handlers/recovery/test_native_session_discovery.py
git commit -m "test(recovery): cover the native agent-session discovery positive paths"
```

---

### Task 15: sync the architecture rules + final gate

**Files:**
- Modify: `.claude/rules/architecture.md` (herdr bullet in `multiplexer/`)

- [ ] **Step 1: Update the stale claims in the herdr bullet**

- "pins `HERDR_PROTOCOL_VERSION` from `herdr status` and refuses on mismatch (`HerdrProtocolError`)" → "verifies the protocol from `herdr status` (supported: 14–17); unverified protocols warn and continue".
- "skips workspace/tab labels matching `^__.*__$` so ccgram never auto-adopts itself" → "marks workspace/tab labels matching `__*__`/`fm-*` as `WindowRef.internal`; `list_windows` (discovery) filters them, `list_windows_for_reconciliation` (liveness truth for prune/status) returns them all".
- Add one sentence about the send-keys translation layer (tmux tokens → kitty-style names) and the `window_id` containment guard parity with tmux.

- [ ] **Step 2: Full gate**

Run: `env -u HERDR_SOCKET_PATH make check` — with the socket var set, `test-integration`'s `-m "not llm" -n auto` would run the live-mutating herdr lane in parallel xdist; unset it so the non-live gate stays hermetic.
Expected: all green. `make check` starts with mutating `ruff format`: check `git status --porcelain` afterwards — any reformat must be folded into the owning commits (fixup), never left dangling for the doc-only commit.
Then run the live herdr lane exactly once, serially: `uv run pytest tests/integration/ -m herdr -v -rs`.
Expected: green, AND the `-rs` summary shows the herdr tests actually RAN — a silently skipped lane proves nothing.

- [ ] **Step 3: Commit**

```bash
git add .claude/rules/architecture.md
git commit -m "docs: sync architecture rules with herdr hardening changes"
```

---

## Out of scope (deliberately)

- **Reordering or merging Tasks 3/4** (raised in the Codex review, refuted on verification): commit-by-commit the original order is monotonically safe — Task 3 alone strictly narrows today's exposure (transient failures stop mass-banner), and the residual shape-drift hole is pre-existing until Task 4 closes it. The reverse order would itself create a regressive intermediate commit: Task-4-only makes `_workspace_labels` failures propagate `None`, which the unchanged poller's `list_windows()` coerces to `[]` — a workspace-list hiccup that today merely degrades labels would mass-dead-banner every topic.
- **tmux hidden-window (`_`-prefix) parity**: tmux's reconciliation listing filters hidden windows the same way herdr filtered `fm-*` — same defect class, tmux-side, untouched here. Follow-up candidate.
- **Stronger crewmate detection than the 3-char `fm-` label prefix** (e.g. herdr agent metadata): needs herdr-side support; the label filter now only affects discovery, so a collision costs a missing topic, not a destroyed binding.
- **`agent_status_cache` TTL**: rejected in favor of reprime eviction (Task 7) — a TTL would reintroduce per-tick subprocess churn the push stream exists to remove.
- **`ccgram doctor` event-stream reachability check**: nice-to-have on top of Task 11's warning; skipped to keep the diff surgical.

## Findings → tasks traceability

| Finding (review 2026-07-26) | Task |
|---|---|
| fm-*/__*__ filter feeds destructive prune [high] | 1, 2 |
| Bound fm-* tabs destroyed as dead [medium] | 1, 2 |
| Hook adoption bypasses the filter [medium] | 2 |
| fm-* filter untested [medium] | 1 |
| Poller mass-death on transient listing failure [high] | 3 |
| `_tab_list` [] on shape drift [medium] | 4 |
| `_workspace_labels` {} on failure [medium] | 4 |
| Key tokens never delivered [high] | 5 |
| Agent-exit-to-shell never detected [high] | 6 |
| Stale push-status cache never evicted [medium] | 7 |
| events.subscribe failure treated as success [medium] | 8 |
| `open_socket_stream` untested [medium] | 8 |
| `window_id` containment guard dropped [medium] | 9 |
| Stale TMUX_PANE hijacks hook identity [medium] | 10 |
| Event stream dead without HERDR_SOCKET_PATH [medium] | 11 |
| Blocking projects scan on event loop [low] | 12 |
| F2 omits reconciliation/workspace/agent-session methods [low] | 13 |
| ef0b5ac discovery zero positive tests [medium] | 14 |
