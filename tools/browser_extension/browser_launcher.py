import asyncio
import json
import os
import subprocess
from pathlib import Path

from loguru import logger

from . import connection_manager

# ── Chrome locations ──────────────────────────────────────────────────────────

_LOCALAPPDATA = Path(os.environ.get("LOCALAPPDATA", ""))

_CHROME_EXE_CANDIDATES = [
    Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
    Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
    _LOCALAPPDATA / "Google" / "Chrome" / "Application" / "chrome.exe",
]

_CHROME_USER_DATA_DIR = _LOCALAPPDATA / "Google" / "Chrome" / "User Data"

# Registry key Chrome reads at startup for force-installed extensions
_FORCELIST_KEY = r"SOFTWARE\Policies\Google\Chrome\ExtensionInstallForcelist"

# ── Public API ────────────────────────────────────────────────────────────────


async def open(config: dict) -> bool:
    """
    If the extension is already connected, return immediately.
    Otherwise detect which Chrome profile has the extension installed,
    launch Chrome with that profile, and wait for the extension to connect.
    Returns True if connected within the configured timeout, False otherwise.
    """
    if connection_manager.is_connected():
        return True

    cfg = config.get("browser_extension", {})
    timeout = cfg.get("open_timeout_seconds", 10)
    ext_id  = cfg.get("extension_id", "")

    profile = detect_best_profile(ext_id) if ext_id else None
    if profile:
        logger.info(f"[BrowserLauncher] Extension found in profile: {profile!r}")
    else:
        logger.info("[BrowserLauncher] No profile with extension found — launching default profile")

    return await _launch_and_wait(config, profile, timeout)


async def open_with_profile(profile_name: str, config: dict) -> bool:
    """
    Launch Chrome forcing a specific named profile directory (e.g. 'Profile 3').
    Used when the user explicitly says 'use my work profile'.
    Returns True if extension connects within timeout.
    """
    cfg     = config.get("browser_extension", {})
    timeout = cfg.get("open_timeout_seconds", 10)
    return await _launch_and_wait(config, profile_name, timeout)


async def install_and_open(profile_name: str, config: dict) -> bool:
    """
    Write a Chrome ExtensionInstallForcelist registry key for the extension,
    then open Chrome with the given profile. Chrome will silently install the
    extension on launch, showing only a brief 'JARVIS was added' toast.
    Returns True if extension connects within timeout.
    """
    cfg        = config.get("browser_extension", {})
    ext_id     = cfg.get("extension_id", "")
    update_url = cfg.get("extension_update_url", "")
    timeout    = cfg.get("open_timeout_seconds", 10)

    if ext_id and update_url:
        _write_force_install_policy(ext_id, update_url)
    else:
        logger.warning(
            "[BrowserLauncher] extension_id or extension_update_url not set in config — "
            "skipping force-install registry write"
        )

    return await _launch_and_wait(config, profile_name, timeout)


# ── Profile detection ─────────────────────────────────────────────────────────


def detect_best_profile(extension_id: str) -> str | None:
    """
    Scan all Chrome profiles and return the directory name of the profile
    that has the extension installed. If multiple profiles have it, the
    most recently used one wins. Returns None if the extension is not found
    in any profile.
    """
    if not _CHROME_USER_DATA_DIR.exists():
        return None

    candidates = [
        p for p in _list_profile_dirs()
        if _profile_has_extension(p, extension_id)
    ]

    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0].name

    best = max(candidates, key=_profile_last_used)
    return best.name


def _list_profile_dirs() -> list[Path]:
    dirs = []
    for entry in _CHROME_USER_DATA_DIR.iterdir():
        if entry.is_dir() and (entry.name == "Default" or entry.name.startswith("Profile ")):
            dirs.append(entry)
    return dirs


def _profile_has_extension(profile_dir: Path, extension_id: str) -> bool:
    return (profile_dir / "Extensions" / extension_id).exists()


def _profile_last_used(profile_dir: Path) -> float:
    """Read last-used timestamp from Preferences JSON. Returns 0 on failure."""
    prefs = profile_dir / "Preferences"
    try:
        data = json.loads(prefs.read_text(encoding="utf-8", errors="ignore"))
        return float(data.get("profile", {}).get("last_used", 0) or 0)
    except Exception:
        return 0.0


# ── Chrome launch ─────────────────────────────────────────────────────────────


async def _launch_and_wait(config: dict, profile_name: str | None, timeout: int) -> bool:
    exe = _find_chrome_exe(config)
    if not exe:
        logger.error("[BrowserLauncher] Chrome executable not found. Check browser_extension.browser_path in config.json")
        return False

    args = [str(exe)]
    if profile_name and _CHROME_USER_DATA_DIR.exists():
        # Passing both flags bypasses the profile picker dialog
        args += [
            f"--user-data-dir={_CHROME_USER_DATA_DIR}",
            f"--profile-directory={profile_name}",
        ]

    logger.info(f"[BrowserLauncher] Launching Chrome (profile={profile_name or 'default'})")
    subprocess.Popen(
        args,
        creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
        close_fds=True,
    )

    return await _wait_for_connection(timeout)


async def _wait_for_connection(timeout: int) -> bool:
    """Poll is_connected() every 200 ms until timeout."""
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if connection_manager.is_connected():
            logger.info("[BrowserLauncher] Extension connected.")
            return True
        await asyncio.sleep(0.2)
    logger.warning(f"[BrowserLauncher] Extension did not connect within {timeout}s.")
    return False


def _find_chrome_exe(config: dict) -> Path | None:
    override = config.get("browser_extension", {}).get("browser_path", "")
    if override:
        p = Path(override)
        if p.exists():
            return p
        logger.warning(f"[BrowserLauncher] browser_path override not found: {p}")

    for candidate in _CHROME_EXE_CANDIDATES:
        if candidate.exists():
            return candidate

    return None


# ── Registry — silent extension install ───────────────────────────────────────


def _write_force_install_policy(extension_id: str, update_url: str) -> None:
    """
    Write the Chrome ExtensionInstallForcelist registry key.
    Chrome installs the extension silently on next launch.
    Skips writing if this extension_id is already in the list.
    """
    import winreg  # Windows-only; imported here so non-Windows imports don't fail

    entry_value = f"{extension_id};{update_url}"
    try:
        key = winreg.CreateKeyEx(
            winreg.HKEY_CURRENT_USER, _FORCELIST_KEY, 0, winreg.KEY_READ | winreg.KEY_SET_VALUE
        )

        # Read existing values to avoid duplicates and find next free index
        existing_indices: set[int] = set()
        i = 0
        while True:
            try:
                name, value, _ = winreg.EnumValue(key, i)
                if value == entry_value:
                    logger.info("[BrowserLauncher] Force-install policy already set — skipping")
                    winreg.CloseKey(key)
                    return
                try:
                    existing_indices.add(int(name))
                except ValueError:
                    pass
                i += 1
            except OSError:
                break  # no more values

        next_index = 1
        while next_index in existing_indices:
            next_index += 1

        winreg.SetValueEx(key, str(next_index), 0, winreg.REG_SZ, entry_value)
        winreg.CloseKey(key)
        logger.info(f"[BrowserLauncher] Force-install policy written for {extension_id}")
    except Exception as exc:
        logger.error(f"[BrowserLauncher] Failed to write registry policy: {exc}")
