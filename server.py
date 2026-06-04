"""
Stealth scrape API — give a URL, get back the page's text and a screenshot URL.

Run:
    pip install -r requirements.txt
    playwright install chromium
    python server.py                 # serves on http://0.0.0.0:8080

Endpoints:
    GET  /health
    GET  /shots/<file>.png                      (static — the saved screenshots)
    POST /scrape   {"url": "...", "full_page": true, "scroll": true, "screenshot": true}
    GET  /scrape?url=...&full_page=true&scroll=true&screenshot=true

Response (JSON):
    {
      "success": true,
      "url": "...", "final_url": "...", "status": 200, "title": "...",
      "text": "<rendered visible text>",
      "screenshot_url": "http://host:8080/shots/<id>.png",   # null if screenshot=false
      "screenshot_file": "<id>.png"
    }

Each screenshot is written to ./shots and served back as a URL. Pass "inline": true
to get the raw base64 in the response instead of saving a file.

A single Chrome instance is shared across requests; each request runs in its own
isolated context with a fresh virtual identity + stealth patches. Built on aiohttp
so it shares Playwright's asyncio event loop with no extra moving parts.

Facebook login (optional):
    Set FB_EMAIL and FB_PASSWORD in .env. When a requested facebook.com URL bounces
    to the login wall, the server logs in once, caches the session cookies (in
    FB_STATE_FILE, default ./fb_state.json) and reuses them on later requests, then
    reloads the target page so you get the real content instead of the login form.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import time
import uuid
from typing import Optional

import dotenv
from aiohttp import web

from browser import StealthBrowser

dotenv.load_dotenv()

HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8080"))
HEADLESS = os.environ.get("HEADLESS", "true").lower() != "false"
# Real Google Chrome usually isn't in a container image; set PREFER_CHROME=false there
# to skip the chrome channel and use bundled Chromium directly (no startup warning).
PREFER_CHROME = os.environ.get("PREFER_CHROME", "true").lower() != "false"
MAX_CONCURRENCY = max(1, int(os.environ.get("MAX_CONCURRENCY", "10")))
NAV_TIMEOUT_MS = max(5000, int(os.environ.get("NAV_TIMEOUT_MS", "45000")))

# Where screenshots are written and how they're linked back.
_HERE = os.path.dirname(os.path.abspath(__file__))
SHOTS_DIR = os.environ.get("SHOTS_DIR") or os.path.join(_HERE, "shots")
# When behind a proxy/domain, set this so URLs are public (e.g. https://scrape.example.com).
# If unset, the URL is derived from the incoming request's scheme + host.
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")
# Auto-delete screenshots older than this many hours (0 = keep forever).
SHOTS_TTL_HOURS = float(os.environ.get("SHOTS_TTL_HOURS", "24"))

# --------------------------------------------------------------------------- #
# Facebook auto-login
# Pages like https://www.facebook.com/<page> bounce anonymous visitors to a login
# wall. Put FB_EMAIL / FB_PASSWORD in .env and we log in ONCE, cache the session
# cookies (to FB_STATE_FILE + memory), and reuse them on later requests — logging
# in every time would get the account checkpointed by Facebook.
# Note: this uses YOUR credentials and must comply with Facebook's terms; accounts
# with 2FA / login approvals can't be automated this way (the challenge will block).
# --------------------------------------------------------------------------- #
FB_EMAIL = os.environ.get("FB_EMAIL", "").strip()
FB_PASSWORD = os.environ.get("FB_PASSWORD", "")
FB_STATE_FILE = os.environ.get("FB_STATE_FILE") or os.path.join(_HERE, "fb_state.json")
FB_LOGIN_ENABLED = bool(FB_EMAIL and FB_PASSWORD)


def _as_bool(value, default: bool = True) -> bool:
    """Coerce query-string / JSON values to bool (query params arrive as strings)."""
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _prune_old_shots() -> None:
    """Best-effort delete of screenshots older than SHOTS_TTL_HOURS."""
    if SHOTS_TTL_HOURS <= 0:
        return
    cutoff = time.time() - SHOTS_TTL_HOURS * 3600
    try:
        for name in os.listdir(SHOTS_DIR):
            path = os.path.join(SHOTS_DIR, name)
            try:
                if os.path.isfile(path) and os.path.getmtime(path) < cutoff:
                    os.remove(path)
            except OSError:
                pass
    except FileNotFoundError:
        pass


async def _auto_scroll(page) -> None:
    """Scroll to the bottom in steps so lazy-loaded content renders."""
    await page.evaluate(
        """async () => {
            await new Promise((resolve) => {
                let total = 0;
                const step = 600;
                const timer = setInterval(() => {
                    window.scrollBy(0, step);
                    total += step;
                    if (total >= document.body.scrollHeight) {
                        clearInterval(timer);
                        window.scrollTo(0, 0);
                        resolve();
                    }
                }, 150);
            });
        }"""
    )
    try:
        await page.wait_for_load_state("networkidle", timeout=10000)
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# Facebook login helpers
# --------------------------------------------------------------------------- #
def _is_facebook_url(url: str) -> bool:
    u = (url or "").lower()
    return "facebook.com" in u or "fb.com" in u


def _looks_like_login(url: str) -> bool:
    """True when the current URL is Facebook's login / checkpoint wall."""
    u = (url or "").lower()
    return ("facebook.com/login" in u) or ("login/?next" in u) or ("/checkpoint" in u)


def _load_fb_cookies() -> list:
    try:
        with open(FB_STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f).get("cookies", [])
    except Exception:
        return []


def _save_fb_cookies(cookies: list) -> None:
    try:
        with open(FB_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump({"cookies": cookies}, f)
    except Exception:
        pass


async def _do_facebook_login(page) -> bool:
    """Fill in Facebook's login form. Returns True if a real session was created."""
    try:
        await page.goto("https://www.facebook.com/login/",
                        wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
    except Exception:
        pass

    # Dismiss the cookie-consent dialog if shown (varies by region).
    for sel in ('[data-cookiebanner="accept_button"]',
                'button[title="Allow all cookies"]',
                'button[title="Accept all"]',
                '[aria-label="Allow all cookies"]'):
        try:
            btn = page.locator(sel).first
            if await btn.count() and await btn.is_visible():
                await btn.click(timeout=3000)
                break
        except Exception:
            pass

    try:
        await page.fill('input[name="email"]', FB_EMAIL, timeout=15000)
        await page.fill('input[name="pass"]', FB_PASSWORD, timeout=15000)
        await page.click('button[name="login"]', timeout=15000)
    except Exception:
        return False

    try:
        await page.wait_for_load_state("networkidle", timeout=20000)
    except Exception:
        pass

    # Success = a session cookie (c_user) exists and we're off the login wall.
    try:
        cookies = await page.context.cookies("https://www.facebook.com")
    except Exception:
        cookies = []
    has_session = any(c.get("name") == "c_user" for c in cookies)
    return has_session and not _looks_like_login(page.url)


async def _ensure_facebook_login(app, context, page, target_url: str, seen_version: int) -> bool:
    """Get past the login wall: try freshly-refreshed cookies first, else log in.
    Serialized by a lock so concurrent Facebook requests don't all log in at once."""
    async with app["fb_lock"]:
        # Another request may have refreshed the session while we held the lock.
        if app["fb_cookies_version"] != seen_version and app.get("fb_cookies"):
            try:
                await context.add_cookies(app["fb_cookies"])
                await page.goto(target_url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
                if not _looks_like_login(page.url):
                    return True
            except Exception:
                pass

        if not await _do_facebook_login(page):
            return False

        try:
            state = await context.storage_state()
            app["fb_cookies"] = state.get("cookies", [])
            app["fb_cookies_version"] += 1
            _save_fb_cookies(app["fb_cookies"])
        except Exception:
            pass
        return True


async def _scrape(app: web.Application, base_url: str, url: str, full_page: bool,
                  scroll: bool, screenshot: bool, inline: bool) -> dict:
    """Open the URL in a fresh stealth context and return text + screenshot."""
    if "://" not in url:
        url = "https://" + url
    sb: StealthBrowser = app["sb"]
    sem: asyncio.Semaphore = app["sem"]

    is_fb = FB_LOGIN_ENABLED and _is_facebook_url(url)
    fb_version = app.get("fb_cookies_version", 0)

    async with sem:
        context, page = await sb.new_identity_page()
        try:
            # Reuse a cached Facebook session so we land on the real page, not login.
            if is_fb and app.get("fb_cookies"):
                try:
                    await context.add_cookies(app["fb_cookies"])
                except Exception:
                    pass

            resp = await page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)

            # Bounced to the login wall? Log in (once, cached) and reload the target.
            if is_fb and _looks_like_login(page.url):
                if await _ensure_facebook_login(app, context, page, url, fb_version):
                    resp = await page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)

            if scroll:
                await _auto_scroll(page)

            text = await page.evaluate("() => document.body ? document.body.innerText : ''")

            result = {
                "success": True,
                "url": url,
                "final_url": page.url,
                "status": resp.status if resp else None,
                "title": await page.title(),
                "text": text,
                "screenshot_url": None,
                "screenshot_file": None,
            }

            if screenshot:
                if inline:
                    png = await page.screenshot(full_page=full_page)
                    result["screenshot_base64"] = base64.b64encode(png).decode("ascii")
                    result["screenshot_format"] = "png"
                else:
                    filename = f"{uuid.uuid4().hex}.png"
                    path = os.path.join(SHOTS_DIR, filename)
                    await page.screenshot(path=path, full_page=full_page)
                    result["screenshot_file"] = filename
                    result["screenshot_url"] = f"{base_url}/shots/{filename}"

            return result
        finally:
            await context.close()


# --------------------------------------------------------------------------- #
# Handlers
# --------------------------------------------------------------------------- #
def _base_url(request: web.Request) -> str:
    return PUBLIC_BASE_URL or f"{request.scheme}://{request.host}"


async def index(request: web.Request) -> web.Response:
    """Root landing route so the domain root doesn't 404 — shows usage."""
    base = _base_url(request)
    return web.json_response({
        "service": "stealth-scrape-api",
        "status": "ok",
        "endpoints": {
            "health": f"{base}/health",
            "scrape_get": f"{base}/scrape?url=https://example.com",
            "scrape_post": f"{base}/scrape  (JSON body: {{\"url\": \"https://example.com\"}})",
            "shots": f"{base}/shots/<file>.png",
        },
    })


async def health(request: web.Request) -> web.Response:
    sb: Optional[StealthBrowser] = request.app.get("sb")
    return web.json_response(
        {"status": "ok", "browser": (sb.channel or "chromium") if sb else "starting"}
    )


async def scrape(request: web.Request) -> web.Response:
    if request.method == "POST":
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"success": False, "error": "invalid JSON body"}, status=400)
    else:
        data = dict(request.query)

    url = (data.get("url") or "").strip()
    if not url:
        return web.json_response({"success": False, "error": "missing 'url'"}, status=400)

    if request.app.get("sb") is None:
        return web.json_response(
            {"success": False, "error": "browser still starting, retry in a moment"},
            status=503,
        )

    full_page = _as_bool(data.get("full_page", True))
    scroll = _as_bool(data.get("scroll", True))
    screenshot = _as_bool(data.get("screenshot", True))
    inline = _as_bool(data.get("inline", False), default=False)

    try:
        result = await _scrape(
            request.app, _base_url(request), url, full_page, scroll, screenshot, inline
        )
        return web.json_response(result)
    except Exception as e:
        return web.json_response(
            {"success": False, "url": url, "error": str(e)}, status=502
        )


# --------------------------------------------------------------------------- #
# Lifecycle
# --------------------------------------------------------------------------- #
async def _on_startup(app: web.Application) -> None:
    os.makedirs(SHOTS_DIR, exist_ok=True)
    _prune_old_shots()
    app["sb"] = None
    app["sem"] = asyncio.Semaphore(MAX_CONCURRENCY)
    app["fb_lock"] = asyncio.Lock()
    app["fb_cookies"] = _load_fb_cookies()
    app["fb_cookies_version"] = 0
    if FB_LOGIN_ENABLED:
        print(f"[server] facebook auto-login enabled for {FB_EMAIL} "
              f"(cached cookies: {len(app['fb_cookies'])})", flush=True)

    # Launch the browser in the background so the HTTP server binds the port
    # immediately. aiohttp runs on_startup BEFORE it starts listening, so doing a
    # slow/failing Chromium launch here would block the port from ever opening
    # (the container looks "up" but nothing answers -> proxy 404). This way
    # /health responds right away and any launch error is logged loudly.
    async def _launch_browser() -> None:
        try:
            sb = StealthBrowser(headless=HEADLESS, prefer_chrome=PREFER_CHROME)
            await sb.launch()
            app["sb"] = sb
            print(f"[server] browser ready ({sb.channel or 'bundled chromium'}), "
                  f"concurrency={MAX_CONCURRENCY}, shots -> {SHOTS_DIR}", flush=True)
        except Exception as e:
            print(f"[server] BROWSER LAUNCH FAILED: {e!r}", flush=True)

    app["browser_task"] = asyncio.create_task(_launch_browser())
    print(f"[server] HTTP server listening on {HOST}:{PORT}", flush=True)


async def _on_cleanup(app: web.Application) -> None:
    task: Optional[asyncio.Task] = app.get("browser_task")
    if task and not task.done():
        task.cancel()
    sb: Optional[StealthBrowser] = app.get("sb")
    if sb:
        await sb.close()
    print("[server] browser closed", flush=True)


def make_app() -> web.Application:
    os.makedirs(SHOTS_DIR, exist_ok=True)
    app = web.Application()
    app.on_startup.append(_on_startup)
    app.on_cleanup.append(_on_cleanup)
    app.add_routes([
        web.get("/", index),
        web.get("/health", health),
        web.get("/scrape", scrape),
        web.post("/scrape", scrape),
        web.static("/shots", SHOTS_DIR),
    ])
    return app


if __name__ == "__main__":
    web.run_app(make_app(), host=HOST, port=PORT)
