"""
Proxy Builder Utility
---------------------
Generates an unzipped Chrome extension (Manifest V2) on the fly so
DrissionPage can drive a Chromium that talks to authenticated
HTTP / HTTPS / SOCKS proxies without crashing on the OS-level basic-auth
dialog.

Caveats — SOCKS authentication
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Chrome's ``chrome.proxy`` API accepts ``socks4`` / ``socks5`` schemes,
but Chrome does NOT support per-request SOCKS auth via
``webRequest.onAuthRequired`` — that callback only fires for HTTP/HTTPS.
For SOCKS proxies the credentials are still written into the extension
so providers that accept ``user:pass@host:port`` URLs at the SOCKS layer
keep working, but providers that require IP-whitelisting are the safer
default.

Public API
~~~~~~~~~~
``parse_proxy_url(url) -> (scheme, user, pass, host, port)``
``create_proxy_extension(proxy_string, extension_folder_name) -> str``

The two-arg ``create_proxy_extension`` API is preserved for callers that
already pass ``user:pass@host:port`` strings — those default to scheme
``http``. New callers should pass a full URL like
``socks5://user:pass@host:1080``.
"""

from __future__ import annotations

import json
import os
from typing import Tuple

# Schemes Chrome's `chrome.proxy.settings.set` accepts.
_VALID_SCHEMES: frozenset[str] = frozenset({"http", "https", "socks4", "socks5"})


def parse_proxy_url(proxy_string: str) -> Tuple[str, str, str, str, str]:
    """Parse a proxy URL into ``(scheme, user, password, host, port)``.

    Accepts:
        * ``protocol://user:pass@host:port``  (preferred)
        * ``user:pass@host:port``             (assumed ``http``)
        * ``host:port``                       (assumed ``http``, no auth)

    Raises:
        ValueError: on malformed input or an unknown scheme.
    """
    raw = (proxy_string or "").strip()
    if not raw:
        raise ValueError("proxy_string is empty")

    # 1. Scheme (optional).
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


def create_proxy_extension(
    proxy_string: str,
    extension_folder_name: str = "proxy_auth_plugin",
) -> str:
    """Generate a Manifest V2 Chrome extension that routes traffic through
    ``proxy_string`` and answers any ``onAuthRequired`` challenge.

    Args:
        proxy_string: A proxy URL in any of the forms listed in
            :func:`parse_proxy_url`. The scheme determines which Chrome
            transport is configured (``http`` / ``https`` / ``socks4`` /
            ``socks5``).
        extension_folder_name: Directory name (under the current working
            dir) into which ``manifest.json`` and ``background.js`` are
            written. The caller owns cleanup of this directory; see
            :class:`workers.core.browser_core.InstagramBrowser.close`.

    Returns:
        Path to the generated extension folder, ready to feed into
        ``ChromiumOptions.add_extension``.
    """
    scheme, proxy_user, proxy_pass, proxy_host, proxy_port = parse_proxy_url(
        proxy_string
    )

    manifest_json = """
    {
        "version": "1.0.0",
        "manifest_version": 2,
        "name": "DP Proxy Auth",
        "permissions": [
            "proxy", "tabs", "unlimitedStorage", "storage", "<all_urls>", "webRequest", "webRequestBlocking"
        ],
        "background": {
            "scripts": ["background.js"]
        },
        "minimum_chrome_version":"22.0.0"
    }
    """

    # JSON-encode every interpolated value so a credential containing a
    # quote, backslash, or unicode char can't break out of the JS string.
    bg_scheme = json.dumps(scheme)
    bg_host = json.dumps(proxy_host)
    bg_port = int(proxy_port)  # already validated as digits in parse step
    bg_user = json.dumps(proxy_user)
    bg_pass = json.dumps(proxy_pass)

    background_js = f"""
    var config = {{
            mode: "fixed_servers",
            rules: {{
              singleProxy: {{
                scheme: {bg_scheme},
                host: {bg_host},
                port: parseInt({bg_port})
              }},
              bypassList: ["localhost"]
            }}
          }};
    chrome.proxy.settings.set({{value: config, scope: "regular"}}, function() {{}});
    function callbackFn(details) {{
        return {{
            authCredentials: {{
                username: {bg_user},
                password: {bg_pass}
            }}
        }};
    }}
    chrome.webRequest.onAuthRequired.addListener(
            callbackFn,
            {{urls: ["<all_urls>"]}},
            ['blocking']
    );
    """

    os.makedirs(extension_folder_name, exist_ok=True)

    with open(
        os.path.join(extension_folder_name, "manifest.json"), "w", encoding="utf-8"
    ) as f:
        f.write(manifest_json.strip())

    with open(
        os.path.join(extension_folder_name, "background.js"), "w", encoding="utf-8"
    ) as f:
        f.write(background_js.strip())

    return extension_folder_name
