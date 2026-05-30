import re
from playwright.sync_api import sync_playwright
from loguru import logger

MAX_PAGE_CHARS = 4000


class Browser:
    def __init__(self, narration):
        self.narration = narration

    def run(self, params: dict) -> str:
        action = params.get("action", "read")
        url = params.get("url", "")
        if action == "read":
            return self._read(url)
        return f"Unknown browser action: {action}"

    def _read(self, url: str) -> str:
        if not url:
            return "No URL provided."
        if not url.startswith("http"):
            url = "https://" + url

        self.narration.say(f"Opening the page...")
        logger.info(f"Browser: reading {url}")

        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                page = browser.new_page()
                page.goto(url, timeout=20000, wait_until="domcontentloaded")
                text = page.evaluate("""() => {
                    document.querySelectorAll('script, style, nav, footer, iframe').forEach(e => e.remove());
                    return document.body ? document.body.innerText : '';
                }""")
                browser.close()
        except Exception as e:
            logger.error(f"Browser failed for {url}: {e}")
            return f"Could not open {url}: {e}"

        text = re.sub(r'\n{3,}', '\n\n', text.strip())
        if len(text) > MAX_PAGE_CHARS:
            text = text[:MAX_PAGE_CHARS] + "\n...[truncated]"

        logger.info(f"Browser: extracted {len(text)} chars from {url}")
        return f"Page content from {url}:\n{text}"
