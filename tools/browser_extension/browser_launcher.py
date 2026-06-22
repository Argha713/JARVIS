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


async def open_browser(browser: str, profile: str | None, config: dict) -> bool:
    """
    If the extension is already connected, return True immediately.
    Otherwise launch the browser with the given profile and wait for the extension.
    Returns True if connected within the configured timeout.
    """
    logger.info("[Launcher] open_browser: browser={!r} profile={!r}", browser, profile)

    if connection_manager.is_connected():
        logger.info("[Launcher] Extension already connected — nothing to open")
        return True

    cfg     = config.get("browser_extension", {})
    timeout = cfg.get("open_timeout_seconds", 15)
    return await _launch_and_wait(browser, profile, config, timeout)


async def install_and_open_browser(browser: str, profile: str | None, config: dict) -> bool:
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

    return await _launch_and_wait(browser, profile, config, timeout)


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


async def _launch_and_wait(browser: str, profile: str | None, config: dict, timeout: int) -> bool:
    global _active_proc

    # If the browser is already open, Popen with --load-extension is silently ignored
    # because Chrome hands off to the existing process. Skip Popen entirely and just
    # wait for the extension to connect — it must be permanently installed for this to work.
    if browser_detector.is_running(browser):
        logger.warning(
            "[Launcher] {} is already running — skipping Popen (--load-extension would be "
            "ignored). Waiting {}s for extension to connect. "
            "If it never connects, install the extension permanently via chrome://extensions "
            "→ Developer mode → Load unpacked → select the 'extension/' folder.",
            browser, timeout,
        )
        connected = await _wait_for_connection(timeout)
        logger.info("[Launcher] _wait_for_connection (existing instance) → connected={}", connected)
        return connected

    exe = browser_detector.find_exe(browser)
    if not exe:
        logger.error("[Launcher] Executable not found for browser={!r}", browser)
        return False

    args = _build_args(browser, exe, profile)
    logger.info("[Launcher] Launching {} args={}", browser, args)

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

    connected = await _wait_for_connection(timeout)
    logger.info("[Launcher] _wait_for_connection → connected={}", connected)

    if not connected:
        _active_proc = None

    return connected


def _build_args(browser: str, exe: Path, profile: str | None) -> list[str]:
    args = [str(exe)]
    udd  = browser_detector.get_user_data_dir(browser)

    if browser in ("chrome", "edge", "brave"):
        # Load the JARVIS extension automatically — no manual Chrome setup needed.
        # The extension folder path: tools/browser_extension/ → tools/ → project root → extension/
        ext_folder = Path(__file__).parent.parent.parent / "extension"
        if ext_folder.exists():
            args.append(f"--load-extension={ext_folder}")
            logger.debug("[Launcher] --load-extension={!r}", str(ext_folder))
        else:
            logger.warning("[Launcher] Extension folder not found at {!r}", str(ext_folder))

        args += ["--no-first-run", "--no-default-browser-check"]

        if profile and udd.exists():
            args += [
                f"--user-data-dir={udd}",
                f"--profile-directory={profile}",
            ]
            logger.debug("[Launcher] Chromium profile flags: user_data={!r} profile={!r}", str(udd), profile)
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
