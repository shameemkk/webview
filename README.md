# webview — Stealth Scrape API

Give it a URL, get back the page's **rendered text** and a **screenshot**. It drives a
hardened Chrome/Chromium with a unique per-request virtual identity, canvas/WebGL noise,
and hidden automation tells, so pages load as they would for a real visitor.

## Layout

| File | What it does |
|------|--------------|
| `server.py` | aiohttp app — the `/scrape` endpoint (url → text + screenshot). |
| `browser.py` | `StealthBrowser` — launches real Chrome (falls back to Chromium), one identity per context. |
| `identity.py` | `VirtualIdentity` + `generate_identity()` — one internally-consistent fingerprint profile. |
| `stealth.py` | `build_stealth_script()` — JS injected before page scripts (webdriver/navigator/screen + canvas/WebGL noise). |
| `fingerprint_http.py` | Optional `curl_cffi` fetch with a Chrome-matched TLS/HTTP-2 fingerprint (raw HTTP, no browser). |

## Install & run

```bash
cd webview
pip install -r requirements.txt
playwright install chromium        # or: playwright install chrome
python server.py                   # http://0.0.0.0:8080
```

Config via env (optional `.env`): `HOST`, `PORT`, `HEADLESS` (default true),
`MAX_CONCURRENCY` (default 5), `NAV_TIMEOUT_MS` (default 45000).

## Docker

```bash
cd webview
docker build -t stealth-scrape .
docker run --rm -p 8080:8080 stealth-scrape
# then: curl "http://localhost:8080/scrape?url=https://example.com"
```

Built on `mcr.microsoft.com/playwright/python` (Chromium + OS deps preinstalled). The image
sets `NO_SANDBOX=true` (Chromium runs as root in containers) and `PREFER_CHROME=false`
(no Google Chrome in the image — uses bundled Chromium). To persist screenshots across
restarts, mount a volume and point `SHOTS_DIR` at it:

```bash
docker run --rm -p 8080:8080 \
  -e PUBLIC_BASE_URL=https://scrape.yourdomain.com \
  -e MAX_CONCURRENCY=8 \
  -v "$PWD/shots:/app/shots" \
  stealth-scrape
```

If you see Chromium OOM/crashes under load, give the container more shared memory:
`docker run --shm-size=1g ...`.

## API

### `POST /scrape`
```json
{ "url": "https://example.com", "full_page": true, "scroll": true, "screenshot": true }
```

### `GET /scrape?url=https://example.com`

### Response
```json
{
  "success": true,
  "url": "https://example.com",
  "final_url": "https://example.com/",
  "status": 200,
  "title": "Example Domain",
  "text": "<rendered visible text of the page>",
  "screenshot_url": "http://localhost:8080/shots/2f9c....png",
  "screenshot_file": "2f9c....png"
}
```
The screenshot is saved to `./shots` and served back as `screenshot_url` (open it in a
browser or download it). Both screenshot fields are `null` when `screenshot=false`.
Pass `"inline": true` to get `screenshot_base64` in the response instead of a saved file.
`text` is `document.body.innerText` captured **after** the auto-scroll, so lazy-loaded
content is included. On failure you get HTTP 502 with
`{ "success": false, "url": ..., "error": ... }`.

Relevant env vars: `SHOTS_DIR` (where PNGs are written), `PUBLIC_BASE_URL` (set this to
your public domain when behind a proxy so URLs are reachable), `SHOTS_TTL_HOURS` (auto-delete
old screenshots, default 24; `0` = keep forever).

### `GET /health`
`{ "status": "ok", "browser": "chrome" }`

## Examples

```bash
# text + screenshot URL
curl -s "http://localhost:8080/scrape?url=https://example.com"
# -> {"...","screenshot_url":"http://localhost:8080/shots/<id>.png", ...}
# then just open that URL, or download it:
curl -s -O http://localhost:8080/shots/<id>.png

# text only, no screenshot
curl -s -X POST http://localhost:8080/scrape \
  -H 'content-type: application/json' \
  -d '{"url":"https://example.com","screenshot":false}'

# base64 inline instead of a saved file (old behaviour)
curl -s "http://localhost:8080/scrape?url=https://example.com&inline=true"
```

## How the stealth features map

- **Unique virtual identity** — `generate_identity()` derives every value (UA, platform,
  screen, WebGL, client hints) from one OS+GPU profile so nothing contradicts.
- **Canvas & WebGL noise** — seeded per-session ±1 LSB perturbation on pixel readback;
  stable within a session, different across sessions (the technique Brave/Tor use).
- **TLS & HTTP/2** — the browser path *is* Chrome, so its JA3/JA4 + HTTP/2 fingerprint is
  already genuine. For raw (non-browser) requests, `fingerprint_http.py` uses `curl_cffi`
  (`impersonate="chrome"`) to match it.

## Scope / responsible use

For scraping **publicly published content** and testing your **own** sites — not for
defeating authentication, CAPTCHAs, or harvesting personal data from walled-garden
platforms. Honor each site's rate limits and terms.
