"""
browser_detector: cross-platform browser detection, profile listing, and
history-based site inference.

Supports (can install extension): Chrome, Edge, Firefox, Brave.
Unsupported (extension requires special build):  Safari.

All public symbols:
  detect_installed()            -> list[dict]   browsers found on this machine
  list_profiles(browser)        -> list[dict]   profiles for a browser
  infer_profile_for_domain(browser, domain) -> str | None
  find_exe(browser)             -> Path | None
  get_user_data_dir(browser)    -> Path
  SUPPORTED / UNSUPPORTED       constants

Each browser dict:
  {browser, display_name, exe, support, profiles: [{name, display_name, path, last_used}]}
Each profile dict:
  {name, display_name, path: Path, last_used: float}
"""
import os
import platform
import shutil
import sqlite3
import tempfile
import json
from pathlib import Path

from loguru import logger

# ── Constants ─────────────────────────────────────────────────────────────────

SUPPORTED   = "supported"
UNSUPPORTED = "unsupported"

_SYSTEM = platform.system()   # 'Windows', 'Darwin', 'Linux'
_HOME   = Path.home()
_LAPPDATA = Path(os.environ.get("LOCALAPPDATA", ""))   # Windows only
_APPDATA  = Path(os.environ.get("APPDATA",      ""))   # Windows only

_BROWSER_DISPLAY = {
    "chrome":  "Chrome",
    "edge":    "Edge",
    "firefox": "Firefox",
    "brave":   "Brave",
    "safari":  "Safari",
}

# ── Public API ─────────────────────────────────────────────────────────────────


def detect_installed() -> list[dict]:
    """
    Return a list of detected browser dicts, each with profiles already populated.
    Logs every browser found and every one checked but absent.
    """
    logger.info("[Detector] Scanning for browsers on {} ({})", _SYSTEM, platform.node())
    results = []
    for name in ("chrome", "edge", "firefox", "brave", "safari"):
        info = _detect_browser(name)
        if info:
            logger.info("[Detector] FOUND: {} exe={!r} profiles={} support={}",
                        name, str(info["exe"]), len(info["profiles"]), info["support"])
            results.append(info)
        else:
            logger.debug("[Detector] NOT FOUND: {}", name)
    logger.info("[Detector] Total browsers detected: {}", len(results))
    return results


def list_profiles(browser: str) -> list[dict]:
    """
    Return all profiles for a browser, sorted by last_used descending.
    Returns [] if browser not found or no profiles directory.
    """
    udd = get_user_data_dir(browser)
    logger.debug("[Detector] list_profiles browser={!r} udd={!r}", browser, str(udd))
    if browser == "firefox":
        profiles = _list_firefox_profiles(udd)
    else:
        profiles = _list_chromium_profiles(udd)
    logger.info("[Detector] Profiles for {}: {}",
                browser, [(p["name"], p["display_name"]) for p in profiles])
    return profiles


def is_running(browser: str) -> bool:
    """
    Return True if the REAL browser is running with its normal user data directory.

    Uses the browser's SingletonLock file rather than tasklist. Tasklist would also
    detect Playwright's bundled Chromium (which shares the chrome.exe binary name on
    Windows) causing false positives when the validator's headless browser is active.
    SingletonLock is only written by the real browser instance — Playwright uses an
    isolated temp user-data-dir and never touches the user's Chrome directory.

    Edge case: if Chrome crashes without cleanup, the lock file can remain (stale).
    Chrome itself handles stale locks on next start; for us the worst case is a 10s
    wait then Playwright fallback, which is acceptable.
    """
    udd = get_user_data_dir(browser)
    if not udd.exists():
        logger.debug("[Detector] is_running {!r} → False (user_data_dir not found)", browser)
        return False
    lock = udd / "SingletonLock"
    found = lock.exists()
    logger.debug("[Detector] is_running {!r} → {} (SingletonLock {})",
                 browser, found, "present" if found else "absent")
    return found


def infer_profile_for_domain(browser: str, domain: str) -> str | None:
    """
    Scan browser history across all profiles to find which one most recently
    visited domain.  Returns the profile directory name (e.g. 'Profile 2') or None.
    Chrome locks its History file when running — we copy it first.
    """
    logger.info("[Detector] infer_profile_for_domain browser={!r} domain={!r}", browser, domain)
    profiles = list_profiles(browser)
    if not profiles:
        logger.debug("[Detector] No profiles — cannot infer")
        return None

    best_name: str | None = None
    best_time: float = 0.0

    for p in profiles:
        t = _last_visit_time(browser, p["path"], domain)
        logger.debug("[Detector] Profile {!r}: last_visit_time for {!r} = {}", p["name"], domain, t)
        if t and t > best_time:
            best_time = t
            best_name = p["name"]

    logger.info("[Detector] infer_profile_for_domain → {!r} (last_visit={})", best_name, best_time)
    return best_name


def find_exe(browser: str) -> Path | None:
    """Return the path to the browser executable, or None if not found."""
    for candidate in _exe_candidates(browser):
        if candidate.exists():
            logger.debug("[Detector] find_exe {!r} → {!r}", browser, str(candidate))
            return candidate
    logger.debug("[Detector] find_exe {!r} → not found", browser)
    return None


def get_user_data_dir(browser: str) -> Path:
    """Return the User Data (profiles root) directory for a browser on this OS."""
    dirs: dict[str, dict[str, Path]] = {
        "chrome": {
            "Windows": _LAPPDATA / "Google" / "Chrome" / "User Data",
            "Darwin":  _HOME / "Library" / "Application Support" / "Google" / "Chrome",
            "Linux":   _HOME / ".config" / "google-chrome",
        },
        "edge": {
            "Windows": _LAPPDATA / "Microsoft" / "Edge" / "User Data",
            "Darwin":  _HOME / "Library" / "Application Support" / "Microsoft Edge",
            "Linux":   _HOME / ".config" / "microsoft-edge",
        },
        "brave": {
            "Windows": _LAPPDATA / "BraveSoftware" / "Brave-Browser" / "User Data",
            "Darwin":  _HOME / "Library" / "Application Support" / "BraveSoftware" / "Brave-Browser",
            "Linux":   _HOME / ".config" / "BraveSoftware" / "Brave-Browser",
        },
        "firefox": {
            "Windows": _APPDATA / "Mozilla" / "Firefox" / "Profiles",
            "Darwin":  _HOME / "Library" / "Application Support" / "Firefox" / "Profiles",
            "Linux":   _HOME / ".mozilla" / "firefox",
        },
        "safari": {
            "Darwin":  Path("/Applications/Safari.app/Contents"),
        },
    }
    return dirs.get(browser, {}).get(_SYSTEM, Path())


# ── Detection helpers ──────────────────────────────────────────────────────────


def _detect_browser(name: str) -> dict | None:
    exe = find_exe(name)
    if not exe:
        return None
    profiles = list_profiles(name)
    support  = UNSUPPORTED if name == "safari" else SUPPORTED
    return {
        "browser":      name,
        "display_name": _BROWSER_DISPLAY[name],
        "exe":          exe,
        "support":      support,
        "profiles":     profiles,
    }


def _exe_candidates(browser: str) -> list[Path]:
    candidates: dict[str, dict[str, list[Path]]] = {
        "chrome": {
            "Windows": [
                Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
                Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
                _LAPPDATA / "Google" / "Chrome" / "Application" / "chrome.exe",
            ],
            "Darwin": [Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")],
            "Linux":  [
                Path("/usr/bin/google-chrome"),
                Path("/usr/bin/google-chrome-stable"),
                Path("/usr/local/bin/google-chrome"),
            ],
        },
        "edge": {
            "Windows": [
                Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
                _LAPPDATA / "Microsoft" / "Edge" / "Application" / "msedge.exe",
            ],
            "Darwin": [Path("/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge")],
            "Linux":  [
                Path("/usr/bin/microsoft-edge"),
                Path("/usr/bin/microsoft-edge-stable"),
            ],
        },
        "firefox": {
            "Windows": [
                Path(r"C:\Program Files\Mozilla Firefox\firefox.exe"),
                Path(r"C:\Program Files (x86)\Mozilla Firefox\firefox.exe"),
            ],
            "Darwin": [Path("/Applications/Firefox.app/Contents/MacOS/firefox")],
            "Linux":  [Path("/usr/bin/firefox"), Path("/usr/local/bin/firefox")],
        },
        "brave": {
            "Windows": [
                _LAPPDATA / "BraveSoftware" / "Brave-Browser" / "Application" / "brave.exe",
                Path(r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe"),
            ],
            "Darwin": [Path("/Applications/Brave Browser.app/Contents/MacOS/Brave Browser")],
            "Linux":  [Path("/usr/bin/brave-browser"), Path("/usr/bin/brave")],
        },
        "safari": {
            "Darwin": [Path("/Applications/Safari.app/Contents/MacOS/Safari")],
        },
    }
    return candidates.get(browser, {}).get(_SYSTEM, [])


# ── Profile listing ────────────────────────────────────────────────────────────


def _list_chromium_profiles(user_data_dir: Path) -> list[dict]:
    if not user_data_dir.exists():
        logger.debug("[Detector] Chromium user_data_dir not found: {!r}", str(user_data_dir))
        return []

    # Local State is authoritative — it's what Chrome's profile switcher UI reads.
    # Individual Preferences files can have stale/generic names like "Your Chrome".
    ls_cache: dict = {}
    local_state = user_data_dir / "Local State"
    if local_state.exists():
        try:
            ls_data  = json.loads(local_state.read_text(encoding="utf-8", errors="ignore"))
            ls_cache = ls_data.get("profile", {}).get("info_cache", {})
        except Exception as exc:
            logger.debug("[Detector] Could not read Local State: {}", exc)

    profiles = []
    for entry in user_data_dir.iterdir():
        if not entry.is_dir():
            continue
        if entry.name != "Default" and not entry.name.startswith("Profile "):
            continue

        ls_entry     = ls_cache.get(entry.name, {})
        display_name = ls_entry.get("name") or entry.name
        last_used    = float(ls_entry.get("last_used", 0) or 0)
        email        = ls_entry.get("user_name", "")

        # Fall back to Preferences if Local State had nothing for this profile dir
        if not ls_entry:
            prefs_path = entry / "Preferences"
            if prefs_path.exists():
                try:
                    prefs        = json.loads(prefs_path.read_text(encoding="utf-8", errors="ignore"))
                    display_name = prefs.get("profile", {}).get("name", entry.name) or entry.name
                    last_used    = float(prefs.get("profile", {}).get("last_used", 0) or 0)
                except Exception as exc:
                    logger.debug("[Detector] Could not read Preferences for {}: {}", entry.name, exc)

        profiles.append({
            "name":         entry.name,
            "display_name": display_name,
            "email":        email,
            "path":         entry,
            "last_used":    last_used,
        })

    profiles.sort(key=lambda p: p["last_used"], reverse=True)
    return profiles


def _list_firefox_profiles(profiles_dir: Path) -> list[dict]:
    """
    Parse Firefox profiles.ini or fall back to scanning the profiles directory.
    Firefox stores profiles inside the same dir on Windows/Linux or one level up on macOS.
    """
    # On macOS the profiles.ini is one level up from Profiles/
    base = profiles_dir.parent if _SYSTEM == "Darwin" else profiles_dir
    ini  = base / "profiles.ini"

    if ini.exists():
        return _parse_firefox_ini(ini, base)

    # No ini — scan directory directly
    profiles = []
    if profiles_dir.exists():
        for entry in profiles_dir.iterdir():
            if entry.is_dir() and "." in entry.name:
                profiles.append({
                    "name":         entry.name,
                    "display_name": entry.name,
                    "path":         entry,
                    "last_used":    0.0,
                })
    return profiles


def _parse_firefox_ini(ini_path: Path, base: Path) -> list[dict]:
    import configparser
    parser = configparser.ConfigParser()
    try:
        parser.read(str(ini_path), encoding="utf-8")
    except Exception as exc:
        logger.debug("[Detector] Could not read Firefox profiles.ini: {}", exc)
        return []
    profiles = []
    for section in parser.sections():
        if not section.startswith("Profile"):
            continue
        display_name = parser.get(section, "Name",       fallback="")
        path_str     = parser.get(section, "Path",       fallback="")
        is_relative  = parser.getboolean(section, "IsRelative", fallback=True)
        if not path_str:
            continue
        norm = path_str.replace("/", os.sep)
        profile_path = (base / norm) if is_relative else Path(path_str)
        if not profile_path.exists():
            logger.debug("[Detector] Firefox profile path missing: {!r}", str(profile_path))
            continue
        profiles.append({
            "name":         profile_path.name,
            "display_name": display_name or profile_path.name,
            "path":         profile_path,
            "last_used":    0.0,
        })
    return profiles


# ── History inference ──────────────────────────────────────────────────────────


def _last_visit_time(browser: str, profile_path: Path, domain: str) -> float:
    """
    Return the last visit timestamp (float) for urls containing domain in this profile.
    Copies the history db to a temp file to avoid Chrome's write lock.
    Returns 0.0 on any error.
    """
    if browser == "firefox":
        db_path = profile_path / "places.sqlite"
        query   = "SELECT last_visit_date FROM moz_places WHERE url LIKE ? ORDER BY last_visit_date DESC LIMIT 1"
    else:
        db_path = profile_path / "History"
        query   = "SELECT last_visit_time FROM urls WHERE url LIKE ? ORDER BY last_visit_time DESC LIMIT 1"

    if not db_path.exists():
        logger.debug("[Detector] History db not found: {!r}", str(db_path))
        return 0.0

    tmp = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            tmp = Path(f.name)
        shutil.copy2(db_path, tmp)

        con = sqlite3.connect(f"file:{tmp}?mode=ro", uri=True)
        row = con.execute(query, (f"%{domain}%",)).fetchone()
        con.close()

        if row and row[0]:
            logger.debug("[Detector] _last_visit_time domain={!r} profile={!r} → {}", domain, profile_path.name, row[0])
            return float(row[0])
        return 0.0
    except Exception as exc:
        logger.debug("[Detector] History query error for {!r}: {}", str(db_path), exc)
        return 0.0
    finally:
        if tmp:
            try:
                tmp.unlink()
            except Exception:
                pass
