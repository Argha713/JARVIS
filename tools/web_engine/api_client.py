"""
Direct API client for portal pages.

After the discoverer has intercepted and saved API endpoint URLs on a first visit,
every subsequent query for that page hits the API directly via httpx — no browser,
no DOM, no semantic search.  Total round-trip: ~300 ms.

Public surface:
    call_page(site_id, page_name, query)  -> dict | None
    format_answer(query, data)            -> str
    AuthExpired                           (exception)
"""
import json

import httpx
from loguru import logger

from tools.web_engine import store
from tools.web_engine.discoverer import _extract_month_target


class AuthExpired(Exception):
    """Raised when the portal session is invalid (401 / redirect to login)."""


def call_page(site_id: str, page_name: str, query: str) -> dict | None:
    """
    Call a saved API endpoint for a portal page and return the JSON response.

    Returns None if no endpoints have been discovered yet for this page.
    Raises AuthExpired if the session cookie is expired or invalid.
    """
    endpoints = store.get_api_endpoints(site_id, page_name)

    if not endpoints:
        logger.debug("[API_CLIENT] No endpoints saved for site={!r} page={!r} — skipping API path",
                     site_id, page_name)
        return None

    logger.info("[API_CLIENT] ── API call ─────────────────────────────")
    logger.info("[API_CLIENT] Site: {}  |  Page: {}", site_id, page_name)
    logger.info("[API_CLIENT] Query: {!r}", query)
    logger.info("[API_CLIENT] Saved endpoints: {}", len(endpoints))

    session = store.load_session(site_id)
    if not session:
        raise AuthExpired("No session saved for " + site_id)

    cookies = {c["name"]: c["value"] for c in session.get("cookies", [])}
    logger.debug("[API_CLIENT] Session cookies loaded: {} cookie(s)", len(cookies))

    headers = {
        "Accept":           "application/json",
        "X-Requested-With": "XMLHttpRequest",
    }

    # Resolve optional month parameter from the query text
    params: dict = {}
    month_target = _extract_month_target(query)
    if month_target:
        params["month"] = f"{month_target.year}-{month_target.month:02d}"
        logger.info("[API_CLIENT] Month param resolved: {}", params["month"])

    for i, ep in enumerate(endpoints, 1):
        url = ep["url"]
        full_url = f"{url}?{'&'.join(f'{k}={v}' for k, v in params.items())}" if params else url
        logger.info("[API_CLIENT] Trying endpoint {}/{}: {}", i, len(endpoints), full_url)

        try:
            resp = httpx.get(
                url,
                cookies=cookies,
                headers=headers,
                params=params,
                timeout=10.0,
                follow_redirects=False,
            )

            logger.info("[API_CLIENT] Response: HTTP {}", resp.status_code)

            if resp.status_code in (401, 403):
                logger.warning("[API_CLIENT] Auth failure ({}) — session likely expired", resp.status_code)
                raise AuthExpired(f"HTTP {resp.status_code} from {url}")

            # 302 = portal bouncing us to Google login
            if resp.is_redirect or resp.status_code == 302:
                location = resp.headers.get("location", "?")
                logger.warning("[API_CLIENT] Redirect → {} (session expired)", location)
                raise AuthExpired(f"Redirect (session expired): {url}")

            if resp.status_code == 404:
                logger.warning("[API_CLIENT] 404 — endpoint does not exist, removing from DB: {}", url)
                _delete_endpoint(ep["id"], url)
                continue

            if resp.status_code >= 500:
                logger.warning("[API_CLIENT] Server error {} — skipping this endpoint", resp.status_code)
                continue

            if resp.status_code == 200:
                try:
                    data = resp.json()
                except Exception as parse_err:
                    logger.warning("[API_CLIENT] Response is not JSON: {}", parse_err)
                    continue

                if isinstance(data, dict):
                    logger.info("[API_CLIENT] Success — {} field(s): {}",
                                len(data), list(data.keys()))
                    _log_data_preview(data)
                elif isinstance(data, list):
                    logger.info("[API_CLIENT] Success — list with {} item(s)", len(data))
                else:
                    logger.info("[API_CLIENT] Success — data type: {}", type(data).__name__)

                # Full response dump so we can evaluate what each endpoint actually returns
                raw_json = json.dumps(data, indent=2, default=str)
                logger.debug("[API_CLIENT] Full response ({} bytes):\n{}", len(raw_json),
                             raw_json[:4000] + (" ... [truncated]" if len(raw_json) > 4000 else ""))

                store.touch_endpoint(ep["id"])
                return data

            logger.warning("[API_CLIENT] Unexpected status {} from {}", resp.status_code, url)

        except AuthExpired:
            raise
        except Exception as e:
            logger.warning("[API_CLIENT] Request failed for {}: {}", url, e)
            continue

    logger.info("[API_CLIENT] All endpoints exhausted — no data returned")
    return None


def _delete_endpoint(endpoint_id: int, url: str) -> None:
    """Remove a known-bad endpoint from the DB so it isn't retried."""
    try:
        from tools.web_engine.store import _conn
        con = _conn()
        try:
            con.execute("DELETE FROM api_endpoints WHERE id=?", (endpoint_id,))
            con.commit()
            logger.info("[API_CLIENT] Endpoint removed from DB (id={}): {}", endpoint_id, url)
        finally:
            con.close()
    except Exception as e:
        logger.debug("[API_CLIENT] Could not remove endpoint {}: {}", endpoint_id, e)


def _log_data_preview(data: dict) -> None:
    """Log a condensed preview of the response data."""
    for k, v in data.items():
        if isinstance(v, (dict, list)):
            logger.debug("[API_CLIENT]   {} = [nested {} items]",
                         k, len(v) if isinstance(v, list) else "object")
        else:
            logger.debug("[API_CLIENT]   {} = {!r}", k, v)


def format_answer(query: str, data: dict, config: dict | None = None) -> str:
    """
    Ask the LLM to format a natural-language answer from raw API JSON.
    Uses OpenAI if configured (same provider as the rest of JARVIS), falls back
    to Ollama, then to a plain key-value listing if both are unreachable.
    """
    logger.info("[API_CLIENT] Formatting answer via LLM | data keys: {}", list(data.keys()))

    prompt = (
        f"The user asked: \"{query}\"\n\n"
        f"The portal returned this data:\n{json.dumps(data, indent=2, default=str)}\n\n"
        "Answer the user's question in one or two clear spoken sentences. "
        "Use the exact values from the data. No markdown, no bullet points. "
        "Speak as if answering aloud to your boss."
    )

    # Try OpenAI first (already connected, higher quality)
    openai_key = (config or {}).get("llm", {}).get("openai_api_key", "")
    if not openai_key:
        # Load from config.json directly if not passed in
        try:
            import json as _j
            from pathlib import Path
            _cfg = _j.loads(Path("config.json").read_text(encoding="utf-8"))
            openai_key = _cfg.get("llm", {}).get("openai_api_key", "")
        except Exception:
            pass

    if openai_key:
        try:
            import openai as _openai
            client = _openai.OpenAI(api_key=openai_key)
            response = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role": "user", "content": prompt}],
                max_tokens=120,
            )
            answer = response.choices[0].message.content.strip()
            logger.info("[API_CLIENT] LLM answer (openai): {!r}", answer[:120])
            return answer
        except Exception as e:
            logger.warning("[API_CLIENT] OpenAI format failed: {} — trying Ollama", e)

    # Fallback: Ollama
    try:
        import ollama
        client = ollama.Client(host="http://localhost:11434")
        response = client.chat(
            model="phi3",
            messages=[{"role": "user", "content": prompt}],
            options={"num_predict": 120},
        )
        answer = response["message"]["content"].strip()
        logger.info("[API_CLIENT] LLM answer (ollama): {!r}", answer[:120])
        return answer
    except Exception as e:
        logger.warning("[API_CLIENT] Ollama format failed: {} — using plain fallback", e)

    # Last resort: flat key-value dump (no nested objects)
    lines = [f"{k}: {v}" for k, v in data.items() if not isinstance(v, (dict, list))]
    return ". ".join(lines) if lines else str(data)
