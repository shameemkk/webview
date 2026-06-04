"""
Unique Virtual Identity generator.

A fingerprint is only convincing if every value agrees with every other value.
Detectors do not flag "unusual" hardware — they flag *contradictions* (a User-Agent
that says Windows while navigator.platform says Linux, or a WebGL renderer that says
Apple on a "Windows" machine).

So we never randomize fields independently. We pick ONE coherent base profile
(OS + GPU family), then derive every dependent value from it. Randomness only ever
chooses *between equally-consistent* options (which screen size, how many CPU cores).

`generate_identity(seed=...)` is deterministic for a given seed, which makes a session
reproducible and lets the canvas/WebGL noise (see stealth.py) stay stable within a run.
"""
from __future__ import annotations

import random
import uuid
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Tuple

# Chrome version is defined in ONE place so the UA string, the sec-ch-ua header,
# and navigator.userAgentData brands can never drift apart.
CHROME_MAJOR = 131
CHROME_FULL = "131.0.0.0"

# Realistic desktop screen resolutions (width, height).
_SCREENS: List[Tuple[int, int]] = [
    (1920, 1080),
    (1536, 864),
    (1366, 768),
    (2560, 1440),
    (1440, 900),
    (1680, 1050),
]

# Plausible CPU / RAM combinations.
_HARDWARE_CONCURRENCY = [4, 8, 12, 16]
_DEVICE_MEMORY = [8, 16]

# Language sets keyed to a locale.
_LOCALES = [
    ("en-US", ["en-US", "en"], "America/New_York"),
    ("en-US", ["en-US", "en"], "America/Chicago"),
    ("en-GB", ["en-GB", "en"], "Europe/London"),
    ("en-US", ["en-US", "en"], "America/Los_Angeles"),
]


# Each base profile is an internally-consistent (OS, GPU) pairing. Everything
# below the profile is filled in to agree with these anchors.
_PROFILES: List[Dict[str, Any]] = [
    {
        "os": "windows",
        "ua_os": "Windows NT 10.0; Win64; x64",
        "platform": "Win32",
        "ua_platform": "Windows",
        "vendor": "Google Inc.",
        # ANGLE wrapper + a real consumer GPU, rendered through Direct3D11 (Windows-only).
        "webgl": [
            ("Google Inc. (NVIDIA)",
             "ANGLE (NVIDIA, NVIDIA GeForce RTX 3060 Direct3D11 vs_5_0 ps_5_0, D3D11)"),
            ("Google Inc. (Intel)",
             "ANGLE (Intel, Intel(R) UHD Graphics 630 Direct3D11 vs_5_0 ps_5_0, D3D11)"),
            ("Google Inc. (AMD)",
             "ANGLE (AMD, AMD Radeon RX 6600 Direct3D11 vs_5_0 ps_5_0, D3D11)"),
        ],
    },
    {
        "os": "macos",
        "ua_os": "Macintosh; Intel Mac OS X 10_15_7",
        "platform": "MacIntel",
        "ua_platform": "macOS",
        "vendor": "Google Inc.",
        # Apple Silicon rendered through Metal/OpenGL — never Direct3D.
        "webgl": [
            ("Google Inc. (Apple)", "ANGLE (Apple, Apple M1, OpenGL 4.1)"),
            ("Google Inc. (Apple)", "ANGLE (Apple, Apple M2, OpenGL 4.1)"),
            ("Google Inc. (Apple)", "ANGLE (Apple, Apple M1 Pro, OpenGL 4.1)"),
        ],
    },
]


@dataclass
class VirtualIdentity:
    """A self-consistent browser fingerprint for one session."""

    seed: int

    # UA / client hints
    user_agent: str
    ua_full_version: str
    ua_brands: List[Dict[str, str]]   # for sec-ch-ua + navigator.userAgentData
    ua_platform: str                  # sec-ch-ua-platform value ("Windows"/"macOS")
    ua_mobile: bool

    # navigator.*
    platform: str
    vendor: str
    languages: List[str]
    hardware_concurrency: int
    device_memory: int

    # locale / time
    locale: str
    timezone_id: str

    # viewport / screen
    viewport: Tuple[int, int]
    screen: Dict[str, int]
    device_scale_factor: float

    # WebGL
    webgl_vendor: str
    webgl_renderer: str

    # Per-session noise seed (drives canvas/WebGL LSB perturbation in stealth.py).
    noise_seed: int = field(default=0)

    @property
    def accept_language(self) -> str:
        """Build a weighted Accept-Language header from `languages`."""
        parts = []
        q = 1.0
        for i, lang in enumerate(self.languages):
            if i == 0:
                parts.append(lang)
            else:
                q = round(q - 0.1, 1)
                parts.append(f"{lang};q={q}")
        return ",".join(parts)

    @property
    def sec_ch_ua(self) -> str:
        """Build the sec-ch-ua header value from `ua_brands`."""
        return ", ".join(f'"{b["brand"]}";v="{b["version"]}"' for b in self.ua_brands)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _build_brands(rng: random.Random) -> List[Dict[str, str]]:
    """
    Chromium reports three brands: a real one (Chromium), the branded one (Google
    Chrome), and a deliberately-randomized "Not...A;Brand" GREASE entry. The GREASE
    text varies between Chrome builds, so we vary it too.
    """
    grease_variants = [
        "Not_A Brand", "Not(A:Brand", "Not.A/Brand", "Not?A_Brand", "Not A;Brand",
    ]
    return [
        {"brand": "Google Chrome", "version": str(CHROME_MAJOR)},
        {"brand": "Chromium", "version": str(CHROME_MAJOR)},
        {"brand": rng.choice(grease_variants), "version": "99"},
    ]


def generate_identity(seed: int | None = None) -> VirtualIdentity:
    """
    Produce one internally-consistent VirtualIdentity.

    Pass a fixed `seed` to reproduce an identity; omit it for a fresh random one.
    """
    if seed is None:
        seed = uuid.uuid4().int & 0x7FFFFFFF
    rng = random.Random(seed)

    profile = rng.choice(_PROFILES)
    webgl_vendor, webgl_renderer = rng.choice(profile["webgl"])

    locale, languages, timezone_id = rng.choice(_LOCALES)

    sw, sh = rng.choice(_SCREENS)
    # The viewport (inner window) is always smaller than the screen — browser chrome,
    # OS taskbar/dock and scrollbars eat real estate. Keep it plausible.
    vw = sw - rng.choice([0, 16, 32])
    vh = sh - rng.choice([74, 88, 120, 139])
    # macOS menu bar + dock vs Windows taskbar give slightly different avail heights.
    avail_h = sh - (25 if profile["os"] == "macos" else 40)

    user_agent = (
        f"Mozilla/5.0 ({profile['ua_os']}) AppleWebKit/537.36 (KHTML, like Gecko) "
        f"Chrome/{CHROME_FULL} Safari/537.36"
    )

    return VirtualIdentity(
        seed=seed,
        user_agent=user_agent,
        ua_full_version=CHROME_FULL,
        ua_brands=_build_brands(rng),
        ua_platform=profile["ua_platform"],
        ua_mobile=False,
        platform=profile["platform"],
        vendor=profile["vendor"],
        languages=languages,
        hardware_concurrency=rng.choice(_HARDWARE_CONCURRENCY),
        device_memory=rng.choice(_DEVICE_MEMORY),
        locale=locale,
        timezone_id=timezone_id,
        viewport=(vw, vh),
        screen={
            "width": sw,
            "height": sh,
            "avail_width": sw,
            "avail_height": avail_h,
            "color_depth": 24,
            "pixel_depth": 24,
        },
        device_scale_factor=rng.choice([1.0, 1.0, 1.25, 1.5, 2.0]),
        webgl_vendor=webgl_vendor,
        webgl_renderer=webgl_renderer,
        # Derive a stable-but-distinct noise seed from the identity seed.
        noise_seed=rng.getrandbits(32),
    )


if __name__ == "__main__":
    import json

    ident = generate_identity()
    print(json.dumps(ident.to_dict(), indent=2, default=str))
    print("\naccept-language:", ident.accept_language)
    print("sec-ch-ua:      ", ident.sec_ch_ua)
