#!/usr/bin/env python3
"""Standalone proxy diagnostic.

Isolates the proxy path so we see EXACTLY where it breaks: URL parse →
MV3 extension build → Chromium launch → real request through the proxy.

Usage:
  cd /home/sk8ver/Documents/Projects/CRM/IG_CRM
  ./.venv/bin/python test_proxy.py "socks5://user:pass@host:port"
  # also accepts the colon format providers like to ship:
  #   ./.venv/bin/python test_proxy.py "host:port:user:pass"
  # interactive:  ./.venv/bin/python test_proxy.py     (it will ask)
  # headless:  HEADLESS=1 ./.venv/bin/python test_proxy.py "http://u:p@h:port"
"""
from __future__ import annotations

import os
import sys
import traceback
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "backend"))

IP_ECHO = "https://api.ipify.org?format=json"


def _normalize(raw: str) -> str:
    """Accept the common colon format host:port:user:pass and turn it into a
    proper URL. Anything that already has a scheme or @ is left alone."""
    raw = (raw or "").strip()
    if not raw:
        return raw
    if "://" in raw or "@" in raw:
        return raw
    parts = raw.split(":")
    if len(parts) == 4:
        host, port, user, pwd = parts
        return f"http://{user}:{pwd}@{host}:{port}"
    return raw


def _build_from_prompt() -> str:
    proto = input("protocol [http/https/socks4/socks5] (default http): ").strip() or "http"
    host = input("host: ").strip()
    port = input("port: ").strip()
    user = input("username (blank if none): ").strip()
    pwd = input("password (blank if none): ").strip()
    if user or pwd:
        return f"{proto}://{user}:{pwd}@{host}:{port}"
    return f"{proto}://{host}:{port}"


def main() -> None:
    proxy_string = _normalize(sys.argv[1] if len(sys.argv) > 1 else _build_from_prompt())
    headless = os.environ.get("HEADLESS", "0") == "1"
    print("=" * 60)
    print(f"  proxy = {proxy_string}")
    print(f"  headless = {headless}")
    print("=" * 60)

    # 0. your direct (home) IP for comparison
    try:
        direct = urllib.request.urlopen(IP_ECHO, timeout=10).read().decode()
        print(f"[0] direct IP (no proxy): {direct}")
    except Exception as e:
        print(f"[0] direct IP check failed: {e}")

    # 1. parse
    try:
        from workers.utils.proxy_builder import parse_proxy_url
        scheme, user, pwd, host, port = parse_proxy_url(proxy_string)
        print(f"[1] parse OK: scheme={scheme} host={host} port={port} auth={bool(user or pwd)}")
    except Exception:
        print("[1] PARSE FAILED:")
        traceback.print_exc()
        return

    # 2. build MV3 extension
    try:
        from workers.utils.proxy_builder import create_proxy_extension
        folder = create_proxy_extension(proxy_string)
        print(f"[2] extension built at: {folder}")
        print("    manifest exists:", os.path.exists(os.path.join(folder or "", "manifest.json")))
    except Exception:
        print("[2] EXTENSION BUILD FAILED:")
        traceback.print_exc()
        return

    # 3. launch browser + 4. request through proxy
    browser = None
    try:
        from workers.core.browser_core import InstagramBrowser
        print("[3] launching Chromium through the proxy…")
        browser = InstagramBrowser(proxy_string=proxy_string, headless=headless)
        print("[3] launched OK")
        print("[4] navigating to IP echo through the proxy…")
        browser.page.get(IP_ECHO)
        browser.page.wait(3)
        body = ""
        try:
            body = browser.page.ele("tag:body").text
        except Exception:
            body = browser.page.html[:300]
        print(f"[4] page says (should be the PROXY IP, not your home IP):\n    {body[:300]}")
        print("\n[OK] If the IP above differs from [0], the proxy WORKS. ✅")
    except Exception:
        print("[3/4] BROWSER/REQUEST FAILED:")
        traceback.print_exc()
    finally:
        if browser is not None:
            try:
                browser.close()
            except Exception:
                pass
    print("=" * 60)


if __name__ == "__main__":
    main()
