"""Proxy builder. Manifest V3, blocking auth.

Builds an unpacked Chrome extension on the fly so DrissionPage can drive
Chromium against authed HTTP / HTTPS / SOCKS proxies without hitting the
native basic-auth dialog.

Main points:
- Manifest V3. Current stable Chrome rejects MV2 extensions.
- Blocking auth listener. In MV3 the general webRequestBlocking permission
  is restricted, but onAuthRequired still works with "blocking" when the
  extension declares the webRequestAuthProvider permission. The listener
  returns {authCredentials: ...} synchronously, which is the simplest and
  most reliable shape for proxy auth.
- No extension when there is no proxy. create_proxy_extension returns None
  for empty proxy_string. Callers check the return value, nothing is
  written to disk, so DrissionPage can not pick up a stale extension.
- Temp folder by default. create_proxy_extension writes to
  tempfile.mkdtemp(prefix="dp_proxy_ext_") unless the caller passes a
  folder. Cleanup is the caller's job (see InstagramBrowser.close).
- Bypass list. localhost and 127.0.0.1 always skip the proxy, so health
  checks and devtools talk direct.

SOCKS auth caveat:
Chrome's chrome.proxy API accepts socks4 / socks5 schemes, but Chrome does
NOT fire onAuthRequired for SOCKS, that callback only runs for HTTP/HTTPS.
A SOCKS proxy with credentials gives ERR_SOCKS_CONNECTION_FAILED at connect.
Default socks_auth_policy="downgrade" rewrites the scheme to http and logs
a big warning. Most commercial proxy providers expose both transports on
the same port so it just works. Use socks_auth_policy="strict" (raise) or
"keep" (preserve and fail) if you want other behavior.

Public api:
    parse_proxy_url(url) -> (scheme, user, pass, host, port)
    create_proxy_extension(proxy_string, extension_folder_name=None) -> Optional[str]
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from typing import Literal, Optional, Tuple

logger = logging.getLogger(__name__)


# constants
# schemes that chrome.proxy.settings.set will accept.
_VALID_SCHEMES: frozenset[str] = frozenset({"http", "https", "socks4", "socks5"})

# schemes that support per-request proxy auth via Chrome's
# webRequestAuthProvider permission. SOCKS does not, onAuthRequired only
# fires for HTTP and HTTPS proxies, so a SOCKS proxy that wants user/pass
# gives ERR_SOCKS_CONNECTION_FAILED at connect.
_AUTH_CAPABLE_SCHEMES: frozenset[str] = frozenset({"http", "https"})

# hosts that always skip the proxy. localhost and ipv4 loopback are a must,
# health probes, devtools and CDP all use them and we can not tunnel them.
_DEFAULT_BYPASS_LIST: list[str] = ["localhost", "127.0.0.1", "::1"]

# prefix for the temp folder when the caller does not pass a name. we use
# tempfile.mkdtemp() by default so the OS picks a safe place (like /tmp on
# linux) and every call gets its own dir.
_TEMP_FOLDER_PREFIX: str = "dp_proxy_ext_"

SocksAuthPolicy = Literal["downgrade", "strict", "keep"]


# url parsing
def parse_proxy_url(proxy_string: str) -> Tuple[str, str, str, str, str]:
    """Parse a proxy URL into (scheme, user, password, host, port).

    Accepts:
        protocol://user:pass@host:port  (preferred)
        user:pass@host:port             (scheme defaults to http)
        host:port                       (scheme defaults to http, no auth)

    Raises:
        ValueError: empty input, bad format, or unknown scheme.
    """
    raw = (proxy_string or "").strip()
    if not raw:
        raise ValueError("proxy_string is empty")

    # 1. scheme. optional, defaults to http.
    if "://" in raw:
        scheme, remainder = raw.split("://", 1)
        scheme = scheme.lower()
    else:
        scheme = "http"
        remainder = raw

    if scheme not in _VALID_SCHEMES:
        raise ValueError(
            f"unknown proxy scheme {scheme!r}, expected one of "
            f"{sorted(_VALID_SCHEMES)}"
        )

    # 2. optional credentials
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

    # 3. host:port
    if server.count(":") != 1:
        raise ValueError(
            f"proxy server must be host:port, got {server!r}"
        )
    host, port = server.split(":", 1)
    if not host or not port.isdigit():
        raise ValueError(f"invalid host or port in proxy server {server!r}")

    return scheme, user, password, host, port


# extension generation
def create_proxy_extension(
    proxy_string: Optional[str],
    extension_folder_name: Optional[str] = None,
    *,
    socks_auth_policy: SocksAuthPolicy = "downgrade",
    bypass_list: Optional[list[str]] = None,
) -> Optional[str]:
    """Generate a Manifest V3 Chrome extension that routes traffic through
    `proxy_string` and answers any onAuthRequired challenge.

    Args:
        proxy_string: a proxy URL in any of the forms listed in
            parse_proxy_url. If None or empty, the function does nothing
            and returns None. Callers branch on the return value, None
            means "do not attach any extension". Nothing is written to
            disk in that case.
        extension_folder_name: where to write the extension files. If
            None (default), a fresh tempfile.mkdtemp(prefix="dp_proxy_ext_")
            is made, so every call is isolated and concurrent callers do
            not clash. If the caller passes a name, that folder is reused
            (and overwritten). The caller owns folder cleanup, see
            workers.core.browser_core.InstagramBrowser.close.
        socks_auth_policy: how to handle SOCKS4/5 proxies that ship
            credentials. See module docstring. Default is "downgrade",
            warn and rewrite scheme to http.
        bypass_list: hosts that skip the proxy. Default is
            ["localhost", "127.0.0.1", "::1"]. Pass a list to extend it
            (like internal company domains) or pass [] to turn bypass
            off (rare, health probes usually want loopback bypass).

    Returns:
        Absolute path to the new extension folder, or None when
        proxy_string was empty (no extension was created).
    """
    # guard 1: no proxy means no extension
    if not proxy_string or not str(proxy_string).strip():
        logger.debug(
            "[proxy_builder] proxy_string is empty/None, no extension"
        )
        return None

    # parse and validate
    scheme, proxy_user, proxy_pass, proxy_host, proxy_port = parse_proxy_url(
        proxy_string
    )

    # SOCKS + credentials guard.
    # only kicks in when both: scheme is SOCKS AND there are creds.
    # SOCKS without creds (provider whitelisted our IP) is fine, Chrome
    # routes via SOCKS and the auth callback never fires.
    if scheme not in _AUTH_CAPABLE_SCHEMES and (proxy_user or proxy_pass):
        warning_msg = (
            "Chrome's webRequestAuthProvider does NOT support per-request "
            f"auth for {scheme!r} proxies, only HTTP/HTTPS. Sending "
            f"credentials with a {scheme} URL will give "
            "ERR_SOCKS_CONNECTION_FAILED at connect."
        )
        if socks_auth_policy == "strict":
            raise ValueError(warning_msg + " (policy=strict)")

        logger.warning("=" * 70)
        logger.warning(warning_msg)
        if socks_auth_policy == "downgrade":
            logger.warning(
                "Auto-downgrading scheme %r to 'http' (policy=downgrade). "
                "Most proxy providers expose both transports on the same "
                "port. Use socks_auth_policy='strict' to refuse, or "
                "'keep' to keep the SOCKS scheme and accept the failure.",
                scheme,
            )
            scheme = "http"
        else:  # "keep"
            logger.warning(
                "Keeping scheme %r (policy=keep). Expect connection "
                "failures.",
                scheme,
            )
        logger.warning("=" * 70)

    # pick target folder
    if extension_folder_name:
        folder = os.path.abspath(extension_folder_name)
        os.makedirs(folder, exist_ok=True)
    else:
        folder = tempfile.mkdtemp(prefix=_TEMP_FOLDER_PREFIX)

    # manifest.json (MV3) notes:
    #   - "manifest_version": 3
    #   - "background.service_worker" replaces V2's "background.scripts"
    #   - "<all_urls>" goes under "host_permissions" in V3
    #   - "webRequestAuthProvider" must be in "permissions". Without it
    #     MV3 silently drops onAuthRequired listeners that use "blocking",
    #     which is the exact ERR_SOCKS_CONNECTION_FAILED symptom we saw.
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

    # background.js (service worker).
    # json-encode every value we interpolate, so a credential with a
    # quote, backslash or unicode can not break out of the JS string.
    bg_scheme = json.dumps(scheme)
    bg_host = json.dumps(proxy_host)
    bg_port = int(proxy_port)  # already checked as digits in parse step
    bg_user = json.dumps(proxy_user)
    bg_pass = json.dumps(proxy_pass)
    bg_bypass = json.dumps(list(bypass_list) if bypass_list is not None else _DEFAULT_BYPASS_LIST)

    background_js = f"""\
// Auto-generated by workers/utils/proxy_builder.py, do not edit by hand.
// MV3 service worker. Two jobs:
//   1. set chrome.proxy.settings so every request goes through the
//      upstream proxy.
//   2. answer onAuthRequired with the creds we parsed from the proxy URL.
//      we use the sync "blocking" form, which MV3 only allows when the
//      extension also declares "webRequestAuthProvider" in permissions
//      (see manifest above).

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
        // setting is stored in the profile, nothing to do in the callback.
    }}
);

const PROXY_USER = {bg_user};
const PROXY_PASS = {bg_pass};

// sync "blocking" listener. MV3 allows it for onAuthRequired when the
// extension has "webRequestAuthProvider" in permissions. Returning
// { authCredentials } tells Chrome to answer the proxy HTTP 407 with
// these creds.
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

    # write files
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
