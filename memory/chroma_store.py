import chromadb
from loguru import logger

_DB_PATH = "data/chromadb"


class ChromaStore:
    def __init__(self):
        self._client = chromadb.PersistentClient(path=_DB_PATH)
        self._facts = self._client.get_or_create_collection("jarvis_facts")

    def prewarm(self) -> None:
        """Trigger embedding model download synchronously (first-run only, ~80 MB)."""
        try:
            self._facts.upsert(ids=["__prewarm__"], documents=["init"], metadatas=[{"type": "prewarm"}])
            self._facts.delete(ids=["__prewarm__"])
            logger.debug("ChromaDB embedding model ready.")
        except Exception as e:
            logger.warning(f"ChromaDB prewarm failed (non-fatal): {e}")

    def remember(self, text: str, metadata: dict | None = None) -> None:
        import hashlib
        doc_id = hashlib.md5(text.encode()).hexdigest()
        self._facts.upsert(
            ids=[doc_id],
            documents=[text],
            metadatas=[metadata or {}],
        )
        logger.debug(f"Memory stored: {text[:80]}")

    def recall(self, query: str, n: int = 3) -> list[str]:
        results = self._facts.query(query_texts=[query], n_results=min(n, self._facts.count()))
        docs = results.get("documents", [[]])[0]
        if docs:
            logger.debug(f"Memory recalled {len(docs)} result(s) for: {query[:60]}")
        return docs

    def recall_exact(self, metadata_filter: dict) -> list[str]:
        """Exact metadata match — e.g. find all entries with type='file_location'."""
        results = self._facts.get(where=metadata_filter)
        return results.get("documents", [])

    def forget(self, metadata_filter: dict) -> None:
        results = self._facts.get(where=metadata_filter)
        ids = results.get("ids", [])
        if ids:
            self._facts.delete(ids=ids)
            logger.debug(f"Memory deleted {len(ids)} entries matching {metadata_filter}")
