"""
Site Analyzer — LLM-based analysis of a discovered page.

Called by discovery_engine.py after extract sections + forms from a page.
Uses the smart model to infer site name, page purpose, tags, and actions.

Output schema:
{
  "site":  { "name": str, "tags": [str, ...] },
  "page":  { "name": str, "purpose": str, "tags": [str, ...] },
  "actions": [
    { "name": str, "description": str, "fields": [...],
      "submit_selector": str, "wait_ms": int, "tags": [str, ...] }
  ]
}
"""
import json
from urllib.parse import urlparse
from loguru import logger

_SYSTEM_PROMPT = """\
You are analyzing a web page for JARVIS, a voice assistant.
Given the URL, extracted data sections, and any forms on the page, return a JSON object.

Rules:
- Tags = short phrases a user might speak to JARVIS to reach this page ("attendance", "my leaves", "check in").
  Include synonyms and STT-friendly variations.
- site.tags: broad identifiers (domain, company name, type of site).
- page.tags: specific to what this page shows or does.
- Every form is an action — login, search box, EOD report, anything.
- Use snake_case action names ("submit_work_journal", "search_portal").
- wait_ms default 3000; adjust if the form is clearly slow (file upload etc.).

Return ONLY valid JSON matching this exact structure (no markdown):
{
  "site": { "name": "...", "tags": ["...", "..."] },
  "page": { "name": "...", "purpose": "...", "tags": ["...", "..."] },
  "actions": [
    {
      "name": "...",
      "description": "...",
      "fields": [{"selector": "...", "label": "...", "type": "..."}],
      "submit_selector": "...",
      "wait_ms": 3000,
      "tags": ["...", "..."]
    }
  ]
}"""


def analyze(url: str, sections: list, forms: dict, config: dict) -> dict:
    """
    Call the smart model to analyze a discovered page.
    Returns the structured dict or a minimal fallback on failure.
    """
    sections_text = "\n".join(
        f"- {s.get('label', s.get('value', ''))}: {s.get('value', '')}"
        for s in sections[:30]
    ) or "(no data sections extracted)"

    form_fields  = forms.get("fields",  [])
    form_submits = forms.get("submits", [])
    if form_fields:
        form_text = "Fields:\n" + "\n".join(
            f"  [{f.get('type','?')}] label={f.get('label','')!r} "
            f"selector={f.get('selector','')!r} placeholder={f.get('placeholder','')!r}"
            for f in form_fields
        )
        if form_submits:
            form_text += "\nSubmit buttons:\n" + "\n".join(
                f"  {s.get('text','')!r} selector={s.get('selector','')!r}"
                for s in form_submits
            )
    else:
        form_text = "(no forms found)"

    user_msg = (
        f"URL: {url}\n\n"
        f"Extracted data sections:\n{sections_text}\n\n"
        f"Forms found:\n{form_text}\n\n"
        "Generate the JSON analysis for this page."
    )

    provider = config.get("llm", {}).get("provider", "openai")

    try:
        if provider == "openai":
            import openai
            client = openai.OpenAI(api_key=config["llm"].get("openai_api_key", ""))
            model  = config["llm"].get("openai_smart_model", "gpt-4o")
            resp = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user",   "content": user_msg},
                ],
                response_format={"type": "json_object"},
                max_tokens=1000,
            )
            raw = resp.choices[0].message.content.strip()
        else:
            import ollama
            client = ollama.Client(host=config["llm"].get("ollama_host", "http://localhost:11434"))
            model  = config["llm"].get("smart_model", "llama3.1:8b")
            resp = client.chat(
                model=model,
                messages=[
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user",   "content": user_msg},
                ],
            )
            raw = resp["message"]["content"].strip()
            # Strip markdown code fences if Ollama wraps output
            if raw.startswith("```"):
                raw = raw.split("```")[1].lstrip("json").strip()

        result = json.loads(raw)
        logger.info(
            "[ANALYZER] {!r} → site={!r} page={!r} actions={}",
            url,
            result.get("site", {}).get("name"),
            result.get("page", {}).get("name"),
            len(result.get("actions", [])),
        )
        return result

    except Exception as exc:
        logger.error("[ANALYZER] LLM analysis failed for {!r}: {}", url, exc)
        parsed = urlparse(url)
        return {
            "site":    {"name": parsed.netloc, "tags": [parsed.netloc.split(".")[0]]},
            "page":    {"name": parsed.path.strip("/").replace("/", "_") or "home",
                        "purpose": "Unknown page", "tags": []},
            "actions": [],
        }
