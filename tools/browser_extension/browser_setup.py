"""
browser_setup: full browser setup conversation flow.

Entry point: ensure_connected(site_id, config, narration) -> bool

Flow:
 1. If site already has a saved browser+profile → skip detection, go to launch.
 2. Detect installed browsers cross-platform.
 3. If none (or only unsupported) → narrate, return False.
 4. Auto-select if one browser; ask if multiple (15s listen window).
 5. Pick profile via history inference or last-used; narrate the choice.
 6. RACE: launch browser (background task) vs 10s listen for override.
    - Override arrives before connect → cancel_active_launch, relaunch with new profile.
    - Connects before override → done.
    - Listen times out, no override → proceed with original profile.
 7. On connect → persist site_profiles + installed_profiles in SQLite, return True.
 8. On timeout → narrate failure, return False (Playwright fallback).

Callers:
  Async context  → await ensure_connected(...)
  Worker thread  → use ensure_browser_connected_sync() from __init__.py
"""
import asyncio
import re
from loguru import logger

from core import personality
from tools.web_engine import store
from . import browser_detector, browser_launcher, connection_manager

# Seconds to listen for a profile override while Chrome is launching
_PROFILE_OVERRIDE_TIMEOUT = 10
# Seconds to listen for browser choice when multiple browsers found
_BROWSER_CHOICE_TIMEOUT   = 15
# Small delay (s) before recording to let TTS narration finish speaking
_NARRATION_SETTLE         = 2.5

# Words that mean "keep the default / I'm fine with it"
_CONFIRM_WORDS = frozenset([
    "default", "fine", "good", "ok", "okay", "yes", "that", "this",
    "proceed", "continue", "go ahead", "keep", "whatever", "it's fine",
])


# ── Public entry point ─────────────────────────────────────────────────────────


async def ensure_connected(
    site_id: str,
    config:  dict,
    narration,
    recorder=None,
    transcriber=None,
) -> bool:
    """
    Ensure the browser extension is connected for site_id.
    Returns True if connected (either already was, or just connected via setup).
    Returns False if setup failed — caller should fall back to Playwright.
    """
    logger.info("[BrowserSetup] ═══ ensure_connected: site_id={!r} ═══", site_id)

    if connection_manager.is_connected():
        logger.info("[BrowserSetup] Extension already connected — nothing to do")
        return True

    return await _run_setup(site_id, config, narration, recorder, transcriber)


# ── Main setup flow ────────────────────────────────────────────────────────────


async def _run_setup(site_id, config, narration, recorder, transcriber) -> bool:
    # ── Step 1: Known site → skip detection ──────────────────────────────────
    saved = store.get_site_profile(site_id)
    if saved:
        browser = saved["browser"]
        profile = saved["profile"]
        logger.info("[BrowserSetup] Step 1: saved profile found browser={!r} profile={!r}", browser, profile)
        profiles = browser_detector.list_profiles(browser)
        return await _launch_race(site_id, browser, profile, profiles, config, narration, recorder, transcriber)

    logger.info("[BrowserSetup] Step 1: no saved profile — running full detection")

    # ── Step 2: Detect browsers ───────────────────────────────────────────────
    browsers  = browser_detector.detect_installed()
    supported = [b for b in browsers if b["support"] == browser_detector.SUPPORTED]
    unsupported_only = browsers and not supported

    logger.info("[BrowserSetup] Step 2: detected={} supported={} unsupported_only={}",
                [b["browser"] for b in browsers],
                [b["browser"] for b in supported],
                unsupported_only)

    # ── Step 3: No supported browser ─────────────────────────────────────────
    if not supported:
        if unsupported_only:
            msg = personality.say("browser_unsupported_only")
            logger.info("[BrowserSetup] Step 3: only unsupported browsers → {!r}", msg)
        else:
            msg = personality.say("browser_none")
            logger.info("[BrowserSetup] Step 3: no browsers at all → {!r}", msg)
        narration.say(msg)
        return False

    # ── Step 4: Select browser ────────────────────────────────────────────────
    if len(supported) == 1:
        browser_info = supported[0]
        msg = personality.say("browser_found_one").format(browser=browser_info["display_name"])
        logger.info("[BrowserSetup] Step 4: single browser auto-selected={!r} msg={!r}",
                    browser_info["browser"], msg)
        narration.say(msg)
    else:
        logger.info("[BrowserSetup] Step 4: multiple browsers — asking user")
        browser_info = await _ask_browser_choice(supported, narration, recorder, transcriber)
        if not browser_info:
            browser_info = supported[0]
            logger.info("[BrowserSetup] Step 4: no clear choice — defaulting to {!r}", browser_info["browser"])

    browser  = browser_info["browser"]
    profiles = browser_info["profiles"]
    logger.info("[BrowserSetup] Step 4: browser={!r} profiles={}", browser, [p["name"] for p in profiles])

    # ── Step 5: Pick initial profile ──────────────────────────────────────────
    domain       = site_id.split("/")[0] if site_id else ""
    best_profile = None

    if len(profiles) > 1 and domain:
        logger.debug("[BrowserSetup] Step 5: inferring profile from history for domain={!r}", domain)
        best_profile = browser_detector.infer_profile_for_domain(browser, domain)
        logger.info("[BrowserSetup] Step 5: history inference → profile={!r}", best_profile)

    if not best_profile and profiles:
        best_profile = profiles[0]["name"]
        logger.info("[BrowserSetup] Step 5: no history match — using most-recent profile={!r}", best_profile)

    # ── Step 5b: Narrate profile choice ──────────────────────────────────────
    if profiles and len(profiles) > 1:
        profile_display = next(
            (p["display_name"] for p in profiles if p["name"] == best_profile),
            best_profile or "Default",
        )
        msg = personality.say("browser_profile_selected").format(profile=profile_display)
        logger.info("[BrowserSetup] Step 5: narrating profile choice={!r} msg={!r}", profile_display, msg)
        narration.say(msg)
    else:
        msg = personality.say("browser_launching").format(browser=browser_info["display_name"])
        narration.say(msg)
        logger.info("[BrowserSetup] Step 5: single/no profile — narrating launch: {!r}", msg)

    # ── Step 6: Race launch vs profile override ────────────────────────────────
    return await _launch_race(site_id, browser, best_profile, profiles, config, narration, recorder, transcriber)


# ── Launch race (the core asyncio.wait pattern) ────────────────────────────────


async def _launch_race(site_id, browser, profile, profiles, config, narration, recorder, transcriber) -> bool:
    """
    Start browser launch and 10s listen concurrently.
    Whichever finishes first determines what happens next.
    """
    logger.info("[BrowserSetup] _launch_race: browser={!r} profile={!r}", browser, profile)

    installed = store.is_profile_installed(browser, profile or "Default")
    logger.info("[BrowserSetup] Extension installed in this profile: {}", installed)

    if installed:
        launch_coro = browser_launcher.open_browser(browser, profile, config)
    else:
        launch_coro = browser_launcher.install_and_open_browser(browser, profile, config)

    launch_task = asyncio.create_task(launch_coro, name="browser_launch")

    # Only listen for override if we have recorder + transcriber + multiple profiles
    can_listen = recorder is not None and transcriber is not None and len(profiles) > 1
    logger.info("[BrowserSetup] can_listen_for_override={} (recorder={} transcriber={} profiles={})",
                can_listen, recorder is not None, transcriber is not None, len(profiles))

    if can_listen:
        listen_coro  = _listen_for_override(profiles, recorder, transcriber)
        listen_task  = asyncio.create_task(listen_coro, name="profile_listen")
        all_tasks    = {launch_task, listen_task}
    else:
        listen_task = None
        all_tasks   = {launch_task}

    done, pending = await asyncio.wait(all_tasks, return_when=asyncio.FIRST_COMPLETED)
    logger.debug("[BrowserSetup] asyncio.wait done: {} pending: {}",
                 [t.get_name() for t in done], [t.get_name() for t in pending])

    # ── Check for profile override ────────────────────────────────────────────
    override_profile: str | None = None
    if listen_task and listen_task in done:
        try:
            override_profile = listen_task.result()
        except Exception as exc:
            logger.debug("[BrowserSetup] listen_task raised (ignored): {}", exc)
        logger.info("[BrowserSetup] listen_task done first: override_profile={!r}", override_profile)

    if override_profile:
        # Cancel current launch and relaunch with new profile
        logger.info("[BrowserSetup] Override received! Cancelling current launch and relaunching with {!r}", override_profile)
        launch_task.cancel()
        await browser_launcher.cancel_active_launch()
        if listen_task and not listen_task.done():
            listen_task.cancel()

        narration.say(personality.say("browser_profile_changed"))
        logger.info("[BrowserSetup] Relaunching with override profile={!r}", override_profile)

        installed2 = store.is_profile_installed(browser, override_profile)
        if installed2:
            success = await browser_launcher.open_browser(browser, override_profile, config)
        else:
            success = await browser_launcher.install_and_open_browser(browser, override_profile, config)

        final_profile = override_profile
    else:
        # No override — cancel listen task if still running, await launch result
        if listen_task and not listen_task.done():
            listen_task.cancel()
            logger.debug("[BrowserSetup] Cancelled listen_task (launch won the race)")

        try:
            success = await launch_task
        except asyncio.CancelledError:
            logger.warning("[BrowserSetup] launch_task was cancelled — treating as failure")
            success = False

        final_profile = profile
        logger.info("[BrowserSetup] Launch completed: success={} final_profile={!r}", success, final_profile)

    # ── Post-connect persistence ──────────────────────────────────────────────
    if success:
        logger.info("[BrowserSetup] Connected! Persisting: browser={!r} profile={!r}", browser, final_profile)
        if site_id:
            store.save_site_profile(site_id, browser, final_profile or "Default")
        store.mark_profile_installed(browser, final_profile or "Default")
        store.mark_profile_connected(browser, final_profile or "Default")
        narration.say(personality.say("browser_connected"))
    else:
        logger.warning("[BrowserSetup] Browser setup failed — returning False for Playwright fallback")
        narration.say(personality.say("browser_launch_failed"))

    logger.info("[BrowserSetup] ═══ ensure_connected → {} ═══", success)
    return success


# ── Listen helpers ─────────────────────────────────────────────────────────────


async def _ask_browser_choice(supported, narration, recorder, transcriber) -> dict | None:
    """
    Narrate the list of browsers and listen for the user's choice.
    Returns the chosen browser dict or None (caller picks the first one).
    """
    browsers_str = ", ".join(b["display_name"] for b in supported)
    base_msg     = personality.say("browser_found_many")
    full_msg     = f"{base_msg} {browsers_str}?"
    logger.info("[BrowserSetup] Asking browser choice: {!r}", full_msg)
    narration.say(full_msg)

    if recorder is None or transcriber is None:
        logger.debug("[BrowserSetup] No recorder/transcriber — cannot listen for browser choice")
        return None

    await asyncio.sleep(_NARRATION_SETTLE)
    logger.debug("[BrowserSetup] Recording browser choice ({}s) …", _BROWSER_CHOICE_TIMEOUT)

    try:
        audio = await recorder.record(max_wait_sec=_BROWSER_CHOICE_TIMEOUT)
    except Exception as exc:
        logger.warning("[BrowserSetup] recorder.record() failed: {}", exc)
        return None

    if not audio or len(audio) == 0:
        logger.info("[BrowserSetup] No audio for browser choice — defaulting to first")
        return None

    try:
        text = await transcriber.transcribe(audio)
    except Exception as exc:
        logger.warning("[BrowserSetup] transcribe failed: {}", exc)
        return None

    logger.info("[BrowserSetup] Browser choice transcription: {!r}", text)
    text_lower = text.lower()

    for b in supported:
        if b["browser"] in text_lower or b["display_name"].lower() in text_lower:
            logger.info("[BrowserSetup] Matched browser: {!r}", b["browser"])
            return b

    logger.info("[BrowserSetup] Could not match browser from {!r} — returning None", text)
    return None


async def _listen_for_override(profiles, recorder, transcriber) -> str | None:
    """
    Record for _PROFILE_OVERRIDE_TIMEOUT seconds (with a short settle delay first).
    Parse the transcription for a profile name reference.
    Returns a profile directory name string, or None (keep the current profile).
    """
    await asyncio.sleep(_NARRATION_SETTLE)
    logger.debug("[BrowserSetup] Listening for profile override ({}s) …", _PROFILE_OVERRIDE_TIMEOUT)

    try:
        audio = await recorder.record(max_wait_sec=_PROFILE_OVERRIDE_TIMEOUT)
    except Exception as exc:
        logger.warning("[BrowserSetup] recorder.record() failed during override listen: {}", exc)
        return None

    if not audio or len(audio) == 0:
        logger.info("[BrowserSetup] No audio during override window — keeping current profile")
        return None

    try:
        text = await transcriber.transcribe(audio)
    except Exception as exc:
        logger.warning("[BrowserSetup] transcribe failed during override listen: {}", exc)
        return None

    logger.info("[BrowserSetup] Override transcription: {!r}", text)
    text_lower = text.lower().strip()

    # User confirmed the default
    if any(w in text_lower for w in _CONFIRM_WORDS):
        logger.info("[BrowserSetup] User confirmed default profile")
        return None

    # Try to match a profile by display name or directory name
    for p in profiles:
        if p["display_name"].lower() in text_lower or p["name"].lower() in text_lower:
            logger.info("[BrowserSetup] Override matched profile display_name={!r} → {!r}", p["display_name"], p["name"])
            return p["name"]

    # "profile 2", "second profile", "number 2" etc.
    m = re.search(r"profile\s+(\d+)", text_lower)
    if m:
        target = f"Profile {m.group(1)}"
        if any(p["name"] == target for p in profiles):
            logger.info("[BrowserSetup] Override matched by number: {!r}", target)
            return target

    # "work", "personal", "home", "office" as partial display name matches
    for p in profiles:
        name_words = p["display_name"].lower().split()
        if any(w in text_lower for w in name_words if len(w) > 3):
            logger.info("[BrowserSetup] Override partial match: {!r} → {!r}", p["display_name"], p["name"])
            return p["name"]

    logger.info("[BrowserSetup] Could not match profile from {!r} — keeping current", text)
    return None
