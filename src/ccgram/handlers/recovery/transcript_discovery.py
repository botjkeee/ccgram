"""Transcript discovery for hookless providers.

Discovers and registers transcripts for providers without hook support
(Codex, Gemini). Also handles provider auto-detection from pane process
and shell ↔ agent transitions.

On ``native_agent_session`` backends the multiplexer is asked first: it
tracks the live agent session, so it is right where the two fallbacks are
blind — resume (hook reports an id whose transcript is never written) and
the start-up race (discovery skips a transcript whose header is not on disk
yet and settles on an older session for the same cwd).

Key components:
  - discover_and_register_transcript: main discovery function called per topic
  - _native_session_transcript: multiplexer-reported session, preferred source
  - _detect_and_apply_provider: provider auto-detection from running process
  - _find_and_register_transcript: transcript search for hookless providers
"""

import asyncio
from typing import TYPE_CHECKING

import structlog

from ...providers import (
    detect_provider_from_pane,
    detect_provider_from_runtime,
    detect_provider_from_transcript_path,
    get_cached_foreground_pgid,
    get_provider_for_window,
    should_probe_pane_title_for_provider_detection,
)
from ...session import session_manager
from ...session_map import session_map_prefix, session_map_sync
from ...telegram_client import TelegramClient
from ...multiplexer import multiplexer as tmux_manager
from ...window_state_ports import identity_state

if TYPE_CHECKING:
    from pathlib import Path

    from ...providers.base import AgentProvider
    from ...multiplexer.base import WindowRef as TmuxWindow

logger = structlog.get_logger()


def _transcript_for_session_id(session_id: str, cwd: str) -> "Path | None":
    """Resolve a bare session id to its transcript file, or None.

    Mirrors ``SessionResolver._build_session_file_path`` (cwd encoded with
    ``/`` → ``-``) and falls back to a glob when the window's recorded cwd has
    drifted from the one the transcript was filed under.
    """
    # Lazy: config pulls the env/.env layer; this module is imported by the
    # polling graph, which must stay import-light.
    from ...config import config

    if not session_id:
        return None
    if cwd:
        direct = (
            config.claude_projects_path / cwd.replace("/", "-") / f"{session_id}.jsonl"
        )
        if direct.exists():
            return direct
    matches = list(config.claude_projects_path.glob(f"*/{session_id}.jsonl"))
    return matches[0] if matches else None


async def _native_session_transcript(
    window_id: str, cwd: str
) -> "tuple[Path, str] | None":
    """Return ``(transcript, session_id)`` reported by the multiplexer, or None.

    None on backends without ``native_agent_session``, when the pane runs no
    agent, or when the reported transcript is not on disk yet — every case
    falls through to hooks and per-provider discovery.
    """
    if not tmux_manager.capabilities.native_agent_session:
        return None
    ref = await tmux_manager.agent_session(window_id)
    if ref is None:
        return None
    # Lazy: keep pathlib next to its only use, matching this module's shape.
    from pathlib import Path

    if ref.kind == "path":
        path = Path(ref.value)
    else:
        path = _transcript_for_session_id(ref.value, cwd)
    if path is None or not path.exists():
        return None
    # Transcript stems are either the bare session id (claude) or
    # "<timestamp>_<session id>" (pi); rpartition handles both.
    session_id = ref.value if ref.kind == "id" else path.stem.rpartition("_")[2]
    return path, (session_id or path.stem)


async def _refresh_identity_from_pane(
    window_id: str,
    identity: identity_state.IdentityProjection,
    w: "TmuxWindow | None",
    *,
    client: TelegramClient | None,
    chat_id: int,
    thread_id: int,
) -> "tuple[identity_state.IdentityProjection, bool] | None":
    """Re-detect the provider from the pane; return ``(identity, restarted)``.

    ``None`` means the window vanished mid-detection and the caller should
    abandon discovery for this tick.
    """
    if not (w and w.pane_current_command):
        return identity, False

    pgid_before = get_cached_foreground_pgid(window_id)
    await _detect_and_apply_provider(
        window_id, identity, w, client=client, chat_id=chat_id, thread_id=thread_id
    )
    refreshed = identity_state.get_identity(window_id)
    if refreshed is None:
        return None
    process_restarted = _foreground_process_restarted(
        before_pgid=pgid_before,
        after_pgid=get_cached_foreground_pgid(window_id),
        old_identity=identity,
        new_identity=refreshed,
    )
    return refreshed, process_restarted


async def _register_native_session(
    window_id: str,
    identity: identity_state.IdentityProjection,
    native: "tuple[Path, str]",
    *,
    cwd: str,
) -> None:
    """Persist a multiplexer-reported session, unless it is already recorded."""
    native_path, native_session_id = native
    if str(native_path) == str(identity.transcript_path or ""):
        return
    provider_name = identity.provider_name or ""
    session_map_sync.register_hookless_session(
        window_id=window_id,
        session_id=native_session_id,
        cwd=cwd,
        transcript_path=str(native_path),
        provider_name=provider_name,
    )
    await asyncio.to_thread(
        session_map_sync.write_hookless_session_map,
        window_id=window_id,
        session_id=native_session_id,
        cwd=cwd,
        transcript_path=str(native_path),
        provider_name=provider_name,
    )
    logger.info(
        "Registered native session: %s -> session_id=%s, transcript=%s",
        window_id,
        native_session_id,
        native_path,
    )


def _session_id_already_bound(session_id: str, window_id: str) -> bool:
    """Return True if another currently bound window already uses ``session_id``."""
    # Lazy: thread_router may not be installed in some test paths; fail open
    # if it isn't available so discovery can still continue with this window.
    from ...thread_router import thread_router

    try:
        iterator = thread_router.iter_thread_bindings()
    except RuntimeError:
        return False

    for _user_id, _thread_id, bound_window_id in iterator:
        if bound_window_id == window_id:
            continue
        if identity_state.get_session_id(bound_window_id) == session_id:
            return True
    return False


async def _detect_and_apply_provider(
    window_id: str,
    identity: identity_state.IdentityProjection,
    w: "TmuxWindow",
    *,
    client: TelegramClient | None = None,
    chat_id: int = 0,
    thread_id: int = 0,
) -> None:
    """Detect provider from pane process and apply transitions."""
    if identity_state.is_provider_manually_overridden(window_id):
        return
    detected = await detect_provider_from_pane(
        w.pane_current_command, window_id=window_id
    )
    if not detected and should_probe_pane_title_for_provider_detection(
        w.pane_current_command
    ):
        pane_title = await tmux_manager.get_pane_title(window_id)
        detected = detect_provider_from_runtime(
            w.pane_current_command,
            pane_title=pane_title,
        )

    if detected and detected != identity.provider_name:
        old_provider = identity.provider_name
        session_manager.set_window_provider(window_id, detected, cwd=w.cwd or None)
        # Lazy: providers/__init__.py reaches back into transcript code
        # via provider format modules.
        from ...providers import get_provider_for_window

        new_caps = get_provider_for_window(window_id, detected)
        old_caps = (
            get_provider_for_window(window_id, old_provider) if old_provider else None
        )
        if new_caps and new_caps.capabilities.chat_first_command_path:
            identity_state.clear_transcript_path(window_id)
            # Lazy: shell.shell_prompt_orchestrator hits the recovery
            # subpackage's discovery code via send-keys callbacks.
            from ..shell.shell_prompt_orchestrator import ensure_setup

            await ensure_setup(
                window_id,
                "provider_switch",
                client=client,
                chat_id=chat_id,
                thread_id=thread_id,
            )
        elif old_caps and old_caps.capabilities.chat_first_command_path:
            # Lazy: same shell ↔ recovery cycle as above.
            from ..shell.shell_capture import clear_shell_monitor_state

            # Lazy: same shell ↔ recovery cycle as above.
            from ..shell.shell_prompt_orchestrator import (
                clear_state as clear_orchestrator,
            )

            clear_shell_monitor_state(window_id)
            clear_orchestrator(window_id)
    elif not detected and identity.transcript_path:
        inferred = detect_provider_from_transcript_path(str(identity.transcript_path))
        if inferred and inferred != identity.provider_name:
            session_manager.set_window_provider(window_id, inferred, cwd=w.cwd or None)


def _resolve_providers_to_try(
    window_id: str,
    identity: identity_state.IdentityProjection,
    w: "TmuxWindow | None",
) -> list[tuple[str, "AgentProvider"]] | None:
    """Determine which providers to probe for transcripts.

    Returns a list of (name, provider) pairs, or ``None`` to signal the
    caller should set up a shell provider.
    """
    # Lazy: hoisting forms polling/__init__ → window_tick →
    # recovery.transcript_discovery → polling_state partial-init
    # cycle (worker-order-dependent; verified during F6.2). polling_types
    # is leaf-level — Task 5 of Round 5 may hoist this once cycle test covers it.
    # Lazy: polling_types is leaf-pure; importing here at module load would touch the polling subpackage __init__
    from ..polling.polling_types import is_shell_prompt

    # Lazy: providers registry reaches back through transcripts
    from ...providers import registry

    if identity.provider_name:
        provider = get_provider_for_window(window_id, identity.provider_name)
        if provider.capabilities.chat_first_command_path:
            return []
        return [(provider.capabilities.name, provider)]

    if w and is_shell_prompt(w.pane_current_command):
        return None  # signals caller to set up shell

    return [
        (name, registry.get(name))
        for name in registry.provider_names()
        if not registry.get(name).capabilities.supports_hook and name != "shell"
    ]


async def _find_and_register_transcript(
    window_id: str,
    identity: identity_state.IdentityProjection,
    providers_to_try: list[tuple[str, "AgentProvider"]],
    pane_alive: bool,
) -> None:
    """Search for transcripts among candidate providers and register if found."""
    window_key = f"{session_map_prefix()}{window_id}"

    transcript_path_str = (
        str(identity.transcript_path) if identity.transcript_path else ""
    )

    for provider_name, provider in providers_to_try:
        max_age = 0 if pane_alive else None
        event = await asyncio.to_thread(
            provider.discover_transcript,
            identity.cwd,
            window_key,
            max_age=max_age,
        )
        if not event:
            continue

        if _session_id_already_bound(event.session_id, window_id):
            logger.debug(
                "Skipping discover result for window %s: session_id %s already bound",
                window_id,
                event.session_id,
            )
            continue

        if (
            identity.session_id == event.session_id
            and transcript_path_str == event.transcript_path
            and identity.provider_name == provider_name
        ):
            return

        session_map_sync.register_hookless_session(
            window_id=window_id,
            session_id=event.session_id,
            cwd=event.cwd,
            transcript_path=event.transcript_path,
            provider_name=provider_name,
        )
        await asyncio.to_thread(
            session_map_sync.write_hookless_session_map,
            window_id=window_id,
            session_id=event.session_id,
            cwd=event.cwd,
            transcript_path=event.transcript_path,
            provider_name=provider_name,
        )
        return


def _hook_already_resolved(
    window_id: str, identity: identity_state.IdentityProjection
) -> bool:
    """True when a hookful provider has already populated transcript_path."""
    if not identity.provider_name:
        return False
    provider = get_provider_for_window(window_id, identity.provider_name)
    return bool(provider.capabilities.supports_hook and identity.transcript_path)


def _foreground_process_restarted(
    *,
    before_pgid: int,
    after_pgid: int,
    old_identity: identity_state.IdentityProjection,
    new_identity: identity_state.IdentityProjection,
) -> bool:
    """True when the same provider is running in a new foreground process group."""
    return bool(
        before_pgid
        and after_pgid
        and before_pgid != after_pgid
        and old_identity.session_id
        and old_identity.provider_name
        and old_identity.provider_name == new_identity.provider_name
    )


async def _switch_to_shell(
    window_id: str,
    *,
    client: TelegramClient | None,
    chat_id: int,
    thread_id: int,
) -> None:
    """Provider-switch to shell and clear transcript bookkeeping."""
    session_manager.set_window_provider(window_id, "shell")
    identity_state.clear_transcript_path(window_id)
    # Lazy: same shell ↔ recovery cycle as _detect_and_apply_provider.
    from ..shell.shell_prompt_orchestrator import ensure_setup

    await ensure_setup(
        window_id,
        "provider_switch",
        client=client,
        chat_id=chat_id,
        thread_id=thread_id,
    )


async def discover_and_register_transcript(
    window_id: str,
    *,
    _window: "TmuxWindow | None" = None,
    client: TelegramClient | None = None,
    user_id: int = 0,
    thread_id: int = 0,
) -> None:
    """Discover and register transcript for hookless providers (Codex, Gemini).

    Also handles provider auto-detection from pane process name
    and shell ↔ agent transitions with prompt marker setup.
    """
    # Lazy: same polling/__init__ cycle as _resolve_providers_to_try.
    from ..polling.polling_types import is_shell_prompt

    # Lazy: thread_router proxy resolved when transcript discovery is invoked
    from ...thread_router import thread_router

    identity = identity_state.get_identity(window_id)
    if identity is None:
        return

    chat_id = thread_router.resolve_chat_id(user_id, thread_id) if user_id else 0

    w = _window or await tmux_manager.find_window_by_id(window_id)

    refresh = await _refresh_identity_from_pane(
        window_id, identity, w, client=client, chat_id=chat_id, thread_id=thread_id
    )
    if refresh is None:
        return
    identity, process_restarted = refresh

    # Preferred source: the multiplexer's own view of the live session. Checked
    # before the hook short-circuit below, because the hook's transcript_path is
    # exactly what goes stale on resume.
    native = await _native_session_transcript(
        window_id, identity.cwd or (w.cwd if w else "")
    )
    if native is not None:
        await _register_native_session(
            window_id, identity, native, cwd=identity.cwd or (w.cwd if w else "")
        )
        return

    if _hook_already_resolved(window_id, identity) and not process_restarted:
        return

    if not identity.cwd:
        if not w or not w.cwd:
            return
        session_manager.set_window_provider(
            window_id, identity.provider_name or "", cwd=w.cwd
        )
        refreshed = identity_state.get_identity(window_id)
        if refreshed is None:
            return
        identity = refreshed

    providers_to_try = _resolve_providers_to_try(window_id, identity, w)
    if providers_to_try is None:
        await _switch_to_shell(
            window_id, client=client, chat_id=chat_id, thread_id=thread_id
        )
        return
    if not providers_to_try:
        return

    pane_alive = w is not None and not is_shell_prompt(w.pane_current_command)
    await _find_and_register_transcript(
        window_id, identity, providers_to_try, pane_alive
    )
