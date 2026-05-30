import os
import re
import shutil
from pathlib import Path
from loguru import logger
from memory.facts import Facts

try:
    import pdfplumber
    _PDF = True
except ImportError:
    _PDF = False

try:
    import docx
    _DOCX = True
except ImportError:
    _DOCX = False

try:
    from rapidfuzz import fuzz as _fuzz
    _RAPIDFUZZ = True
except ImportError:
    _RAPIDFUZZ = False

SEARCH_DRIVES = ["C:/", "D:/"]
MAX_READ_CHARS = 4000   # cap file content sent to LLM
SKIP_DIRS = {
    "$Recycle.Bin", "Windows", "System32", "SysWOW64",
    "node_modules", ".git", "__pycache__", ".venv", "venv",
    "Program Files", "Program Files (x86)", "ProgramData",
    "AppData", "Recovery", "Boot", "EFI",
}
# Key files to read when exploring a project folder, in priority order
_KEY_FILE_NAMES = ["README.md", "readme.md", "README.txt", "readme.txt"]
_KEY_FILE_EXTS  = [".md", ".sln", ".csproj", ".fsproj", "requirements.txt",
                   "pyproject.toml", "package.json", "Dockerfile"]
_SEP_RE = re.compile(r"[-_\s]+")


def _normalize(s: str) -> str:
    """Collapse hyphens, underscores, and spaces to a single space for comparison."""
    return _SEP_RE.sub(" ", s).lower().strip()


def _matches(query_norm: str, name: str) -> bool:
    name_norm = _normalize(name)
    if query_norm in name_norm:
        return True
    if _RAPIDFUZZ and _fuzz.partial_ratio(query_norm, name_norm) >= 88:
        return True
    return False


def _find_key_file(folder: Path) -> Path | None:
    """Return the most informative file in a project folder, or None."""
    for name in _KEY_FILE_NAMES:
        p = folder / name
        if p.exists():
            return p
    for entry in folder.iterdir():
        if not entry.is_file():
            continue
        if entry.name in _KEY_FILE_EXTS or entry.suffix in _KEY_FILE_EXTS:
            return entry
    return None


class FileOps:
    def __init__(self, memory, narration, config: dict):
        self.memory = memory
        self.facts = Facts(memory)
        self.narration = narration
        drives = config.get("paths", {}).get("search_drives", SEARCH_DRIVES)
        self.search_drives = drives

    def run(self, params: dict) -> str:
        action = params.get("action", "search")
        query = params.get("query", "")

        if action == "search":
            return self._search(query)
        if action == "explore":
            return self._explore(query)
        if action == "read":
            return self._read(params.get("path", ""))
        if action == "copy":
            return self._copy(params.get("src", ""), params.get("dst", ""))
        if action == "move":
            return self._move(params.get("src", ""), params.get("dst", ""))
        return f"Unknown file action: {action}"

    def _search(self, query: str) -> str:
        # Memory-first: answer instantly if we've found this before
        cached = self.facts.recall_location(query)
        if cached and Path(cached).exists():
            logger.info(f"Memory hit for '{query}': {cached}")
            self.facts.remember_location(query, cached)  # refresh TTL
            return f"From memory: '{query}' is at {cached}"

        self.narration.say(f"Searching your PC for {query}...")
        query_norm = _normalize(query)
        matches = []
        logger.info(f"Search started | query='{query}' | normalized='{query_norm}' | drives={self.search_drives}")

        for drive in self.search_drives:
            logger.info(f"Scanning drive: {drive}")
            drive_matches = 0
            for root, dirs, files in os.walk(drive):
                dirs[:] = [d for d in dirs if d not in SKIP_DIRS]

                depth = root.replace("\\", "/").rstrip("/").count("/")
                if depth <= 2:
                    logger.debug(f"  → {root}")

                for name in dirs + files:
                    if _matches(query_norm, name):
                        path = os.path.join(root, name)
                        matches.append(path)
                        drive_matches += 1
                        logger.info(f"Match found: {path}")
                    if drive_matches >= 10:
                        break
                if drive_matches >= 10:
                    break

        logger.info(f"Search complete | query='{query}' | total matches={len(matches)}")

        if not matches:
            logger.info(f"No results found for '{query}'")
            return f"I couldn't find anything matching '{query}' on your PC."

        result = f"Found {len(matches)} result(s) for '{query}':\n" + "\n".join(matches[:5])
        if len(matches) > 5:
            result += f"\n...and {len(matches)-5} more."

        self.facts.remember_location(query, matches[0])
        return result

    def _explore(self, query: str) -> str:
        """Search for a project folder, then read its key file to explain what it does."""
        search_result = self._search(query)
        if "couldn't find" in search_result:
            return search_result

        # Pull the first path out of the result
        lines = [l for l in search_result.splitlines() if l and not l.startswith("Found") and not l.startswith("From memory:")]
        # "From memory:" line has the path directly after the colon
        if search_result.startswith("From memory:"):
            first_path = search_result.split("is at ", 1)[-1].strip()
        else:
            first_path = lines[0].strip() if lines else None

        if not first_path:
            return search_result

        p = Path(first_path)
        logger.info(f"Explore: found '{query}' at {p}")

        if p.is_dir():
            key_file = _find_key_file(p)
            if key_file:
                logger.info(f"Explore: reading key file {key_file.name}")
                self.narration.say(f"Found it. Reading {key_file.name}...")
                content = self._read(str(key_file))
                return f"Found '{query}' at {p}.\n\nKey file ({key_file.name}):\n{content}"
            else:
                # List top-level files/dirs as a summary
                entries = [e.name for e in p.iterdir()][:15]
                return f"Found '{query}' at {p}.\n\nContents: {', '.join(entries)}"
        else:
            # It's a file — read it directly
            self.narration.say(f"Found it. Reading {p.name}...")
            content = self._read(str(p))
            return f"Found '{query}' at {p}.\n\nContents:\n{content}"

    def _read(self, path: str) -> str:
        p = Path(path)
        if not p.exists():
            return f"File not found: {path}"

        self.narration.say(f"Reading {p.name}...")
        ext = p.suffix.lower()

        try:
            if ext == ".pdf" and _PDF:
                with pdfplumber.open(p) as pdf:
                    text = "\n".join(page.extract_text() or "" for page in pdf.pages[:5])
            elif ext == ".docx" and _DOCX:
                doc = docx.Document(p)
                text = "\n".join(para.text for para in doc.paragraphs)
            else:
                text = p.read_text(encoding="utf-8", errors="ignore")

            if len(text) > MAX_READ_CHARS:
                text = text[:MAX_READ_CHARS] + "\n...[truncated]"
            return text or "(file is empty)"
        except Exception as e:
            return f"Could not read {p.name}: {e}"

    def _copy(self, src: str, dst: str) -> str:
        try:
            s, d = Path(src), Path(dst)
            if s.is_dir():
                shutil.copytree(s, d)
            else:
                shutil.copy2(s, d)
            return f"Copied {s.name} to {d}."
        except Exception as e:
            return f"Copy failed: {e}"

    def _move(self, src: str, dst: str) -> str:
        try:
            shutil.move(src, dst)
            return f"Moved {Path(src).name} to {dst}."
        except Exception as e:
            return f"Move failed: {e}"
