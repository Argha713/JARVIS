"""
Direct API client for portal pages.

After the discoverer has intercepted and saved API endpoint URLs on a first visit,
every subsequent query for that page hits the API directly via httpx — no browser,
no DOM, no semantic search.  Total round-trip: ~300 ms.

Auth flow (Simplified HR / people.codeclouds.com):
  1. Call getAccessData with session cookies → get JWT access_token
  2. Add "Authorization: Bearer <token>" to all processRequest* calls
  3. Token cached in-memory for 12h (server expiry is 14 days)

Public surface:
    call_page(site_id, page_name, query)  -> dict | None
    format_answer(query, data)            -> str
    AuthExpired                           (exception)
"""
import json
import time

import httpx
from loguru import logger

from tools.web_engine import store
from tools.web_engine.discoverer import _extract_month_target

# Auth endpoint for Simplified HR — returns Bearer token using session cookies.
# NOT a data endpoint — never saved to api_endpoints table.
_GET_ACCESS_DATA_URL = "https://hr.besimplified.com/api/server/getAccessData"
_BEARER_TTL_SECONDS  = 12 * 3600   # re-fetch after 12h (server expiry is 14 days)

# How long a browser-fetched sample_response is trusted before we require a fresh
# Playwright run.  4 hours covers a full workday while still catching mid-day changes.
_SAMPLE_TTL_SECONDS  = 4 * 3600

# In-memory token cache: site_id → {"token": str, "fetched_at": float}
_bearer_cache: dict[str, dict] = {}


class AuthExpired(Exception):
    """Raised when the portal session is invalid (401 / redirect to login)."""


def _get_bearer_token(site_id: str, cookies: dict, force_refresh: bool = False) -> str | None:
    """
    Get a Bearer token for portal API calls.

    1. Check in-memory cache (TTL=12h). Return cached token if fresh.
    2. Otherwise call getAccessData with session cookies and cache the result.
    3. If getAccessData returns HTML (cookies expired), returns None.
    """
    if not force_refresh:
        cached = _bearer_cache.get(site_id)
        if cached:
            age = time.time() - cached["fetched_at"]
            if age < _BEARER_TTL_SECONDS:
                logger.debug("[API_CLIENT] Bearer token from cache (age={:.0f}s, ttl={}s)",
                             age, _BEARER_TTL_SECONDS)
                return cached["token"]
            logger.debug("[API_CLIENT] Bearer cache stale (age={:.0f}s) → refreshing", age)
        else:
            logger.debug("[API_CLIENT] No cached Bearer token → fetching fresh")
    else:
        logger.info("[API_CLIENT] Force-refresh Bearer token requested")

    logger.info("[API_CLIENT] Calling getAccessData: {}", _GET_ACCESS_DATA_URL)
    try:
        resp = httpx.get(
            _GET_ACCESS_DATA_URL,
            cookies=cookies,
            timeout=10.0,
            follow_redirects=False,
        )
        logger.info("[API_CLIENT] getAccessData → HTTP {} | Content-Type: {}",
                    resp.status_code, resp.headers.get("content-type", "?"))

        if resp.status_code != 200:
            logger.warning("[API_CLIENT] getAccessData failed: HTTP {} → no Bearer token",
                           resp.status_code)
            return None

        raw = resp.text
        if raw.lstrip().startswith("<") or "<!DOCTYPE" in raw[:100]:
            logger.warning("[API_CLIENT] getAccessData returned HTML (session cookies expired) "
                           "→ cannot get Bearer token → need re-login")
            return None

        data = resp.json()
        token = data.get("result", {}).get("access_token")
        expiry = data.get("result", {}).get("jwt_expiry", "?")

        if not token:
            logger.warning("[API_CLIENT] getAccessData response has no access_token field. "
                           "Keys: {}", list(data.get("result", {}).keys()))
            return None

        _bearer_cache[site_id] = {"token": token, "fetched_at": time.time()}
        logger.info("[API_CLIENT] Bearer token obtained and cached ✓ "
                    "(server expiry={}s ≈ {:.0f} days)", expiry, int(expiry or 0) / 86400)
        logger.debug("[API_CLIENT] Token preview: {}...{}", token[:20], token[-10:])
        return token

    except Exception as e:
        logger.warning("[API_CLIENT] getAccessData request failed: {} → no Bearer token", e)
        return None


def _invalidate_bearer(site_id: str) -> None:
    """Clear cached Bearer token — call when a request returns unexpected HTML."""
    removed = _bearer_cache.pop(site_id, None)
    if removed:
        logger.info("[API_CLIENT] Bearer token cache invalidated for {}", site_id)
    else:
        logger.debug("[API_CLIENT] Bearer cache already empty for {}", site_id)


def call_page(site_id: str, page_name: str, query: str) -> dict | None:
    """
    Call a saved API endpoint for a portal page and return the JSON response.

    Returns None if no endpoints have been discovered yet for this page.
    Raises AuthExpired if the session cookie is expired or invalid.
    """
    endpoints = store.get_api_endpoints(site_id, page_name)

    if not endpoints:
        logger.debug("[API_CLIENT] Decision: no endpoints saved for site={!r} page={!r} "
                     "→ skip API path, fall through to cache/discoverer", site_id, page_name)
        return None

    logger.info("[API_CLIENT] ── API call ─────────────────────────────")
    logger.info("[API_CLIENT] Site: {}  |  Page: {}", site_id, page_name)
    logger.info("[API_CLIENT] Query: {!r}", query)
    logger.info("[API_CLIENT] Saved endpoints: {}", len(endpoints))
    for i, ep in enumerate(endpoints, 1):
        logger.debug("[API_CLIENT]   Endpoint {}: method={} url={} body={}",
                     i, ep.get("method", "GET"), ep["url"],
                     "yes" if ep.get("body") else "none")

    # ── Fast path: merge ALL browser-fetched sample_responses if fresh ──────────
    # _fetch_endpoints_in_browser() stores fresh JSON from page.evaluate() fetch()
    # for every endpoint. Different processRequest* endpoints serve different data
    # (attendance stats, leave counts, request counts, etc.) — we must collect ALL
    # of them and merge before passing to format_answer(), so the LLM can pick the
    # value that actually answers the query (not just whatever endpoint happened first).
    from datetime import datetime, timezone as _tz
    merged: dict = {}
    fresh_ids: list[int] = []
    for ep in endpoints:
        sample_json   = ep.get("sample")
        discovered_at = ep.get("discovered_at")
        if not sample_json or not discovered_at:
            continue
        try:
            age = (datetime.now(_tz.utc) - datetime.fromisoformat(discovered_at)).total_seconds()
        except Exception:
            continue
        if age > _SAMPLE_TTL_SECONDS:
            logger.debug("[API_CLIENT] sample_response stale ({:.0f}s > {}s) for {}",
                         age, _SAMPLE_TTL_SECONDS, ep["url"])
            continue
        try:
            data = json.loads(sample_json)
            if not data:
                continue
            # Merge: use endpoint URL tail as namespace key to avoid collisions
            key = ep["url"].rstrip("/").rsplit("/", 1)[-1]  # e.g. "processRequest5"
            merged[key] = data
            fresh_ids.append(ep["id"])
            logger.debug("[API_CLIENT] Fast path collected sample from {} (age={:.0f}s)",
                         ep["url"], age)
        except Exception as e:
            logger.debug("[API_CLIENT] sample_response parse failed for {}: {}", ep["url"], e)

    if merged:
        logger.info("[API_CLIENT] Fast path ✓ — merged {} fresh endpoint(s), no browser needed",
                    len(merged))
        for ep_id in fresh_ids:
            store.touch_endpoint(ep_id)
        return merged

    logger.debug("[API_CLIENT] No fresh sample_response → falling through to httpx")

    session = store.load_session(site_id)
    if not session:
        logger.warning("[API_CLIENT] Decision: no session saved → raising AuthExpired")
        raise AuthExpired("No session saved for " + site_id)

    cookies = {c["name"]: c["value"] for c in session.get("cookies", [])}
    logger.debug("[API_CLIENT] Session cookies loaded: {} cookie(s)", len(cookies))

    # ── Step 1: Get Bearer token via getAccessData ───────────────────────────
    # The portal (Simplified HR) requires Authorization: Bearer <JWT> on all
    # data API calls. The token is obtained by calling getAccessData with the
    # session cookies, then cached in-memory for 12 hours.
    bearer = _get_bearer_token(site_id, cookies)
    if not bearer:
        logger.warning("[API_CLIENT] Decision: could not obtain Bearer token "
                       "→ raising AuthExpired (session cookies likely expired)")
        raise AuthExpired("Could not obtain Bearer token for " + site_id)

    headers = {
        "Accept":           "application/json",
        "X-Requested-With": "XMLHttpRequest",
        "Content-Type":     "application/json",
        "Authorization":    f"Bearer {bearer}",
    }
    logger.info("[API_CLIENT] Headers: Authorization=Bearer <token>, Accept=application/json")

    # Resolve optional month parameter from the query text
    params: dict = {}
    month_target = _extract_month_target(query)
    if month_target:
        params["month"] = f"{month_target.year}-{month_target.month:02d}"
        logger.info("[API_CLIENT] Decision: month target found in query → adding param month={}",
                    params["month"])
    else:
        logger.debug("[API_CLIENT] Decision: no month target in query → no month param")

    for i, ep in enumerate(endpoints, 1):
        url    = ep["url"]
        method = ep.get("method", "GET").upper()
        body   = ep.get("body")  # raw POST body string captured during discovery

        full_url = f"{url}?{'&'.join(f'{k}={v}' for k, v in params.items())}" if params else url
        logger.info("[API_CLIENT] ── Endpoint {}/{} ─────────────────────────────", i, len(endpoints))
        logger.info("[API_CLIENT]   Method: {}", method)
        logger.info("[API_CLIENT]   URL:    {}", full_url)
        if body:
            logger.debug("[API_CLIENT]   Body:   {}", body[:300])
        else:
            logger.debug("[API_CLIENT]   Body:   none (GET or no body captured)")

        try:
            if method == "POST":
                logger.debug("[API_CLIENT] Decision: method=POST → using httpx.post with stored body")
                # Try to parse body as JSON for proper Content-Type handling
                json_body = None
                raw_body  = None
                if body:
                    try:
                        json_body = json.loads(body)
                        logger.debug("[API_CLIENT]   POST body parsed as JSON: {}", json_body)
                    except Exception:
                        raw_body = body.encode() if isinstance(body, str) else body
                        logger.debug("[API_CLIENT]   POST body is raw (not JSON): {!r}", body[:100])
                resp = httpx.post(
                    url,
                    cookies=cookies,
                    headers=headers,
                    params=params,
                    json=json_body,
                    content=raw_body,
                    timeout=10.0,
                    follow_redirects=False,
                )
            else:
                logger.debug("[API_CLIENT] Decision: method=GET → using httpx.get")
                resp = httpx.get(
                    url,
                    cookies=cookies,
                    headers=headers,
                    params=params,
                    timeout=10.0,
                    follow_redirects=False,
                )

            logger.info("[API_CLIENT]   Response: HTTP {} | Content-Type: {}",
                        resp.status_code, resp.headers.get("content-type", "unknown"))

            # ── Auth failures ────────────────────────────────────────────────
            if resp.status_code in (401, 403):
                logger.warning("[API_CLIENT] Decision: HTTP {} → session expired → raising AuthExpired",
                               resp.status_code)
                raise AuthExpired(f"HTTP {resp.status_code} from {url}")

            # ── Redirect = session bounce to login ───────────────────────────
            if resp.is_redirect or resp.status_code == 302:
                location = resp.headers.get("location", "?")
                logger.warning("[API_CLIENT] Decision: redirect → {} → session expired → raising AuthExpired",
                               location)
                raise AuthExpired(f"Redirect (session expired): {url}")

            # ── Not found ────────────────────────────────────────────────────
            if resp.status_code == 404:
                logger.warning("[API_CLIENT] Decision: 404 → endpoint doesn't exist → removing from DB: {}",
                               url)
                _delete_endpoint(ep["id"], url)
                continue

            # ── Server error ─────────────────────────────────────────────────
            if resp.status_code >= 500:
                logger.warning("[API_CLIENT] Decision: server error {} → skip this endpoint, try next",
                               resp.status_code)
                continue

            # ── Success ──────────────────────────────────────────────────────
            if resp.status_code == 200:
                # Log raw response text first (always)
                raw_text = resp.text
                logger.debug("[API_CLIENT]   Raw response text ({} bytes):\n{}",
                             len(raw_text),
                             raw_text[:4000] + (" ... [truncated]" if len(raw_text) > 4000 else ""))

                # Detect HTML — portal returned login/logout page instead of JSON.
                # This means the Bearer token was rejected. Invalidate it and
                # try a one-time refresh — if fresh token still gives HTML, the
                # session itself is expired (raise AuthExpired).
                if raw_text.lstrip().startswith("<") or "<!DOCTYPE" in raw_text[:100]:
                    logger.warning("[API_CLIENT] Decision: response is HTML → Bearer token rejected "
                                   "by endpoint {} → invalidating cache and fetching fresh token", url)
                    _invalidate_bearer(site_id)
                    fresh_bearer = _get_bearer_token(site_id, cookies, force_refresh=True)
                    if not fresh_bearer:
                        logger.warning("[API_CLIENT] Decision: fresh token fetch failed → "
                                       "session expired → raising AuthExpired")
                        raise AuthExpired(f"Bearer token refresh failed for {url}")
                    # Retry this one endpoint with fresh token
                    headers["Authorization"] = f"Bearer {fresh_bearer}"
                    logger.info("[API_CLIENT] Retrying {} with fresh Bearer token", url)
                    try:
                        if method == "POST":
                            resp = httpx.post(url, cookies=cookies, headers=headers,
                                              params=params, json=json_body, content=raw_body,
                                              timeout=10.0, follow_redirects=False)
                        else:
                            resp = httpx.get(url, cookies=cookies, headers=headers,
                                             params=params, timeout=10.0, follow_redirects=False)
                        raw_text = resp.text
                        logger.info("[API_CLIENT] Retry response: HTTP {}", resp.status_code)
                        if raw_text.lstrip().startswith("<") or "<!DOCTYPE" in raw_text[:100]:
                            logger.warning("[API_CLIENT] Decision: retry also returned HTML → "
                                           "session truly expired → raising AuthExpired")
                            raise AuthExpired(f"HTML even with fresh token: {url}")
                        logger.info("[API_CLIENT] Retry succeeded with fresh token ✓")
                        # Fall through to JSON parsing below
                    except AuthExpired:
                        raise
                    except Exception as retry_err:
                        logger.warning("[API_CLIENT] Retry failed: {} → skip endpoint", retry_err)
                        continue

                try:
                    data = resp.json()
                except Exception as parse_err:
                    logger.warning("[API_CLIENT] Decision: response is not valid JSON ({}) "
                                   "→ skipping endpoint: {}", parse_err, url)
                    continue

                # Log parsed JSON
                raw_json = json.dumps(data, indent=2, default=str)
                logger.debug("[API_CLIENT]   Full response ({} bytes):\n{}",
                             len(raw_json),
                             raw_json[:4000] + (" ... [truncated]" if len(raw_json) > 4000 else ""))

                # Skip auth/session endpoints — they return tokens not HR data
                if isinstance(data, dict):
                    nested = data.get("result", data)
                    if isinstance(nested, dict):
                        auth_keys = {"access_token", "refresh_token", "token",
                                     "jwt_expiry", "bearer_token"}
                        found = auth_keys & nested.keys()
                        if found:
                            logger.warning("[API_CLIENT] Decision: response contains auth token keys {} "
                                           "→ not HR data → removing from DB: {}", found, url)
                            _delete_endpoint(ep["id"], url)
                            continue
                        else:
                            logger.debug("[API_CLIENT] Decision: no auth keys found → response looks like data")

                # Log what we got
                if isinstance(data, dict):
                    logger.info("[API_CLIENT]   Success — {} field(s): {}", len(data), list(data.keys()))
                    _log_data_preview(data)
                elif isinstance(data, list):
                    logger.info("[API_CLIENT]   Success — list with {} item(s)", len(data))
                else:
                    logger.info("[API_CLIENT]   Success — data type: {}", type(data).__name__)

                store.touch_endpoint(ep["id"])
                logger.info("[API_CLIENT] Decision: endpoint {} succeeded → returning data", url)
                return data

            logger.warning("[API_CLIENT] Decision: unexpected status {} → skipping endpoint: {}",
                           resp.status_code, url)

        except AuthExpired:
            raise
        except Exception as e:
            logger.warning("[API_CLIENT] Decision: request failed ({}) → skip endpoint: {}", e, url)
            continue

    logger.info("[API_CLIENT] Decision: all {} endpoints exhausted without valid data "
                "→ returning None → will fall through to discoverer", len(endpoints))
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
    """Log each field of the response dict (nested objects summarised)."""
    for k, v in data.items():
        if isinstance(v, dict):
            logger.debug("[API_CLIENT]   {} = {{object, {} keys: {}}}", k, len(v), list(v.keys()))
        elif isinstance(v, list):
            logger.debug("[API_CLIENT]   {} = [list, {} items]", k, len(v))
        else:
            logger.debug("[API_CLIENT]   {} = {!r}", k, v)


def format_answer(query: str, data: dict, config: dict | None = None) -> str:
    """
    Ask the LLM to format a natural-language answer from raw API JSON.
    Uses OpenAI if configured, falls back to Ollama, then plain text.
    """
    logger.info("[API_CLIENT] format_answer: query={!r} | data keys: {}", query, list(data.keys()))

    prompt = (
        f"The user asked: \"{query}\"\n\n"
        f"The portal returned this data:\n{json.dumps(data, indent=2, default=str)}\n\n"
        "Answer the user's question in one or two clear spoken sentences. "
        "Use the exact values from the data. No markdown, no bullet points. "
        "Speak as if answering aloud to your boss."
    )
    logger.debug("[API_CLIENT] LLM prompt:\n{}", prompt)

    # Try OpenAI first (already connected, higher quality)
    openai_key = (config or {}).get("llm", {}).get("openai_api_key", "")
    if not openai_key:
        try:
            import json as _j
            from pathlib import Path
            _cfg = _j.loads(Path("config.json").read_text(encoding="utf-8"))
            openai_key = _cfg.get("llm", {}).get("openai_api_key", "")
        except Exception:
            pass

    if openai_key:
        logger.debug("[API_CLIENT] Decision: OpenAI key available → using gpt-4o-mini to format answer")
        try:
            import openai as _openai
            client = _openai.OpenAI(api_key=openai_key)
            response = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role": "user", "content": prompt}],
                max_tokens=120,
            )
            answer = response.choices[0].message.content.strip()
            logger.info("[API_CLIENT] LLM answer (openai): {!r}", answer)
            return answer
        except Exception as e:
            logger.warning("[API_CLIENT] Decision: OpenAI failed ({}) → falling back to Ollama", e)
    else:
        logger.debug("[API_CLIENT] Decision: no OpenAI key → trying Ollama")

    try:
        import ollama
        client = ollama.Client(host="http://localhost:11434")
        response = client.chat(
            model="phi3",
            messages=[{"role": "user", "content": prompt}],
            options={"num_predict": 120},
        )
        answer = response["message"]["content"].strip()
        logger.info("[API_CLIENT] LLM answer (ollama): {!r}", answer)
        return answer
    except Exception as e:
        logger.warning("[API_CLIENT] Decision: Ollama failed ({}) → using plain text fallback", e)

    # Last resort: flat key-value dump
    lines = [f"{k}: {v}" for k, v in data.items() if not isinstance(v, (dict, list))]
    fallback = ". ".join(lines) if lines else str(data)
    logger.info("[API_CLIENT] Plain fallback answer: {!r}", fallback)
    return fallback
