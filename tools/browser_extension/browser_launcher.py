"""
browser_launcher: launch a browser with the JARVIS extension, cross-platform.

Supports: Chrome, Edge (registry force-install), Firefox (policies.json), Brave.
A module-level _active_proc handle lets browser_setup.py cancel a launch-in-progress
and relaunch with a different profile via cancel_active_launch().

Public async API:
  open_browser(browser, profile, config)         -> bool
  install_and_open_browser(browser, profile, config) -> bool
  cancel_active_launch()                         -> None   (async)
"""
import asyncio
import json
import os
import platform
import subprocess
from pathlib import Path

from loguru import logger

from . import connection_manager
from . import browser_detector

# ── Module state ───────────────────────────────────────────────────────────────

_active_proc: subprocess.Popen | None = None   # browser process being waited on

# Registry key roots for Chromium-family browsers
_FORCELIST_KEYS: dict[str, str] = {
    "chrome": r"SOFTWARE\Policies\Google\Chrome\ExtensionInstallForcelist",
    "edge":   r"SOFTWARE\Policies\Microsoft\Edge\ExtensionInstallForcelist",
    "brave":  r"SOFTWARE\Policies\BraveSoftware\Brave\ExtensionInstallForcelist",
}

# ── Public async API ───────────────────────────────────────────────────────────


async def open_browser(browser: str, profile: str | None, config: dict, narration=None, domain: str | None = None) -> bool:
    """
    Ensure the browser is open with the JARVIS extension connected.

    - If already connected: return True immediately.
    - If profile is None: auto-detect profile.  If domain is given, picks the
      profile that most recently visited domain (infer_profile_for_domain).
      Falls back to the most recently used profile overall.
    - If browser is running without the extension: narrate and wait for user
      to close it (can't inject --load-extension into a running instance).
    """
    logger.info("[Launcher] open_browser: browser={!r} profile={!r} domain={!r}", browser, profile, domain)

    if connection_manager.is_connected():
        logger.info("[Launcher] Extension already connected — nothing to open")
        return True

    # Auto-detect profile when not specified
    if profile is None and browser in ("chrome", "edge", "brave", "firefox"):
        profiles = browser_detector.list_profiles(browser)
        if domain and len(profiles) > 1:
            profile = browser_detector.infer_profile_for_domain(browser, domain)
            if profile:
                logger.info("[Launcher] Profile inferred from domain {!r}: {!r}", domain, profile)
        if not profile:
            profile = profiles[0]["name"] if profiles else None
            if profile:
                logger.info("[Launcher] Auto-detected profile (most recent): {!r}", profile)

    cfg     = config.get("browser_extension", {})
    timeout = cfg.get("open_timeout_seconds", 30)
    return await _launch_and_wait(browser, profile, config, timeout, narration=narration)


async def install_and_open_browser(browser: str, profile: str | None, config: dict, narration=None) -> bool:
    """
    Write force-install policy for the extension (registry on Windows for Chromium,
    policies.json for Firefox), then launch the browser.
    Returns True if extension connects within timeout.
    """
    logger.info("[Launcher] install_and_open_browser: browser={!r} profile={!r}", browser, profile)

    cfg        = config.get("browser_extension", {})
    ext_id     = cfg.get("extension_id",     "")
    update_url = cfg.get("extension_update_url", "")
    timeout    = cfg.get("open_timeout_seconds", 15)

    if ext_id and update_url:
        if browser in ("chrome", "edge", "brave"):
            _write_chromium_policy(browser, ext_id, update_url)
        elif browser == "firefox":
            _write_firefox_policy(update_url)
        else:
            logger.warning("[Launcher] No force-install mechanism for browser={!r}", browser)
    else:
        logger.warning(
            "[Launcher] extension_id or extension_update_url not set in config "
            "— cannot write force-install policy (manual install required)"
        )

    return await _launch_and_wait(browser, profile, config, timeout, narration=narration)


async def cancel_active_launch() -> None:
    """
    Terminate the currently-launching browser process and close any
    just-connected WebSocket so the setup can relaunch with a different profile.
    Safe to call even if no launch is active.
    """
    global _active_proc
    logger.info("[Launcher] cancel_active_launch called — active_proc={}", _active_proc is not None)

    if _active_proc and _active_proc.poll() is None:
        logger.info("[Launcher] Terminating browser process (pid={})", _active_proc.pid)
        try:
            _active_proc.terminate()
        except Exception as exc:
            logger.debug("[Launcher] terminate() error (ignored): {}", exc)
    _active_proc = None

    # If the extension managed to connect in the race window, disconnect it
    if connection_manager.is_connected():
        logger.info("[Launcher] Extension connected during cancel window — closing WebSocket")
        ws = connection_manager._ws
        if ws:
            try:
                await ws.close()
            except Exception as exc:
                logger.debug("[Launcher] ws.close() error (ignored): {}", exc)


# ── Launch helpers ─────────────────────────────────────────────────────────────




async def _launch_and_wait(browser: str, profile: str | None, config: dict, timeout: int, narration=None) -> bool:
    global _active_proc

    ext_folder = Path(__file__).parent.parent.parent / "extension"
    timeout    = max(timeout, 30)

    # ── Step 1: If Chrome is already running with CDP, load the extension there ──
    if browser_detector.is_running(browser):
        cdp_alive = await _cdp_port_available(_CDP_PORT)
        if cdp_alive:
            logger.info("[Launcher] Chrome running with CDP on port {} — loading extension without restart", _CDP_PORT)
            if ext_folder.exists():
                ext_id = await _cdp_load_extension(ext_folder, config, cdp_port=_CDP_PORT)
                if ext_id:
                    logger.info("[Launcher] CDP reload succeeded (ext_id={})", ext_id)
                    return await _wait_for_connection(timeout)
            # CDP alive but load failed — fall through to kill + relaunch
            logger.warning("[Launcher] CDP available but extension load failed — force-relaunching")
        else:
            logger.info("[Launcher] Chrome running without CDP — force-restarting to enable CDP + extension")

    # ── Step 2: Kill all Chrome processes so we can launch fresh with CDP ────────
    udd            = browser_detector.get_user_data_dir(browser)
    has_user_tabs  = (udd / "SingletonLock").exists()   # user has visible windows
    if has_user_tabs:
        msg = (
            f"Sir, {browser.capitalize()} is open with your tabs. "
            "I'll close it now and reopen it with my extension — this is a one-time setup."
        )
    else:
        msg = (
            f"Sir, setting up the JARVIS extension in {browser.capitalize()}. "
            "One moment — this only happens once."
        )
    logger.info("[Launcher] Narrating restart: {}", msg)
    if narration:
        narration.say(msg)

    await _close_and_wait_all(browser)   # graceful → force → wait for OS cleanup

    # ── Step 3: Launch Chrome fresh with CDP flags ────────────────────────────────
    exe = browser_detector.find_exe(browser)
    if not exe:
        logger.error("[Launcher] Executable not found for browser={!r}", browser)
        return False

    args = _build_args(browser, exe, profile, first_setup=True)
    logger.info("[Launcher] Launching fresh {} with CDP: {}", browser, args)

    popen_kwargs: dict = {"close_fds": True}
    if platform.system() == "Windows":
        popen_kwargs["creationflags"] = (
            subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        )

    try:
        _active_proc = subprocess.Popen(args, **popen_kwargs)
        logger.info("[Launcher] Browser process started pid={}", _active_proc.pid)
    except Exception as exc:
        logger.error("[Launcher] Popen failed for {}: {}", browser, exc)
        _active_proc = None
        return False

    # ── Step 4: Wait for CDP to be available then load extension ─────────────────
    if ext_folder.exists():
        ext_id = await _cdp_load_extension(ext_folder, config, cdp_port=_CDP_PORT)
        if ext_id:
            logger.info("[Launcher] Extension loaded via CDP (id={})", ext_id)
        else:
            logger.warning("[Launcher] CDP extension load failed — service worker may not start")

    connected = await _wait_for_connection(timeout)
    logger.info("[Launcher] _wait_for_connection → connected={}", connected)

    if not connected:
        _active_proc = None

    return connected


_CDP_PORT = 9222   # Chrome DevTools Protocol port — always enabled so extension can be reloaded without killing Chrome


def _get_cdp_user_data_dir(browser: str) -> Path:
    """
    Return a --user-data-dir path that Chrome 128+ accepts for CDP.

    Chrome refuses CDP when --user-data-dir equals the OS-level default profile
    directory (LOCALAPPDATA/Google/Chrome/User Data).  We bypass this by creating
    a directory junction at a non-default path that transparently maps to the real
    profile — reads/writes go to the real profile, but the path string is different
    so Chrome's IsDefaultUserDataDirectory() check passes.

    The junction is created once at first use and persists indefinitely.
    Falls back to the real UDD on any error (CDP will be refused, but we try).
    """
    real_udd = browser_detector.get_user_data_dir(browser)

    if platform.system() != "Windows" or browser not in ("chrome", "edge", "brave"):
        return real_udd

    if not real_udd.exists():
        return real_udd

    _JUNCTION_NAMES: dict[str, str] = {
        "chrome": "JarvisChrome",
        "edge":   "JarvisEdge",
        "brave":  "JarvisBrave",
    }
    junction_name = _JUNCTION_NAMES.get(browser)
    if not junction_name:
        return real_udd

    lappdata     = Path(os.environ.get("LOCALAPPDATA", ""))
    junction_path = lappdata / junction_name

    if junction_path.exists():
        logger.debug("[Launcher] CDP junction already exists: {!r}", str(junction_path))
        return junction_path

    # Create the directory junction (no admin rights required for junctions)
    try:
        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(junction_path), str(real_udd)],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            logger.info("[Launcher] Created CDP junction: {} → {}", junction_path, real_udd)
            return junction_path
        else:
            logger.warning("[Launcher] mklink /J failed ({}): {}", result.returncode, result.stderr.strip())
    except Exception as exc:
        logger.warning("[Launcher] Could not create CDP junction: {}", exc)

    return real_udd


def _build_args(browser: str, exe: Path, profile: str | None, *, first_setup: bool = False) -> list[str]:
    args = [str(exe)]
    udd  = browser_detector.get_user_data_dir(browser)

    if browser in ("chrome", "edge", "brave"):
        args += ["--no-first-run", "--no-default-browser-check"]

        # Always expose CDP so we can reload the extension via Extensions.loadUnpacked
        # on any JARVIS restart without needing to kill Chrome again.
        args.append(f"--remote-debugging-port={_CDP_PORT}")
        args.append("--remote-allow-origins=*")
        logger.debug("[Launcher] CDP port {} always enabled", _CDP_PORT)

        if profile and udd.exists():
            # Use a junction path (not the real default UDD) because Chrome 128+
            # refuses CDP when --user-data-dir matches the OS-default profile dir.
            cdp_udd = _get_cdp_user_data_dir(browser)
            args += [
                f"--user-data-dir={cdp_udd}",
                f"--profile-directory={profile}",
            ]
            logger.debug("[Launcher] Chromium profile: user_data={!r} profile={!r}", str(cdp_udd), profile)
        elif not udd.exists():
            logger.debug("[Launcher] Chromium user_data_dir not found ({!r}) — launching default", str(udd))


    elif browser == "firefox":
        if profile and udd.exists():
            profile_path = udd / profile
            if profile_path.exists():
                args += ["--profile", str(profile_path)]
                logger.debug("[Launcher] Firefox --profile {!r}", str(profile_path))
            else:
                logger.debug("[Launcher] Firefox profile path missing ({!r}) — launching default", str(profile_path))

    return args


async def _wait_for_connection(timeout: int) -> bool:
    """Poll is_connected() every 200 ms until timeout (seconds)."""
    loop     = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    logger.debug("[Launcher] Waiting up to {}s for extension to connect …", timeout)
    while loop.time() < deadline:
        if connection_manager.is_connected():
            logger.info("[Launcher] Extension connected after {:.1f}s", timeout - (deadline - loop.time()))
            return True
        await asyncio.sleep(0.2)
    logger.warning("[Launcher] Extension did not connect within {}s", timeout)
    return False


async def _close_browser(browser: str, narration=None) -> None:
    """
    Kill all real browser processes then wait until is_running() confirms they
    are gone.  Used when the browser is open but the extension has never been
    installed — we close and relaunch with --load-extension so the service
    worker can register cleanly.
    """
    _EXE_NAMES: dict[str, str] = {
        "chrome":  "chrome.exe",
        "edge":    "msedge.exe",
        "brave":   "brave.exe",
        "firefox": "firefox.exe",
    }
    exe_name = _EXE_NAMES.get(browser, f"{browser}.exe")

    msg = (
        f"Sir, {browser.capitalize()} is running without the JARVIS extension. "
        "Restarting it now to complete the one-time setup."
    )
    logger.info("[Launcher] Closing {} for fresh extension launch", browser)
    if narration:
        narration.say(msg)
    else:
        logger.info("[Launcher] {}", msg)

    if platform.system() == "Windows":
        # Graceful first — lets Chrome flush Secure Preferences (preserves dev mode state)
        try:
            subprocess.run(["taskkill", "/im", exe_name], capture_output=True, timeout=10)
            logger.info("[Launcher] Graceful close signal sent for {}", exe_name)
        except Exception as exc:
            logger.warning("[Launcher] taskkill (graceful) failed: {}", exc)

        loop = asyncio.get_event_loop()
        grace_end = loop.time() + 12
        while browser_detector.is_running(browser) and loop.time() < grace_end:
            await asyncio.sleep(0.5)

        if browser_detector.is_running(browser):
            logger.info("[Launcher] Graceful close timed out — force-killing {}", exe_name)
            try:
                subprocess.run(["taskkill", "/f", "/im", exe_name], capture_output=True, timeout=10)
            except Exception as exc:
                logger.warning("[Launcher] Force taskkill failed: {}", exc)
    else:
        try:
            subprocess.run(["pkill", "-f", browser], capture_output=True, timeout=10)
        except Exception as exc:
            logger.warning("[Launcher] pkill failed: {}", exc)

    loop     = asyncio.get_event_loop()
    deadline = loop.time() + 15
    while browser_detector.is_running(browser) and loop.time() < deadline:
        await asyncio.sleep(0.5)

    if browser_detector.is_running(browser):
        logger.warning("[Launcher] {} still running after kill — proceeding anyway", browser)
    else:
        logger.info("[Launcher] {} closed successfully", browser)

    await asyncio.sleep(1)


# ── Developer-mode helpers ────────────────────────────────────────────────────


def _is_developer_mode_on(browser: str, profile: str | None) -> bool:
    """
    Read the Chromium Secure Preferences for the given profile and return
    whether extensions.ui.developer_mode is True.
    Returns False on any read/parse error (safe default — triggers narration).
    """
    if browser not in ("chrome", "edge", "brave"):
        return True  # Firefox doesn't use developer mode in the same way

    udd     = browser_detector.get_user_data_dir(browser)
    profile = profile or "Default"
    sec_prefs = udd / profile / "Secure Preferences"

    if not sec_prefs.exists():
        logger.debug("[Launcher] Secure Preferences not found at {!r} — assuming dev mode off", str(sec_prefs))
        return False

    try:
        data = json.loads(sec_prefs.read_text(encoding="utf-8", errors="ignore"))
        on   = data.get("extensions", {}).get("ui", {}).get("developer_mode", False)
        logger.info("[Launcher] developer_mode in Secure Preferences: {}", on)
        return bool(on)
    except Exception as exc:
        logger.debug("[Launcher] Could not read Secure Preferences: {} — assuming dev mode off", exc)
        return False


def _ensure_developer_mode_policy(browser: str) -> None:
    """
    Write ExtensionDeveloperModeSettings = 1 to the HKCU Chrome policy registry
    key.  This forces Developer Mode ON via Group Policy so --load-extension is
    honoured without the user having to toggle anything in chrome://extensions.
    Requires Chrome 128+.  No-op on non-Windows or non-Chromium browsers.
    """
    if platform.system() != "Windows" or browser not in ("chrome", "edge", "brave"):
        return

    _POLICY_ROOTS: dict[str, str] = {
        "chrome": r"SOFTWARE\Policies\Google\Chrome",
        "edge":   r"SOFTWARE\Policies\Microsoft\Edge",
        "brave":  r"SOFTWARE\Policies\BraveSoftware\Brave",
    }
    reg_root = _POLICY_ROOTS.get(browser)
    if not reg_root:
        return

    import winreg
    try:
        key = winreg.CreateKeyEx(
            winreg.HKEY_CURRENT_USER, reg_root, 0, winreg.KEY_ALL_ACCESS,
        )
        winreg.SetValueEx(key, "ExtensionDeveloperModeSettings", 0, winreg.REG_DWORD, 1)
        winreg.CloseKey(key)
        logger.info("[Launcher] ExtensionDeveloperModeSettings=1 written to HKCU\\{}", reg_root)
    except Exception as exc:
        logger.warning("[Launcher] Could not write ExtensionDeveloperModeSettings for {}: {}", browser, exc)


def _patch_developer_mode_prefs(browser: str, profile: str | None) -> None:
    """
    Write developer_mode=True to Chrome's Preferences file, then remove the
    HMAC-protected developer_mode entry from Secure Preferences so Chrome has no
    record of the value there and falls back to the Preferences file on next start.

    This is a best-effort call: Chrome may re-protect the value on startup, but the
    window between this write and the Chrome launch is enough for --load-extension to
    be honoured.  Errors are swallowed because the CDP fallback handles the case
    where this doesn't take effect.
    """
    if browser not in ("chrome", "edge", "brave"):
        return

    udd         = browser_detector.get_user_data_dir(browser)
    profile_dir = udd / (profile or "Default")

    # 1. Write developer_mode=True to the regular Preferences file
    prefs_path = profile_dir / "Preferences"
    if prefs_path.exists():
        try:
            prefs = json.loads(prefs_path.read_text(encoding="utf-8", errors="ignore"))
            prefs.setdefault("extensions", {}).setdefault("ui", {})["developer_mode"] = True
            prefs_path.write_text(json.dumps(prefs, separators=(",", ":")), encoding="utf-8")
            logger.info("[Launcher] Wrote developer_mode=True to Preferences")
        except Exception as exc:
            logger.debug("[Launcher] Could not write Preferences: {}", exc)

    # 2. Remove developer_mode value + its HMAC from Secure Preferences so Chrome
    #    has no HMAC-protected entry for this key and falls back to Preferences above.
    sec_path = profile_dir / "Secure Preferences"
    if sec_path.exists():
        try:
            sec = json.loads(sec_path.read_text(encoding="utf-8", errors="ignore"))
            ext_ui = sec.get("extensions", {}).get("ui", {})
            if "developer_mode" in ext_ui:
                del ext_ui["developer_mode"]
            macs_ui = (
                sec.get("protection", {})
                   .get("macs", {})
                   .get("extensions", {})
                   .get("ui", {})
            )
            if "developer_mode" in macs_ui:
                del macs_ui["developer_mode"]
            sec_path.write_text(json.dumps(sec, separators=(",", ":")), encoding="utf-8")
            logger.info("[Launcher] Removed developer_mode HMAC from Secure Preferences")
        except Exception as exc:
            logger.debug("[Launcher] Could not patch Secure Preferences: {}", exc)


async def _cdp_port_available(cdp_port: int) -> bool:
    """Return True if Chrome's CDP HTTP endpoint is reachable on cdp_port."""
    try:
        import httpx as _httpx
        r = _httpx.get(f"http://127.0.0.1:{cdp_port}/json/version", timeout=1.0)
        return r.status_code == 200
    except Exception:
        return False


def _patch_exit_type_normal(browser: str) -> None:
    """
    Write exit_type='Normal' and exited_cleanly=True to every active profile's
    Preferences file before Chrome is killed.

    Chrome writes exit_type='Crashed' on startup (forward-write crash detector).
    A clean exit rewrites it to 'Normal'. A force-kill leaves 'Crashed', which
    on next startup causes Chrome to reset its Google Sync OAuth token — signing
    the user out of Google.  Pre-writing 'Normal' here prevents that reset even
    if we end up force-killing Chrome.
    """
    udd = browser_detector.get_user_data_dir(browser)
    if not udd.exists():
        return
    for entry in udd.iterdir():
        if entry.name != "Default" and not entry.name.startswith("Profile "):
            continue
        prefs_path = entry / "Preferences"
        if not prefs_path.exists():
            continue
        try:
            prefs = json.loads(prefs_path.read_text(encoding="utf-8", errors="ignore"))
            profile_sect = prefs.setdefault("profile", {})
            profile_sect["exit_type"]      = "Normal"
            profile_sect["exited_cleanly"] = True
            prefs_path.write_text(json.dumps(prefs, separators=(",", ":")), encoding="utf-8")
            logger.debug("[Launcher] Pre-wrote exit_type=Normal for {}", entry.name)
        except Exception as exc:
            logger.debug("[Launcher] Could not patch exit_type for {}: {}", entry.name, exc)


async def _close_and_wait_all(browser: str) -> None:
    """
    Gracefully close browser, then force-kill any survivors, then wait until
    ALL browser processes (including child renderers) are gone from the OS.
    This ensures Chrome's port 9222 is released before we relaunch.
    """
    _EXE_NAMES: dict[str, str] = {
        "chrome":  "chrome.exe",
        "edge":    "msedge.exe",
        "brave":   "brave.exe",
        "firefox": "firefox.exe",
    }
    exe_name = _EXE_NAMES.get(browser, f"{browser}.exe")

    if platform.system() == "Windows":
        # Pre-write exit_type='Normal' before sending the kill signal.
        # Chrome sets exit_type='Crashed' on startup as a forward-write crash
        # detector; a force-kill leaves it 'Crashed', causing Google Sync to
        # reset its token on next startup (= "Google account signed out").
        # Writing 'Normal' now means even a force-kill won't trigger that reset.
        if browser in ("chrome", "edge", "brave"):
            _patch_exit_type_normal(browser)

        # Graceful close — lets Chrome flush its profile
        try:
            subprocess.run(["taskkill", "/im", exe_name], capture_output=True, timeout=10)
            logger.info("[Launcher] Graceful close sent for {}", exe_name)
        except Exception as exc:
            logger.debug("[Launcher] Graceful taskkill error: {}", exc)

        # Wait up to 20s for graceful exit.
        # Chrome writes exit_type='Normal' to Preferences only on a clean shutdown.
        # A force-kill leaves exit_type='Crashed', which causes Chrome to reset
        # Google Sync tokens on next startup — signing the user out of Google.
        # 20 seconds is enough for Chrome with many tabs to flush its profile.
        loop     = asyncio.get_event_loop()
        deadline = loop.time() + 20
        while loop.time() < deadline:
            result = subprocess.run(
                ["tasklist", "/fi", f"imagename eq {exe_name}", "/fo", "csv", "/nh"],
                capture_output=True, text=True, timeout=5,
            )
            if exe_name.lower() not in result.stdout.lower():
                logger.info("[Launcher] {} fully exited after graceful close", browser)
                await asyncio.sleep(1)
                return
            await asyncio.sleep(0.5)

        # Force-kill survivors (last resort — may leave exit_type='Crashed')
        logger.warning("[Launcher] Force-killing remaining {} processes after 20s graceful timeout", exe_name)
        try:
            subprocess.run(["taskkill", "/f", "/im", exe_name], capture_output=True, timeout=10)
        except Exception as exc:
            logger.debug("[Launcher] Force taskkill error: {}", exc)

        # Wait up to 5s for all processes to vanish (including child renderers)
        deadline = asyncio.get_event_loop().time() + 5
        while asyncio.get_event_loop().time() < deadline:
            result = subprocess.run(
                ["tasklist", "/fi", f"imagename eq {exe_name}", "/fo", "csv", "/nh"],
                capture_output=True, text=True, timeout=5,
            )
            if exe_name.lower() not in result.stdout.lower():
                break
            await asyncio.sleep(0.5)

    else:
        try:
            subprocess.run(["pkill", "-f", browser], capture_output=True, timeout=10)
        except Exception as exc:
            logger.debug("[Launcher] pkill error: {}", exc)

    await asyncio.sleep(1)   # extra buffer for OS port release
    logger.info("[Launcher] {} close complete", browser)


async def _cdp_load_extension(ext_path: Path, config: dict, cdp_port: int = 9222) -> str | None:
    """
    Use Chrome DevTools Protocol Extensions.loadUnpacked (Chrome 128+) to load
    the extension without requiring the Developer Mode toggle to be ON.

    Chrome must already be running with --remote-debugging-port=cdp_port.
    Returns the extension ID string on success, None on failure.
    Saves the extension ID to config['browser_extension']['extension_id'] and
    writes it back to config.json on disk.
    """
    cdp_base = f"http://127.0.0.1:{cdp_port}"

    try:
        import httpx as _httpx
    except ImportError:
        logger.debug("[Launcher] httpx not available — skipping CDP extension load")
        return None

    # Wait up to 15s for CDP endpoint to become available (Chrome needs ~1s normally)
    version_resp = None
    for _ in range(30):
        try:
            r = _httpx.get(f"{cdp_base}/json/version", timeout=1.0)
            if r.status_code == 200:
                version_resp = r
                break
        except Exception:
            pass
        await asyncio.sleep(0.5)

    if version_resp is None:
        logger.warning("[Launcher] CDP not available on port {} after 15s", cdp_port)
        return None

    ws_url = version_resp.json().get("webSocketDebuggerUrl", "")
    if not ws_url:
        logger.warning("[Launcher] CDP /json/version missing webSocketDebuggerUrl")
        return None

    import json as _json
    import websockets as _ws
    try:
        async with _ws.connect(ws_url) as cdp:
            payload = _json.dumps({
                "id": 1,
                "method": "Extensions.loadUnpacked",
                "params": {"path": str(ext_path)},
            })
            await cdp.send(payload)
            raw    = await asyncio.wait_for(cdp.recv(), timeout=10.0)
            result = _json.loads(raw)
            if "error" in result:
                logger.warning("[Launcher] CDP Extensions.loadUnpacked error: {}", result["error"])
                return None
            ext_id = result.get("result", {}).get("id", "")
            if not ext_id:
                logger.warning("[Launcher] CDP loadUnpacked returned no extension id: {}", result)
                return None
            logger.info("[Launcher] CDP Extensions.loadUnpacked succeeded — id={}", ext_id)
            _save_extension_id(ext_id, config)
            return ext_id
    except Exception as exc:
        logger.warning("[Launcher] CDP loadUnpacked exception: {}", exc)
        return None


def _save_extension_id(ext_id: str, config: dict) -> None:
    """Persist the extension ID to config.json so the WebSocket server can use it."""
    config.setdefault("browser_extension", {})["extension_id"] = ext_id
    # Find config.json: project root is 3 levels up from this file
    config_path = Path(__file__).parent.parent.parent / "config.json"
    try:
        existing = json.loads(config_path.read_text(encoding="utf-8")) if config_path.exists() else {}
        existing.setdefault("browser_extension", {})["extension_id"] = ext_id
        config_path.write_text(json.dumps(existing, indent=2, ensure_ascii=False), encoding="utf-8")
        logger.info("[Launcher] Saved extension_id={!r} to config.json", ext_id)
    except Exception as exc:
        logger.warning("[Launcher] Could not save extension_id to config.json: {}", exc)


# ── Force-install policies ─────────────────────────────────────────────────────


def _write_chromium_policy(browser: str, ext_id: str, update_url: str) -> None:
    """
    Write ExtensionInstallForcelist registry entry for Chrome / Edge / Brave.
    Windows only.  Skips silently on other platforms.
    """
    if platform.system() != "Windows":
        logger.debug("[Launcher] _write_chromium_policy: skipping on non-Windows platform")
        return

    import winreg
    reg_key  = _FORCELIST_KEYS.get(browser)
    if not reg_key:
        logger.warning("[Launcher] No registry key defined for browser={!r}", browser)
        return

    entry_value = f"{ext_id};{update_url}"
    logger.info("[Launcher] Writing force-install policy for {} → {}", browser, entry_value)
    try:
        key = winreg.CreateKeyEx(
            winreg.HKEY_CURRENT_USER, reg_key, 0, winreg.KEY_READ | winreg.KEY_SET_VALUE
        )
        existing_indices: set[int] = set()
        i = 0
        while True:
            try:
                name, value, _ = winreg.EnumValue(key, i)
                if value == entry_value:
                    logger.info("[Launcher] Force-install policy already set for {} — skipping", browser)
                    winreg.CloseKey(key)
                    return
                try:
                    existing_indices.add(int(name))
                except ValueError:
                    pass
                i += 1
            except OSError:
                break

        next_index = 1
        while next_index in existing_indices:
            next_index += 1

        winreg.SetValueEx(key, str(next_index), 0, winreg.REG_SZ, entry_value)
        winreg.CloseKey(key)
        logger.info("[Launcher] Registry entry written: {}\\{} = {!r}", reg_key, next_index, entry_value)
    except Exception as exc:
        logger.error("[Launcher] Failed to write registry policy for {}: {}", browser, exc)


def _write_firefox_policy(update_url: str) -> None:
    """
    Write Firefox policies.json to force-install the extension.
    Supports Windows, macOS, and Linux install paths.
    The update_url should point to an .xpi file (local file:// or hosted URL).
    """
    candidates = _firefox_policy_paths()
    logger.info("[Launcher] Writing Firefox policies.json (update_url={!r})", update_url)
    logger.debug("[Launcher] Firefox policy candidate paths: {}", [str(p) for p in candidates])

    policy_content = json.dumps({
        "policies": {
            "Extensions": {
                "Install": [update_url]
            }
        }
    }, indent=2)

    written = False
    for dist_dir in candidates:
        try:
            dist_dir.mkdir(parents=True, exist_ok=True)
            policy_path = dist_dir / "policies.json"

            # Merge with existing if present
            if policy_path.exists():
                try:
                    existing = json.loads(policy_path.read_text(encoding="utf-8"))
                    install_list = (
                        existing.get("policies", {}).get("Extensions", {}).get("Install", [])
                    )
                    if update_url in install_list:
                        logger.info("[Launcher] Firefox policy already contains this extension — skipping {!r}", str(policy_path))
                        written = True
                        continue
                    install_list.append(update_url)
                    existing.setdefault("policies", {}).setdefault("Extensions", {})["Install"] = install_list
                    policy_content = json.dumps(existing, indent=2)
                except Exception as exc:
                    logger.debug("[Launcher] Could not merge existing policies.json ({}): {}", str(policy_path), exc)

            policy_path.write_text(policy_content, encoding="utf-8")
            logger.info("[Launcher] Firefox policies.json written to {!r}", str(policy_path))
            written = True
        except PermissionError as exc:
            logger.debug("[Launcher] No write permission to {!r}: {}", str(dist_dir), exc)
        except Exception as exc:
            logger.debug("[Launcher] Could not write Firefox policy to {!r}: {}", str(dist_dir), exc)

    if not written:
        logger.error(
            "[Launcher] Could not write Firefox policies.json to any candidate path "
            "(may need admin rights). Manual extension install required."
        )


def _firefox_policy_paths() -> list[Path]:
    """Return candidate distribution/ dirs for Firefox policies.json, by OS."""
    system = platform.system()
    paths  = []
    if system == "Windows":
        for base in (
            Path(r"C:\Program Files\Mozilla Firefox"),
            Path(r"C:\Program Files (x86)\Mozilla Firefox"),
        ):
            paths.append(base / "distribution")
    elif system == "Darwin":
        paths.append(Path("/Applications/Firefox.app/Contents/Resources/distribution"))
    else:  # Linux
        for base in (
            Path("/usr/lib/firefox"),
            Path("/usr/lib64/firefox"),
            Path("/usr/share/firefox"),
            Path("/opt/firefox"),
        ):
            paths.append(base / "distribution")
    return paths
