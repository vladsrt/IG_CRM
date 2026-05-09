"""
Proxy Builder Utility (Manifest V3, blocking auth)
--------------------------------------------------
Generates an unpacked Chrome extension on the fly so DrissionPage can
drive a Chromium that talks to authenticated HTTP / HTTPS / SOCKS
proxies without crashing on the OS-level basic-auth dialog.

Key design points
~~~~~~~~~~~~~~~~~
* **Manifest V3** — current Chrome stable rejects MV2 extensions.
* **Blocking auth listener** — In MV3 the general
  ``webRequestBlocking`` permission is restricted, but
  ``onAuthRequired`` retains support for ``"blocking"`` when the
  extension declares the ``webRequestAuthProvider`` permission. The
  listener returns ``{authCredentials: ...}`` synchronously, which
  is the simplest and most reliable shape for proxy auth.
* **No extension when no proxy** — :func:`create_proxy_extension`
  returns ``None`` when given a falsy ``proxy_string``. Callers
  branch on the return value; nothing gets written to disk for the
  no-proxy path, so DrissionPage can never resurrect a stale
  extension.
* **Temporary folder by default** — :func:`create_proxy_extension`
  writes to ``tempfile.mkdtemp(prefix="dp_proxy_ext_")`` unless the
  caller supplies an explicit folder. Cleanup is the caller's
  responsibility (see ``InstagramBrowser.close``).
* **Bypass list** — ``localhost`` and ``127.0.0.1`` are always
  excluded so health checks and devtools introspection stay direct.

Caveats — SOCKS authentication
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Chrome's ``chrome.proxy`` API accepts ``socks4`` / ``socks5`` schemes,
but Chrome does NOT fire ``onAuthRequired`` for SOCKS — that callback
only runs for HTTP/HTTPS. A SOCKS proxy with credentials therefore
yields ``ERR_SOCKS_CONNECTION_FAILED`` at connect time. The default
``socks_auth_policy="downgrade"`` rewrites the scheme to ``http`` and
emits a heavy warning; most commercial proxy providers expose both
transports on the same port so this Just Works. Override with
``socks_auth_policy="strict"`` (raise) or ``"keep"`` (preserve, will
fail) if you need different behaviour.

Public API
~~~~~~~~~~
``parse_proxy_url(url) -> (scheme, user, pass, host, port)``
``create_proxy_extension(proxy_string, extension_folder_name=None) -> Optional[str]``
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from typing import Literal, Optional, Tuple

logger = logging.getLogger(__name__)


# ── Constants ───────────────────────────────────────────────────────────
# Schemes Chrome's `chrome.proxy.settings.set` accepts.
_VALID_SCHEMES: frozenset[str] = frozenset({"http", "https", "socks4", "socks5"})

# Schemes that DO support per-request proxy-auth via Chrome's
# webRequestAuthProvider permission. SOCKS does not — `onAuthRequired`
# only fires for HTTP/HTTPS proxies, so a SOCKS proxy that demands
# user/pass produces ``ERR_SOCKS_CONNECTION_FAILED`` at connect time.
_AUTH_CAPABLE_SCHEMES: frozenset[str] = frozenset({"http", "https"})

# Hosts that should ALWAYS bypass the proxy. Localhost and the IPv4
# loopback are non-negotiable — health probes, devtools, CDP all use
# them and must not be tunnelled.
_DEFAULT_BYPASS_LIST: list[str] = ["localhost", "127.0.0.1", "::1"]

# Folder prefix when the caller doesn't supply an explicit folder name.
# We use tempfile.mkdtemp() by default so the OS picks an unambiguous
# location (e.g. /tmp on Linux) and each instance gets its own dir.
_TEMP_FOLDER_PREFIX: str = "dp_proxy_ext_"

SocksAuthPolicy = Literal["downgrade", "strict", "keep"]


# ── URL parsing ─────────────────────────────────────────────────────────
def parse_proxy_url(proxy_string: str) -> Tuple[str, str, str, str, str]:
    """Parse a proxy URL into ``(scheme, user, password, host, port)``.

    Accepts:
        * ``protocol://user:pass@host:port``  (preferred)
        * ``user:pass@host:port``             (assumed ``http``)
        * ``host:port``                       (assumed ``http``, no auth)

    Raises:
        ValueError: on empty input, malformed input, or an unknown scheme.
    """
    raw = (proxy_string or "").strip()
    if not raw:
        raise ValueError("proxy_string is empty")

    # 1. Scheme (optional; defaults to http).
    if "://" in raw:
        scheme, remainder = raw.split("://", 1)
        scheme = scheme.lower()
    else:
        scheme = "http"
        remainder = raw

    if scheme not in _VALID_SCHEMES:
        raise ValueError(
            f"unknown proxy scheme {scheme!r}; expected one of "
            f"{sorted(_VALID_SCHEMES)}"
        )

    # 2. Optional credentials.
    if "@" in remainder:
        creds, server = remainder.rsplit("@", 1)
        if ":" not in creds:
            raise ValueError(
                f"proxy credentials must be user:pass, got {creds!r}"
            )
        user, password = creds.split(":", 1)
    else:
        server = remainder
        user, password = "", ""

    # 3. host:port.
    if server.count(":") != 1:
        raise ValueError(
            f"proxy server must be host:port, got {server!r}"
        )
    host, port = server.split(":", 1)
    if not host or not port.isdigit():
        raise ValueError(f"invalid host or port in proxy server {server!r}")

    return scheme, user, password, host, port


# ── Extension generation ────────────────────────────────────────────────
def create_proxy_extension(
    proxy_string: Optional[str],
    extension_folder_name: Optional[str] = None,
    *,
    socks_auth_policy: SocksAuthPolicy = "downgrade",
    bypass_list: Optional[list[str]] = None,
) -> Optional[str]:
    """Generate a Manifest V3 Chrome extension that routes traffic through
    ``proxy_string`` and answers any ``onAuthRequired`` challenge.

    Args:
        proxy_string: A proxy URL in any of the forms listed in
            :func:`parse_proxy_url`. **If ``None`` or empty, the
            function does nothing and returns ``None``.** Callers
            should branch on the return value: ``None`` means "do not
            attach any extension". Nothing is ever written to disk on
            this code path.
        extension_folder_name: Where to write the extension's two
            files. If ``None`` (default), a fresh
            ``tempfile.mkdtemp(prefix="dp_proxy_ext_")`` is created
            so each call is isolated and concurrent callers cannot
            collide. Existing folders are reused (and overwritten)
            when the caller supplies an explicit name. The caller
            owns cleanup of the folder; see
            :class:`workers.core.browser_core.InstagramBrowser.close`.
        socks_auth_policy: How to handle SOCKS4/5 proxies that ship
            credentials. See module docstring for details. Defaults
            to ``"downgrade"`` (warn + rewrite scheme to ``http``).
        bypass_list: Hosts that should bypass the proxy. Defaults to
            ``["localhost", "127.0.0.1", "::1"]``. Pass an explicit
            list (e.g. with internal corp domains) to extend, or pass
            ``[]`` to disable bypass entirely (rare — health probes
            usually want loopback bypass).

    Returns:
        Absolute path to the generated extension folder, or ``None``
        if ``proxy_string`` was falsy (no extension was created).
    """
    # ── Guard 1: no proxy → no extension. ─────────────────────────────
    if not proxy_string or not str(proxy_string).strip():
        logger.debug(
            "[proxy_builder] proxy_string is empty/None — not generating extension"
        )
        return None

    # ── Parse + validate. ─────────────────────────────────────────────
    scheme, proxy_user, proxy_pass, proxy_host, proxy_port = parse_proxy_url(
        proxy_string
    )

    # ── SOCKS + credentials guard. ────────────────────────────────────
    # Only kicks in when BOTH conditions hold: scheme is SOCKS *and*
    # creds are present. SOCKS without creds (IP-whitelisted at the
    # provider) is fine — Chrome routes via SOCKS without the auth
    # callback ever firing.
    if scheme not in _AUTH_CAPABLE_SCHEMES and (proxy_user or proxy_pass):
        warning_msg = (
            "Chrome's webRequestAuthProvider does NOT support per-request "
            f"auth for {scheme!r} proxies — only HTTP/HTTPS. Supplying "
            f"credentials with a {scheme} URL will produce "
            "ERR_SOCKS_CONNECTION_FAILED at connect time."
        )
        if socks_auth_policy == "strict":
            raise ValueError(warning_msg + " (policy=strict)")

        logger.warning("=" * 70)
        logger.warning(warning_msg)
        if socks_auth_policy == "downgrade":
            logger.warning(
                "Auto-downgrading scheme %r → 'http' (policy=downgrade). "
                "Most proxy providers expose both transports on the same "
                "port. Pass socks_auth_policy='strict' to refuse, or "
                "'keep' to preserve the SOCKS scheme and accept the failure.",
                scheme,
            )
            scheme = "http"
        else:  # "keep"
            logger.warning(
                "Keeping scheme %r as requested (policy=keep). Expect "
                "connection failures.",
                scheme,
            )
        logger.warning("=" * 70)

    # ── Resolve target folder. ────────────────────────────────────────
    if extension_folder_name:
        folder = os.path.abspath(extension_folder_name)
        os.makedirs(folder, exist_ok=True)
    else:
        folder = tempfile.mkdtemp(prefix=_TEMP_FOLDER_PREFIX)

    # ── manifest.json (Manifest V3). ─────────────────────────────────
    # Notes:
    #   * "manifest_version": 3
    #   * "background.service_worker" replaces V2's "background.scripts"
    #   * "<all_urls>" lives under "host_permissions" in V3
    #   * "webRequestAuthProvider" must be in "permissions" — without
    #     it, MV3 silently rejects onAuthRequired listeners with
    #     "blocking", which is exactly the ERR_SOCKS_CONNECTION_FAILED
    #     symptom the operator was seeing.
    manifest: dict = {
        "name": "DrissionPage Proxy Auth",
        "version": "1.0.0",
        "manifest_version": 3,
        "permissions": [
            "proxy",
            "storage",
            "tabs",
            "unlimitedStorage",
            "webRequest",
            "webRequestAuthProvider",
        ],
        "host_permissions": ["<all_urls>"],
        "background": {
            "service_worker": "background.js",
        },
        "minimum_chrome_version": "108",
    }

    # ── background.js (Service Worker). ──────────────────────────────
    # JSON-encode every interpolated value so a credential containing
    # a quote, backslash, or unicode char can't break out of the JS
    # string literal.
    bg_scheme = json.dumps(scheme)
    bg_host = json.dumps(proxy_host)
    bg_port = int(proxy_port)  # already validated as digits in parse step
    bg_user = json.dumps(proxy_user)
    bg_pass = json.dumps(proxy_pass)
    bg_bypass = json.dumps(list(bypass_list) if bypass_list is not None else _DEFAULT_BYPASS_LIST)

    background_js = f"""\
// Auto-generated by workers/utils/proxy_builder.py — do not edit by hand.
// Manifest V3 service worker. Two responsibilities:
//   1. Configure Chrome's proxy.settings to route every request via
//      the upstream proxy.
//   2. Answer onAuthRequired with the credentials parsed from the
//      proxy URL. We use the synchronous "blocking" extra info, which
//      MV3 retains support for ONLY when the extension also declares
//      the "webRequestAuthProvider" permission (see manifest above).

const PROXY_CONFIG = {{
    mode: "fixed_servers",
    rules: {{
        singleProxy: {{
            scheme: {bg_scheme},
            host:   {bg_host},
            port:   {bg_port}
        }},
        bypassList: {bg_bypass}
    }}
}};

chrome.proxy.settings.set(
    {{ value: PROXY_CONFIG, scope: "regular" }},
    function () {{
        // Setting persists in the profile; nothing to do in the callback.
    }}
);

const PROXY_USER = {bg_user};
const PROXY_PASS = {bg_pass};

// Synchronous "blocking" listener. MV3 allows this for onAuthRequired
// specifically when "webRequestAuthProvider" is in permissions.
// Returning {{ authCredentials }} tells Chrome to satisfy the proxy's
// HTTP 407 challenge with these creds.
chrome.webRequest.onAuthRequired.addListener(
    function (details) {{
        return {{
            authCredentials: {{
                username: PROXY_USER,
                password: PROXY_PASS
            }}
        }};
    }},
    {{ urls: ["<all_urls>"] }},
    ["blocking"]
);
"""

    # ── Write files. ─────────────────────────────────────────────────
    manifest_path = os.path.join(folder, "manifest.json")
    background_path = os.path.join(folder, "background.js")
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)
    with open(background_path, "w", encoding="utf-8") as f:
        f.write(background_js)

    logger.info(
        "[proxy_builder] generated MV3 extension at %s (scheme=%s, host=%s:%d, "
        "auth=%s)",
        folder, scheme, proxy_host, bg_port, bool(proxy_user or proxy_pass),
    )
    return folder
