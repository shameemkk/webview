"""
Stealth init-script builder.

`build_stealth_script(identity)` returns a single JavaScript string that is injected
with `context.add_init_script(...)`. Playwright runs init scripts *before any page
script*, in every frame, so by the time a detector reads `navigator.webdriver` or
hashes a canvas, our patches are already in place.

Two jobs:
  1. Remove automation tells and align every JS-readable signal with the identity
     (so the in-page fingerprint agrees with the UA / client-hint headers the browser
     sends at the network layer).
  2. Add tiny, deterministic per-session noise to Canvas and WebGL readback so the
     fingerprint *hash* differs from session to session (this is the same defensive
     technique Brave/Tor use), while staying visually identical and stable within a run.

The Python side only serializes the identity to JSON; all logic lives in JS.
"""
from __future__ import annotations

import json

from identity import VirtualIdentity


# Realistic Chrome plugin/mimeType set (Chrome exposes these five PDF entries).
_PLUGINS_JS = """
[
  {name:"PDF Viewer", filename:"internal-pdf-viewer", desc:"Portable Document Format"},
  {name:"Chrome PDF Viewer", filename:"internal-pdf-viewer", desc:"Portable Document Format"},
  {name:"Chromium PDF Viewer", filename:"internal-pdf-viewer", desc:"Portable Document Format"},
  {name:"Microsoft Edge PDF Viewer", filename:"internal-pdf-viewer", desc:"Portable Document Format"},
  {name:"WebKit built-in PDF", filename:"internal-pdf-viewer", desc:"Portable Document Format"}
]
"""


def build_stealth_script(identity: VirtualIdentity) -> str:
    """Return the JS init script tailored to `identity`."""

    config = {
        "platform": identity.platform,
        "vendor": identity.vendor,
        "languages": identity.languages,
        "hardwareConcurrency": identity.hardware_concurrency,
        "deviceMemory": identity.device_memory,
        "uaBrands": identity.ua_brands,
        "uaPlatform": identity.ua_platform,
        "uaMobile": identity.ua_mobile,
        "uaFullVersion": identity.ua_full_version,
        "screen": identity.screen,
        "webglVendor": identity.webgl_vendor,
        "webglRenderer": identity.webgl_renderer,
        "noiseSeed": identity.noise_seed,
    }

    return (
        "(() => {\n"
        f"const CFG = {json.dumps(config)};\n"
        f"const PLUGIN_DATA = {_PLUGINS_JS};\n"
        + _SCRIPT_BODY
        + "\n})();"
    )


# Everything below is plain JS executed in the page context. Kept as one constant so
# the only interpolation point is the trusted CFG/PLUGIN_DATA JSON above.
_SCRIPT_BODY = r"""
// ---- helper: define a property without leaving an enumerable footprint ----
const define = (obj, prop, getter) => {
  try {
    Object.defineProperty(obj, prop, { get: getter, configurable: true, enumerable: true });
  } catch (e) {}
};

// ---- helper: make an overridden function report as native in toString() ----
const NATIVE = Function.prototype.toString;
const nativeMap = new WeakMap();
const asNative = (fn, name) => {
  nativeMap.set(fn, `function ${name}() { [native code] }`);
  return fn;
};
Function.prototype.toString = new Proxy(NATIVE, {
  apply(target, thisArg, args) {
    if (nativeMap.has(thisArg)) return nativeMap.get(thisArg);
    return Reflect.apply(target, thisArg, args);
  },
});

// ---- helper: deterministic per-session, per-index noise in {-1, 0, 1} ----
// Same (seed, index) -> same offset, so a canvas hashed twice in one session is
// stable (realistic), but a new session's seed yields a different hash.
const SEED = CFG.noiseSeed >>> 0;
const offsetAt = (i) => {
  let h = (SEED ^ (i * 2654435761)) >>> 0;
  h = Math.imul(h ^ (h >>> 15), 2246822507) >>> 0;
  h = Math.imul(h ^ (h >>> 13), 3266489909) >>> 0;
  h ^= h >>> 16;
  return (h % 3) - 1; // -1, 0, or 1
};

// =====================================================================
// 1. Automation tells
// =====================================================================
define(Navigator.prototype, "webdriver", () => undefined);

// Strip Selenium/CDP breadcrumbs if any tooling injected them.
for (const k of Object.keys(window)) {
  if (/^(cdc_|\$cdc_|__webdriver|__selenium|__driver)/.test(k)) {
    try { delete window[k]; } catch (e) {}
  }
}

// =====================================================================
// 2. navigator.* aligned to the identity
// =====================================================================
define(Navigator.prototype, "platform", () => CFG.platform);
define(Navigator.prototype, "vendor", () => CFG.vendor);
define(Navigator.prototype, "languages", () => Object.freeze([...CFG.languages]));
define(Navigator.prototype, "hardwareConcurrency", () => CFG.hardwareConcurrency);
define(Navigator.prototype, "deviceMemory", () => CFG.deviceMemory);

// navigator.userAgentData (client hints surfaced to JS) — must match sec-ch-ua headers.
try {
  const brands = CFG.uaBrands.map(b => ({ brand: b.brand, version: b.version }));
  const uaData = {
    brands,
    mobile: CFG.uaMobile,
    platform: CFG.uaPlatform,
    getHighEntropyValues: asNative(function (hints) {
      const full = {
        architecture: "x86",
        bitness: "64",
        brands,
        fullVersionList: CFG.uaBrands.map(b => ({
          brand: b.brand,
          version: b.brand.indexOf("Not") === 0 ? "99.0.0.0" : CFG.uaFullVersion,
        })),
        mobile: CFG.uaMobile,
        model: "",
        platform: CFG.uaPlatform,
        platformVersion: CFG.uaPlatform === "Windows" ? "15.0.0" : "14.5.0",
        uaFullVersion: CFG.uaFullVersion,
        wow64: false,
      };
      const out = {};
      (hints || []).forEach(h => { if (h in full) out[h] = full[h]; });
      return Promise.resolve(out);
    }, "getHighEntropyValues"),
    toJSON: function () { return { brands, mobile: CFG.uaMobile, platform: CFG.uaPlatform }; },
  };
  define(Navigator.prototype, "userAgentData", () => uaData);
} catch (e) {}

// =====================================================================
// 3. window.chrome runtime stub (headless Chromium omits this)
// =====================================================================
if (!window.chrome) {
  window.chrome = {};
}
if (!window.chrome.runtime) {
  window.chrome.runtime = {
    connect: asNative(function () { return { onMessage: {}, postMessage() {}, disconnect() {} }; }, "connect"),
    sendMessage: asNative(function () {}, "sendMessage"),
    id: undefined,
  };
}
window.chrome.app = window.chrome.app || { isInstalled: false, InstallState: {}, RunningState: {} };
window.chrome.csi = window.chrome.csi || asNative(function () { return {}; }, "csi");
window.chrome.loadTimes = window.chrome.loadTimes || asNative(function () { return {}; }, "loadTimes");

// =====================================================================
// 4. permissions.query — headless reports "denied" for notifications, real Chrome "prompt"
// =====================================================================
try {
  const origQuery = window.navigator.permissions.query.bind(window.navigator.permissions);
  window.navigator.permissions.query = asNative(function (params) {
    if (params && params.name === "notifications") {
      return Promise.resolve({ state: Notification.permission === "denied" ? "denied" : "prompt" });
    }
    return origQuery(params);
  }, "query");
} catch (e) {}

// =====================================================================
// 5. plugins / mimeTypes — present a plausible, non-empty list
// =====================================================================
try {
  const mimeTypes = [];
  const plugins = PLUGIN_DATA.map((p) => {
    const plugin = Object.create(Plugin.prototype);
    define(plugin, "name", () => p.name);
    define(plugin, "filename", () => p.filename);
    define(plugin, "description", () => p.desc);
    define(plugin, "length", () => 1);
    return plugin;
  });
  const pdfMime = Object.create(MimeType.prototype);
  define(pdfMime, "type", () => "application/pdf");
  define(pdfMime, "suffixes", () => "pdf");
  define(pdfMime, "description", () => "Portable Document Format");
  mimeTypes.push(pdfMime);

  const pluginArray = Object.create(PluginArray.prototype);
  plugins.forEach((p, i) => { pluginArray[i] = p; });
  define(pluginArray, "length", () => plugins.length);
  pluginArray.item = asNative(function (i) { return plugins[i]; }, "item");
  pluginArray.namedItem = asNative(function (n) { return plugins.find(p => p.name === n) || null; }, "namedItem");

  define(Navigator.prototype, "plugins", () => pluginArray);
} catch (e) {}

// =====================================================================
// 6. screen.* aligned to the identity
// =====================================================================
try {
  define(window.screen, "width", () => CFG.screen.width);
  define(window.screen, "height", () => CFG.screen.height);
  define(window.screen, "availWidth", () => CFG.screen.avail_width);
  define(window.screen, "availHeight", () => CFG.screen.avail_height);
  define(window.screen, "colorDepth", () => CFG.screen.color_depth);
  define(window.screen, "pixelDepth", () => CFG.screen.pixel_depth);
} catch (e) {}

// =====================================================================
// 7. WebGL — unmasked vendor/renderer identity + subtle readback noise
// =====================================================================
const UNMASKED_VENDOR = 37445;
const UNMASKED_RENDERER = 37446;
const patchWebGL = (proto) => {
  if (!proto) return;
  const origGetParameter = proto.getParameter;
  proto.getParameter = asNative(function (param) {
    if (param === UNMASKED_VENDOR) return CFG.webglVendor;
    if (param === UNMASKED_RENDERER) return CFG.webglRenderer;
    return origGetParameter.call(this, param);
  }, "getParameter");

  const origReadPixels = proto.readPixels;
  if (origReadPixels) {
    proto.readPixels = asNative(function (x, y, w, h, fmt, type, pixels) {
      const r = origReadPixels.call(this, x, y, w, h, fmt, type, pixels);
      if (pixels && pixels.length) {
        for (let i = 0; i < pixels.length; i += 257) { // sparse, cheap
          const v = pixels[i] + offsetAt(i);
          pixels[i] = v < 0 ? 0 : v > 255 ? 255 : v;
        }
      }
      return r;
    }, "readPixels");
  }
};
if (window.WebGLRenderingContext) patchWebGL(WebGLRenderingContext.prototype);
if (window.WebGL2RenderingContext) patchWebGL(WebGL2RenderingContext.prototype);

// =====================================================================
// 8. Canvas — deterministic per-session noise on pixel readback
// =====================================================================
try {
  const origGetImageData = CanvasRenderingContext2D.prototype.getImageData;
  const perturb = (data) => {
    for (let i = 0; i < data.length; i += 4) { // one tweak per pixel, RGB only
      const o = offsetAt(i);
      if (o !== 0) {
        data[i]     = Math.min(255, Math.max(0, data[i]     + o));
        data[i + 1] = Math.min(255, Math.max(0, data[i + 1] + o));
        data[i + 2] = Math.min(255, Math.max(0, data[i + 2] + o));
      }
    }
    return data;
  };

  CanvasRenderingContext2D.prototype.getImageData = asNative(function (x, y, w, h) {
    const imageData = origGetImageData.call(this, x, y, w, h);
    perturb(imageData.data);
    return imageData;
  }, "getImageData");

  // toDataURL / toBlob render the canvas — bake the same noise in before export so
  // hash-based fingerprinting sees the perturbed bytes too.
  const applyNoiseToCanvas = (canvas) => {
    try {
      const ctx = canvas.getContext("2d");
      if (!ctx) return;
      const d = origGetImageData.call(ctx, 0, 0, canvas.width, canvas.height);
      perturb(d.data);
      ctx.putImageData(d, 0, 0);
    } catch (e) {}
  };

  const origToDataURL = HTMLCanvasElement.prototype.toDataURL;
  HTMLCanvasElement.prototype.toDataURL = asNative(function (...args) {
    applyNoiseToCanvas(this);
    return origToDataURL.apply(this, args);
  }, "toDataURL");

  const origToBlob = HTMLCanvasElement.prototype.toBlob;
  HTMLCanvasElement.prototype.toBlob = asNative(function (...args) {
    applyNoiseToCanvas(this);
    return origToBlob.apply(this, args);
  }, "toBlob");
} catch (e) {}
"""
