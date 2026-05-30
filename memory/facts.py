from memory.chroma_store import ChromaStore


class Facts:
    """High-level memory helpers for file locations and user facts."""

    def __init__(self, store: ChromaStore):
        self._store = store

    def remember_location(self, name: str, path: str) -> None:
        self._store.remember(
            f"'{name}' is located at: {path}",
            {"type": "file_location", "name": name.lower(), "path": path},
        )

    def recall_location(self, query: str) -> str | None:
        """Returns stored path for query, or None if not in memory."""
        hits = self._store.recall(query + " location", n=1)
        if not hits:
            return None
        text = hits[0]
        if " is located at: " in text:
            return text.split(" is located at: ", 1)[1].strip()
        return None
