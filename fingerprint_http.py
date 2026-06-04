"""
TLS & HTTP/2 fingerprint matching for the *raw-HTTP* path.

This is the one place where TLS/HTTP-2 spoofing actually happens — and it only matters
*outside* the browser. Here's why:

  * When you drive Playwright (browser.py), requests go out through real Chrome's network
    stack, so the JA3/JA4 TLS handshake and HTTP/2 SETTINGS/frame order are already
    Chrome's. Nothing to spoof.
  * When you instead make a *direct* HTTP call from Python, libraries like `requests`,
    `httpx` or `urllib` hand the server Python/OpenSSL's TLS ClientHello and a non-Chrome
    HTTP/2 profile. Anti-bot services (Cloudflare, Akamai, DataDome) fingerprint exactly
    that mismatch — "claims to be Chrome in the UA, but its TLS says Python."

`curl_cffi` is curl compiled against BoringSSL with Chrome's TLS extension order and
HTTP/2 settings, so `impersonate="chrome"` reproduces a genuine Chrome JA3/JA4 + HTTP/2
fingerprint. Use this for fast HTML fetches that don't need JS rendering, while keeping
the headers consistent with the same VirtualIdentity used by the browser.
"""
from __future__ import annotations

from typing import Optional, Dict, Any

from identity import VirtualIdentity, generate_identity

try:
    from curl_cffi import requests as cffi_requests
except ImportError as e:  # pragma: no cover
    raise ImportError(
        "curl_cffi is required for TLS/HTTP-2 fingerprint matching. "
        "Install it with: pip install curl_cffi"
    ) from e


# Map our Chrome major version to a curl_cffi impersonation target. curl_cffi ships
# specific browser builds; pick the closest available and let "chrome" track latest.
_IMPERSONATE = "chrome"


def _headers_from_identity(identity: VirtualIdentity) -> Dict[str, str]:
    """Build a Chrome-like header set consistent with the identity."""
    return {
        "User-Agent": identity.user_agent,
        "Accept": (
            "text/html,application/xhtml+xml,application/xml;q=0.9,"
            "image/avif,image/webp,image/apng,*/*;q=0.8,"
            "application/signed-exchange;v=b3;q=0.7"
        ),
        "Accept-Language": identity.accept_language,
        "Accept-Encoding": "gzip, deflate, br, zstd",
        "sec-ch-ua": identity.sec_ch_ua,
        "sec-ch-ua-mobile": "?1" if identity.ua_mobile else "?0",
        "sec-ch-ua-platform": f'"{identity.ua_platform}"',
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "Upgrade-Insecure-Requests": "1",
    }


def fetch(
    url: str,
    identity: Optional[VirtualIdentity] = None,
    proxy: Optional[str] = None,
    timeout: float = 30.0,
    **kwargs: Any,
):
    """
    GET `url` with a Chrome-matched TLS/HTTP-2 fingerprint and identity-consistent headers.

    Args:
        url:      Target URL.
        identity: VirtualIdentity for headers (a fresh one is generated if omitted).
        proxy:    Optional proxy URL, e.g. "http://user:pass@host:port".
        timeout:  Seconds.
        **kwargs: Forwarded to curl_cffi (e.g. allow_redirects=False).

    Returns:
        A curl_cffi Response (`.status_code`, `.text`, `.content`, `.headers`).
    """
    if identity is None:
        identity = generate_identity()

    proxies = {"http": proxy, "https": proxy} if proxy else None

    return cffi_requests.get(
        url,
        headers=_headers_from_identity(identity),
        impersonate=_IMPERSONATE,
        proxies=proxies,
        timeout=timeout,
        **kwargs,
    )


if __name__ == "__main__":
    # Quick self-check: hit a TLS-fingerprint echo service and print what the server saw.
    ident = generate_identity()
    resp = fetch("https://tls.peet.ws/api/all", ident)
    print("status:", resp.status_code)
    print("UA sent:", ident.user_agent)
    print(resp.text[:1200])
