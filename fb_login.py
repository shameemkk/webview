"""
Generate a reusable Facebook session for the scraper (server.py).

WHY: Facebook almost always blocks an automated login coming from a server /
datacenter IP (captcha, "suspicious login" checkpoint), and 2FA can't be typed by
a bot. So instead of letting the server log in, you log in ONCE here — locally, in
a real visible browser, on your home IP, doing any 2FA / captcha by hand — and this
saves the session cookies to fb_state.json. server.py loads that file on startup
and reuses the session, so it never has to log in itself.

USAGE (run on your own machine, NOT the server):
    pip install playwright
    playwright install chromium
    python fb_login.py

It writes two files (both gitignored — they're secrets, don't commit them):
    fb_state.json      - the session, for a server that reads the file
    fb_state.b64.txt   - the same session base64-encoded, to paste into the
                         FB_STATE_B64 env var on a PaaS host (no file upload needed)
The FB_STATE_B64 value is also printed to the console.

Sessions don't last forever; re-run this when the server starts hitting the wall
again.
"""
import base64
import json
import os
import sys
import time

from playwright.sync_api import sync_playwright

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.environ.get("FB_STATE_FILE") or os.path.join(HERE, "fb_state.json")
B64_FILE = os.path.join(HERE, "fb_state.b64.txt")
TIMEOUT_S = int(os.environ.get("FB_LOGIN_TIMEOUT_S", "600"))  # 10 min to log in by hand


def main() -> int:
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=False)  # visible: you log in by hand
        context = browser.new_context()
        page = context.new_page()
        page.goto("https://www.facebook.com/login/")

        print("\nA browser window opened.")
        print("-> Log in to Facebook there (do any 2FA / captcha yourself).")
        print(f"-> Waiting up to {TIMEOUT_S}s for a logged-in session...\n")

        deadline = time.time() + TIMEOUT_S
        while time.time() < deadline:
            try:
                cookies = context.cookies("https://www.facebook.com")
            except Exception:
                cookies = []
            if any(c.get("name") == "c_user" for c in cookies):
                break
            time.sleep(2)
        else:
            print("Timed out — no session detected. Nothing saved.", file=sys.stderr)
            browser.close()
            return 1

        # Give the session a moment to fully settle, then grab all cookies.
        time.sleep(2)
        cookies = context.cookies()
        payload = json.dumps({"cookies": cookies})
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            f.write(payload)

        # Same session as base64 — paste this into the FB_STATE_B64 env var.
        b64 = base64.b64encode(payload.encode("utf-8")).decode("ascii")
        with open(B64_FILE, "w", encoding="utf-8") as f:
            f.write(b64)

        print(f"\nSaved {len(cookies)} cookies to {STATE_FILE}")
        print(f"Base64 written to {B64_FILE}")
        print("\n--- For a PaaS deploy, set this env var (also saved to the file above): ---")
        print(f"FB_STATE_B64={b64}")
        print("\nThen redeploy. (For a file-based server instead, upload fb_state.json.)")
        browser.close()
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
