"""
Portal discovery script — run once to map people.codeclouds.com.
Opens a visible browser window. You navigate normally; press Enter in the
terminal after each page to capture a screenshot + page source.
Saves everything to data/portal_discovery/.
"""
import json
import os
import time
from pathlib import Path
from playwright.sync_api import sync_playwright

PORTAL_URL   = "https://people.codeclouds.com/"
SESSION_FILE = "data/portal_session.json"
OUT_DIR      = Path("data/portal_discovery")

PAGES_TO_VISIT = [
    "dashboard / home",
    "timesheet",
    "leave balance",
    "leave application",
    "seat booking",
    "activity / profile activity",
    "support / raise ticket",
    "any other section you use",
]


def save_session(context, path: str) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    storage = context.storage_state()
    with open(path, "w") as f:
        json.dump(storage, f)
    print(f"  ✓ Session saved → {path}")


def discover():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    index = []

    with sync_playwright() as p:
        # Launch VISIBLE browser (not headless) so you can log in with Google
        browser = p.chromium.launch(headless=False, slow_mo=100)

        # Load existing session if available
        if Path(SESSION_FILE).exists():
            print(f"Loading saved session from {SESSION_FILE}...")
            context = browser.new_context(storage_state=SESSION_FILE)
        else:
            context = browser.new_context()

        page = context.new_page()
        page.goto(PORTAL_URL)

        print("\n" + "="*60)
        print("JARVIS Portal Discovery")
        print("="*60)
        print("A browser window has opened.")
        print("Log in with Google if prompted.")
        print()
        print("We'll visit these sections:")
        for i, name in enumerate(PAGES_TO_VISIT, 1):
            print(f"  {i}. {name}")
        print()

        input("Press Enter once you are logged in and on the dashboard... ")
        save_session(context, SESSION_FILE)

        # Capture each page the user navigates to
        capture_num = 0
        while True:
            print()
            print("─"*60)
            page_name = input("What page are you on? (type name, or 'done' to finish): ").strip()
            if page_name.lower() in ("done", "exit", "quit", ""):
                break

            capture_num += 1
            url = page.url
            slug = page_name.lower().replace(" ", "_").replace("/", "_")
            screenshot_path = OUT_DIR / f"{capture_num:02d}_{slug}.png"
            html_path       = OUT_DIR / f"{capture_num:02d}_{slug}.html"

            page.screenshot(path=str(screenshot_path), full_page=True)
            html = page.content()
            html_path.write_text(html, encoding="utf-8")

            entry = {"num": capture_num, "name": page_name, "url": url,
                     "screenshot": str(screenshot_path), "html": str(html_path)}
            index.append(entry)

            print(f"  ✓ Captured: {screenshot_path.name}")
            print(f"  ✓ URL: {url}")
            print()
            print("Navigate to the NEXT section in the browser, then come back here.")

        # Save index
        index_path = OUT_DIR / "index.json"
        index_path.write_text(json.dumps(index, indent=2), encoding="utf-8")
        print()
        print("="*60)
        print(f"Discovery complete. {capture_num} pages captured.")
        print(f"Index saved → {index_path}")
        print(f"Screenshots → {OUT_DIR}/")
        print("="*60)

        browser.close()


if __name__ == "__main__":
    os.chdir(Path(__file__).parent.parent)  # run from project root
    discover()
