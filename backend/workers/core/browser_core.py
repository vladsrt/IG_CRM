"""Browser engine core.

Wraps DrissionPage Chromium instances with proxies and custom user agents.
Makes sure we do not leak temp extension folders if something crashes.

Proxy auth strategy:
  We spawn a tiny local HTTP forwarder (pproxy) per browser, on a random
  127.0.0.1 port. The forwarder holds the upstream proxy credentials and
  presents itself as a NO-AUTH HTTP proxy to Chrome. Chrome is told via
  --proxy-server=http://127.0.0.1:<port> — so Chrome never sees a 407,
  never pops the auth dialog, never has the MV3 service-worker boot race.
  The forwarder process is owned by the InstagramBrowser instance and
  killed in close().
"""

import os
import shutil
import socket
import subprocess
import sys
import time
from typing import Dict, List, Optional

from DrissionPage import ChromiumPage, ChromiumOptions


# Chrome binary candidates, in order of preference.
# We pick the FIRST one that exists and is NOT a snap wrapper. snap-confined
# Chromium can't read /tmp/ (confinement=strict, only `home` + `removable-media`
# plugs), so passing --load-extension=/tmp/dp_proxy_ext_XXX silently fails
# with "Failed to load extension from: . Manifest file is missing or unreadable"
# and the proxy is NEVER applied → user IP leaks via direct connection.
# DrissionPage's default `which('chrome') or which('chromium')` picks
# /snap/bin/chromium first on Ubuntu, so we override it explicitly.
_CHROME_BINARY_CANDIDATES: tuple[str, ...] = (
    "/opt/google/chrome/google-chrome",   # deb-installed Google Chrome (no snap)
    "/opt/google/chrome/chrome",
    "/usr/bin/google-chrome-stable",
    "/usr/bin/chromium-browser",           # deb-installed Chromium, NOT snap
)


def _resolve_chrome_binary() -> Optional[str]:
    """Return the first non-snap Chrome binary on disk, or None.

    /usr/bin/chromium-browser on Ubuntu can be the snap shim — we detect that
    via the shebang/grep and skip it. /snap/bin/chromium is always snap.
    """
    for path in _CHROME_BINARY_CANDIDATES:
        if not os.path.isfile(path):
            continue
        # /usr/bin/chromium-browser on Ubuntu is a wrapper shell script that
        # `exec /snap/bin/chromium "$@"`. Skip it if so.
        try:
            with open(path, "rb") as f:
                head = f.read(512)
            if b"/snap/bin/chromium" in head or b"snap install chromium" in head:
                continue
        except OSError:
            continue
        return path
    return None


def _pick_free_local_port() -> int:
    """Ask the kernel for an unused TCP port on 127.0.0.1."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _spawn_local_auth_forwarder(
    upstream_proxy_url: str,
) -> tuple[int, "subprocess.Popen[bytes]"]:
    """Start a localhost HTTP proxy that forwards to upstream with auth.

    Architecture:
        Chrome --no-auth--> 127.0.0.1:<port>  --(adds Proxy-Auth header)--> upstream

    Why this exists:
        Chrome 147 + MV3 has a fatal race for AUTHENTICATED proxies:
        the proxy is applied at Chrome startup, but the service worker
        that should answer 407 challenges (via webRequestAuthProvider)
        boots LAZILY. The first request through the proxy fires before
        the SW has registered onAuthRequired → Chrome shows the native
        login dialog → user IP leaks if the dialog is dismissed.

        By moving auth OFF Chrome entirely and into a local pproxy
        subprocess, Chrome only ever sees an unauthenticated localhost
        proxy. No 407, no dialog, no race.

    Args:
        upstream_proxy_url: "scheme://user:pass@host:port" — the original
            proxy URL as parsed by parse_proxy_url().

    Returns:
        (local_port, popen) — the port Chrome should point at, and the
        Popen handle the caller owns (terminate it in close()).

    Raises:
        RuntimeError: forwarder failed to bind within 3s.
    """
    from workers.utils.proxy_builder import parse_proxy_url

    scheme, user, password, host, port = parse_proxy_url(upstream_proxy_url)
    if scheme not in ("http", "https"):
        # SOCKS upstream — pproxy supports it too, but we mark scheme so
        # the upstream URL is correct.
        upstream_scheme = scheme
    else:
        upstream_scheme = scheme

    # pproxy upstream URL: protocol://host:port#user:pass
    # (auth uses # as separator, not standard URL @ format)
    if user or password:
        upstream = f"{upstream_scheme}://{host}:{port}#{user}:{password}"
    else:
        upstream = f"{upstream_scheme}://{host}:{port}"

    local_port = _pick_free_local_port()
    # `-l http://127.0.0.1:N`  = listen as HTTP (no auth) on localhost
    # `-r <upstream>`          = forward everything to upstream w/ auth
    cmd = [
        sys.executable, "-m", "pproxy",
        "-l", f"http://127.0.0.1:{local_port}",
        "-r", upstream,
    ]
    # Capture stderr to a temp file so if pproxy dies on launch we know
    # WHY — without this, the only error you ever see is the generic
    # "died on launch (exit=1)" with no clue about pproxy's complaint
    # (missing module, bad upstream URL, port collision, etc).
    import tempfile
    err_log = tempfile.NamedTemporaryFile(
        prefix=f"pproxy_{local_port}_", suffix=".err",
        delete=False, mode="w+b",
    )
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=err_log,
        # new session so the forwarder survives if our caller is
        # interrupted with Ctrl+C and gets a chance to clean up via close().
        start_new_session=True,
    )

    def _read_stderr_tail() -> str:
        """Read whatever pproxy wrote to its stderr file, trimmed."""
        try:
            err_log.flush()
            err_log.seek(0)
            data = err_log.read().decode("utf-8", "replace").strip()
            return data[-800:] if data else "(empty stderr)"
        except Exception as exc:
            return f"(stderr read failed: {exc})"

    # Wait up to 3s for the port to actually accept connections — if
    # pproxy crashed on bad args, we don't want Chrome to launch and
    # try a dead proxy.
    deadline = time.time() + 3.0
    while time.time() < deadline:
        if proc.poll() is not None:
            err = _read_stderr_tail()
            raise RuntimeError(
                f"local proxy forwarder died on launch "
                f"(exit={proc.returncode}, upstream={host}:{port}). "
                f"pproxy stderr: {err}"
            )
        try:
            with socket.create_connection(("127.0.0.1", local_port), timeout=0.3):
                # success path — leave err_log open; if pproxy spews errors
                # later, we keep capturing them. Tempfile is auto-cleaned by
                # the OS eventually; we don't strictly need to manage it.
                return local_port, proc
        except OSError:
            time.sleep(0.05)
    # didn't start in time
    proc.terminate()
    raise RuntimeError(
        f"local proxy forwarder failed to bind 127.0.0.1:{local_port} within 3s "
        f"(upstream={host}:{port})"
    )


class InstagramBrowser:
    """Chromium browser set up for IG automation, with safe shutdown."""

    def __init__(
        self,
        proxy_string: Optional[str],
        user_agent: Optional[str] = None,
        headless: bool = False,
        task_id: Optional[str] = None,
        *,
        account_user_agent: Optional[str] = None,
        account_platform: Optional[str] = None,
        user_data_dir: Optional[str] = None,
    ):
        """Set up the browser.

        Args:
            proxy_string: Proxy URL (protocol://user:pass@host:port). Pass None to skip.
            user_agent: explicit User-Agent override.
            headless: run in headless mode.
            task_id: Task UUID, used to make the proxy extension folder name unique.
            account_user_agent: UA saved on the account in the db.
            account_platform: OS platform, used to pick a UA if nothing else is set.
            user_data_dir: path to a Chromium profile. "fresh" makes a new tempdir.

        Raises:
            ValueError: bad proxy format.
        """
        # turn empty string into None so the rest of the method has one
        # clear "no proxy" signal to check.
        self.proxy_string: Optional[str] = (
            proxy_string.strip() if isinstance(proxy_string, str) and proxy_string.strip() else None
        )
        self._uses_proxy: bool = self.proxy_string is not None
        # pick the UA up front. order: explicit > account-pinned > pool-by-
        # platform > Windows fallback. doing it here (not in the caller)
        # keeps the priority rule in one place and the action handler API
        # the same.
        self.user_agent = self._resolve_user_agent(
            explicit=user_agent,
            account_user_agent=account_user_agent,
            account_platform=account_platform,
        )
        self.task_id = task_id

        # init the cleanup attrs BEFORE any line that can raise. that way
        # self.close() is safe to call from the except branch even if the
        # constructor blew up half way through.
        self.page: Optional[ChromiumPage] = None
        self.co: Optional[ChromiumOptions] = None
        self.plugin_path: str = ""  # legacy MV3 ext path (unused, kept for janitor)
        # local pproxy forwarder lifecycle handles
        self._local_proxy_proc: Optional["subprocess.Popen[bytes]"] = None
        self._local_proxy_port: Optional[int] = None
        # caller-supplied user_data_dir path (set when explicit path passed).
        self._user_data_dir: str = ""

        try:
            # janitor: sweep dp_proxy_ext_* + runtime_proxy_plugin_* folders
            # left in /tmp by a previous worker that died before close()
            # (Ctrl+C from dev.sh, OOM, etc). Older than 1h so we never touch
            # a folder a sibling worker just created.
            try:
                import tempfile
                self.cleanup_orphaned_extensions(
                    root=tempfile.gettempdir(), older_than_seconds=3600
                )
            except Exception as janitor_exc:
                print(f"[*] tempfile janitor: skipped ({janitor_exc})")

            # build ChromiumOptions
            self.co = ChromiumOptions()

            # Pin Chrome binary to a non-snap install. DrissionPage's default
            # path resolution (which('chrome') or which('chromium')) picks
            # /snap/bin/chromium first on Ubuntu, and snap-confined Chromium
            # can't read /tmp/ → MV3 proxy extension never loads → home IP
            # leaks. Falling back to DP default if no candidate exists keeps
            # the worker bootable on hosts where Chrome isn't installed yet.
            chrome_bin = _resolve_chrome_binary()
            if chrome_bin:
                try:
                    self.co.set_browser_path(chrome_bin)
                    print(f"[*] Chrome binary pinned: {chrome_bin}")
                except Exception as exc:
                    print(f"[!] set_browser_path({chrome_bin!r}) failed: {exc}")
            else:
                print(
                    "[!] No non-snap Chrome found on standard paths. "
                    "DrissionPage may fall back to snap chromium, which "
                    "CANNOT load /tmp/ extensions — proxy plugin will silently "
                    "fail. Install google-chrome-stable (deb)."
                )

            # auto_port=True → DrissionPage picks a free CDP port in the
            # 9600..59600 range AND creates a unique temporary profile.
            # Without this, parallel workers connect to the same Chromium on
            # the default port 9222, and the proxy plugin of the second
            # worker never loads.
            self.co.auto_port(True)

            # optional explicit user data dir (pin a profile across runs).
            # auto_port(True) already gives a fresh temp profile — the
            # explicit path hook is for future per-account profile pinning.
            if user_data_dir and user_data_dir != "fresh":
                try:
                    self.co.set_user_data_path(os.path.abspath(user_data_dir))
                    self._user_data_dir = os.path.abspath(user_data_dir)
                    print(f"[*] Chromium user-data-dir set to {self._user_data_dir}")
                except Exception as exc:
                    print(f"[*] DP set_user_data_path({user_data_dir!r}) failed: {exc}")

            if self._uses_proxy:
                # Spawn the local auth-forwarder. Chrome will be told to
                # use 127.0.0.1:<port> as a NO-AUTH HTTP proxy; the forwarder
                # holds the upstream creds and adds Proxy-Authorization for
                # every request. No 407 ever reaches Chrome → no native
                # auth dialog, no MV3 SW boot race, no IP leak.
                self._local_proxy_port, self._local_proxy_proc = (
                    _spawn_local_auth_forwarder(self.proxy_string)  # type: ignore[arg-type]
                )
                print(
                    f"[*] Local proxy forwarder up at 127.0.0.1:{self._local_proxy_port} "
                    f"-> {self._redacted_proxy()}"
                )
                # Point Chrome at the forwarder. CRITICAL: bypass loopback
                # so CDP / devtools traffic (also on 127.0.0.1) doesn't
                # ricochet back into the proxy and deadlock.
                try:
                    self.co.set_argument(
                        f"--proxy-server=http://127.0.0.1:{self._local_proxy_port}"
                    )
                    # bypass everything on localhost EXCEPT our forwarder port.
                    # `<-loopback>` is Chromium-specific: it un-bypasses
                    # loopback so the proxy DOES handle 127.0.0.1:<port>;
                    # we still need it because Chrome auto-bypasses loopback.
                    self.co.set_argument("--proxy-bypass-list=<-loopback>")
                    print(
                        f"[*] Chrome proxy-server flag set: "
                        f"http://127.0.0.1:{self._local_proxy_port} (no auth)"
                    )
                except Exception as exc:
                    print(f"[!] Could not set --proxy-server flag: {exc}")
            else:
                # no proxy: hard-disable any cached proxy
                self.plugin_path = ""  # keep close() happy
                self._apply_no_proxy_settings(self.co)
                print("[*] No proxy set, going direct.")

            print(f"[*] Spoofing User-Agent: {self.user_agent}")

            self.co.set_user_agent(self.user_agent)

            # turn off images, pages load faster
            self.co.set_pref("profile.default_content_setting_values.images", 2)
            # block browser notifications
            self.co.set_pref("profile.default_content_setting_values.notifications", 2)

            # headless on/off. Use the NEW headless mode — the old --headless
            # silently DROPS extensions, so the MV3 proxy-auth extension never
            # loads and the proxy is never applied. --headless=new loads it.
            if headless:
                try:
                    self.co.set_argument("--headless=new")
                except Exception:
                    self.co.headless(True)
            else:
                self.co.headless(False)

            # Server / container flags — apply when running as root.
            # On a headless VPS or inside a Docker container the worker runs
            # as root; Chrome's setuid sandbox doesn't work there and Chrome
            # refuses to start without --no-sandbox. We also throw in
            # --disable-dev-shm-usage as belt-and-suspenders even though our
            # docker-compose already raises shm_size to 2gb.
            try:
                if os.geteuid() == 0:
                    self.co.set_argument("--no-sandbox")
                    self.co.set_argument("--disable-dev-shm-usage")
                    print("[*] Detected root — added --no-sandbox + --disable-dev-shm-usage")
            except Exception as exc:
                # geteuid() doesn't exist on Windows; not our concern but be safe.
                print(f"[*] could not detect euid ({exc}), skipping root flags")

            # launch the Page. this is the heavy step. if it raises after
            # the Chrome subprocess is spawned, the except branch below
            # cleans up the orphan via self.close().
            self.page = ChromiumPage(self.co)
            print("[*] Browser launched.")

            # MV3 proxy-auth race fix.
            # Chrome's MV3 service workers boot lazily. If a request fires
            # before the SW has registered its onAuthRequired listener, Chrome
            # falls back to the native proxy auth dialog and the page hangs
            # behind a modal nobody can dismiss in headed mode.
            # 5 seconds gives Chrome time to unpack the extension, run
            # background.js once, register chrome.proxy.settings.set, and
            # commit the proxy config before our first nav.
            if self._uses_proxy:
                time.sleep(5)
                self._verify_proxy_or_raise()
        except Exception:
            # constructor failed in the middle. clean up whatever made it
            # onto disk or into a subprocess before re-raising. the inner
            # try/except in close() makes sure a cleanup error does not
            # hide the real constructor error.
            try:
                self.close()
            except Exception as cleanup_exc:
                print(
                    f"[!] Cleanup during failed __init__ also raised: {cleanup_exc}"
                )
            raise

    # UA / proxy resolution helpers
    @staticmethod
    def _resolve_user_agent(
        *,
        explicit: Optional[str],
        account_user_agent: Optional[str],
        account_platform: Optional[str],
    ) -> str:
        """Pick a UA: explicit > db > random by platform."""
        # local import: keeps the workers/utils/ua_generator dep out of the
        # import path until we actually use it. the module has no side
        # effects so a top-level import would also be fine, this just keeps
        # cold-start time honest.
        from workers.utils.ua_generator import (
            Platform as _Platform,
            get_random_user_agent,
        )

        if explicit:
            return explicit
        if account_user_agent:
            return account_user_agent
        if account_platform:
            try:
                return get_random_user_agent(account_platform)
            except ValueError:
                # unknown platform string, fall through to the safe default.
                pass
        return get_random_user_agent(_Platform.WINDOWS)

    def _redacted_proxy(self) -> str:
        """Return the proxy URL with credentials hidden, safe to log."""
        if not self.proxy_string:
            return "(none)"
        if "@" not in self.proxy_string:
            return self.proxy_string
        # cut the credentials block: everything between "://" (or start) and "@".
        scheme_split = self.proxy_string.split("://", 1)
        if len(scheme_split) == 2:
            scheme, rest = scheme_split
            after_at = rest.split("@", 1)[1]
            return f"{scheme}://***@{after_at}"
        after_at = self.proxy_string.split("@", 1)[1]
        return f"***@{after_at}"

    def _verify_proxy_or_raise(self) -> None:
        """Confirm the proxy is actually routing browser traffic.

        Strategy:
          1. Get the HOST's direct public IP via Python urllib (no browser,
             no proxy — this is the "home IP" we must NOT see in the browser).
          2. Get the browser's effective IP by navigating to api.ipify.org.
          3. If browser_ip == home_ip → proxy DID NOT apply, abort task.
             If they differ → proxy is routing somewhere else (good).

        This is the only reliable test: comparing against the user-configured
        proxy host fails for residential/rotating proxies whose exit IPs
        differ from the gateway. Comparing against the actual home IP catches
        the leak even when the proxy uses a totally different exit pool.
        """
        # Step 1: get home IP without any proxy
        import urllib.request

        home_ip: Optional[str] = None
        for echo_url in ("https://api.ipify.org", "https://ifconfig.me/ip",
                         "https://icanhazip.com"):
            try:
                # Build an opener that explicitly REFUSES any system proxy
                # (HTTP_PROXY/HTTPS_PROXY env vars, gsettings, etc).
                opener = urllib.request.build_opener(
                    urllib.request.ProxyHandler({}),  # disable env proxies
                )
                with opener.open(echo_url, timeout=8) as resp:
                    home_ip = (resp.read().decode("utf-8", "replace")
                               .strip().split()[0])
                if home_ip:
                    break
            except Exception:
                continue
        if not home_ip:
            print(
                "[proxy-verify] WARN: could not establish HOME ip from any "
                "echo service — falling back to RFC1918 / loopback check only."
            )

        # Step 2: get browser's observed IP through the proxy chain
        print("[proxy-verify] checking effective browser IP via api.ipify.org ...")
        try:
            self.page.get("https://api.ipify.org?format=json", timeout=20)
            time.sleep(1)
            try:
                body = self.page.ele("tag:body", timeout=5).text  # type: ignore[arg-type]
            except Exception:
                body = (self.page.html or "")[:300]
        except Exception as exc:
            raise RuntimeError(
                f"proxy verify: could not reach api.ipify.org through the "
                f"browser (proxy={self._redacted_proxy()}): {exc}"
            ) from exc

        import re as _re
        ip_match = _re.search(r"\b(\d{1,3}(?:\.\d{1,3}){3})\b", body or "")
        if not ip_match:
            raise RuntimeError(
                f"proxy verify: api.ipify.org returned no parseable IP. "
                f"Body head: {(body or '')[:200]!r}"
            )
        observed_ip = ip_match.group(1)

        # Step 3: RFC1918 / loopback sanity
        is_private = (
            observed_ip.startswith("127.")
            or observed_ip.startswith("10.")
            or observed_ip.startswith("192.168.")
            or any(observed_ip.startswith(f"172.{i}.") for i in range(16, 32))
        )
        if is_private:
            raise RuntimeError(
                f"proxy verify FAILED: observed browser IP is private "
                f"({observed_ip}). Proxy did not apply — traffic going direct."
            )

        # Step 4: the strict leak test
        if home_ip and observed_ip == home_ip:
            raise RuntimeError(
                f"proxy verify FAILED: observed browser IP ({observed_ip}) "
                f"EQUALS the host's home IP ({home_ip}). The proxy is NOT "
                f"routing browser traffic — task refused to avoid IP leak. "
                f"Likely cause: --proxy-server flag dropped by Chrome, proxy "
                f"server unreachable, or proxy returned a 407 the SW didn't "
                f"answer."
            )

        # All checks passed — log the outcome
        if home_ip:
            print(
                f"[proxy-verify] OK — browser exit IP {observed_ip} differs "
                f"from home IP {home_ip}. Proxy is routing."
            )
        else:
            print(
                f"[proxy-verify] OK (no home-ip oracle) — browser exit IP "
                f"{observed_ip} is public, not loopback/RFC1918."
            )

    @staticmethod
    def _apply_no_proxy_settings(co: ChromiumOptions) -> None:
        """Force a direct connection at every layer DrissionPage/Chromium has."""
        # 1. DrissionPage level clear, best effort.
        try:
            co.set_proxy("")  # type: ignore[arg-type]
        except Exception as exc:
            # some DrissionPage versions do not accept an empty string,
            # others do not expose set_proxy at all. the cli flag below is
            # the authoritative override anyway.
            print(f"[*] DrissionPage set_proxy('') not available ({exc}), using CLI flag")

        # 2. chromium command line flag, this one is authoritative.
        try:
            co.set_argument("--no-proxy-server")
        except Exception as exc:
            print(f"[!] Could not set --no-proxy-server flag: {exc}")

    @staticmethod
    def _sanitize_cookie(raw: Dict) -> Dict | None:
        """Normalize an exported cookie into something CDP Network.setCookie
        accepts. Browser-extension exports (Cookie-Editor etc.) carry fields CDP
        rejects: sameSite='no_restriction', expirationDate, hostOnly,
        firstPartyDomain, partitionKey, storeId. We keep only the valid subset.
        """
        name = raw.get("name")
        if not name:
            return None
        out: Dict = {
            "name": name,
            "value": raw.get("value") or "",
            "path": raw.get("path") or "/",
        }
        if raw.get("domain"):
            out["domain"] = raw["domain"]
        if "secure" in raw:
            out["secure"] = bool(raw["secure"])
        if "httpOnly" in raw:
            out["httpOnly"] = bool(raw["httpOnly"])
        # sameSite: CDP wants Strict/Lax/None; exports use no_restriction/lax/...
        ss = {"no_restriction": "None", "none": "None", "lax": "Lax",
              "strict": "Strict"}.get(str(raw.get("sameSite", "")).lower())
        if ss:
            out["sameSite"] = ss
            if ss == "None":
                out["secure"] = True  # SameSite=None requires Secure
        # expiry: accept expirationDate / expiry / expires
        exp = raw.get("expirationDate") or raw.get("expiry") or raw.get("expires")
        if exp:
            try:
                out["expires"] = float(exp)
            except (TypeError, ValueError):
                pass
        return out

    def inject_cookies(self, cookies_list: List[Dict[str, str]]) -> None:
        """Set cookies in the browser. We navigate to the target domain first.

        Each cookie is sanitized for CDP and injected independently, so one bad
        cookie can't abort the whole session.

        After injection we navigate to instagram.com and HARD-VERIFY the home
        feed actually rendered. Without this, a slow/dead proxy can leave the
        browser stuck on robots.txt forever — every downstream action then
        spends its retry budget hunting for IG DOM elements that simply
        aren't there. Failing fast with a clear error here is much better.
        """
        print("[*] Navigating to robots.txt to set domain context...")
        self.page.get("https://www.instagram.com/robots.txt")
        time.sleep(2)

        print("[*] Injecting session cookies...")
        injected = 0
        for raw in cookies_list:
            clean = self._sanitize_cookie(raw)
            if clean is None:
                continue
            try:
                self.page.set.cookies(clean)
                injected += 1
            except Exception as e:
                print(f"[!] Skipped cookie {clean.get('name')!r}: {e}")

        print(f"[*] Injected {injected}/{len(cookies_list)} cookies.")

        # navigate AWAY from robots.txt to home feed and confirm IG bundle
        # actually rendered. If proxy is dead/slow this raises with a clear
        # message instead of letting the action handler grind for minutes on
        # selectors that can't match.
        print("[*] Navigating to home feed to confirm logged-in session...")
        try:
            self.page.get("https://www.instagram.com/", timeout=25)
        except Exception as exc:
            raise RuntimeError(
                f"home feed navigation failed (proxy too slow / dead?): "
                f"{type(exc).__name__}: {exc}"
            ) from exc
        if not self._wait_for_ig_home_ready(timeout_s=20):
            raise RuntimeError(
                "Instagram home feed did NOT render after cookie injection. "
                "Common causes (in order of likelihood): (1) proxy is too slow "
                "and the JS bundle never finished loading, (2) cookies are "
                "expired or for a different account, (3) IG returned a "
                "checkpoint/challenge page. Task aborted to avoid running "
                "actions against a blank page."
            )
        print("[*] Home feed ready, proceeding to commands.")

    def _wait_for_ig_home_ready(self, *, timeout_s: float = 20.0) -> bool:
        """Return True once the IG home DOM has the main left-rail nav.

        We look for a generic anchor in the side navigation — IG rotates the
        exact icons and labels but the <nav> with role-based links is stable.
        Polls every 500ms until found or timeout.
        """
        deadline = time.monotonic() + max(1.0, timeout_s)
        # Multiple selectors in order — IG has shipped both old (<nav>) and
        # new (<div role="navigation">) shells. We accept ANY match.
        candidates = (
            'xpath://nav//a[@href="/"]',
            'xpath://div[@role="navigation"]//a[@href="/"]',
            'xpath://a[@href="/"][.//svg[@aria-label]]',
        )
        while time.monotonic() < deadline:
            for sel in candidates:
                try:
                    el = self.page.ele(sel, timeout=1)
                    if el:
                        return True
                except Exception:
                    pass
            time.sleep(0.5)
        return False

    def close(self) -> None:
        """Close the browser, kill the local proxy forwarder, clean temp dirs."""
        print("[*] Shutting down browser...")
        page = getattr(self, "page", None)
        if page is not None:
            try:
                page.quit()
            except Exception as e:
                print(f"[!] Error closing browser: {e}")
            finally:
                # drop the ref so a later close() call is cheap
                self.page = None

        # Tear down the local pproxy auth forwarder. SIGTERM first, give it
        # half a second, then SIGKILL. We do NOT want a parade of orphaned
        # pproxy subprocesses on the host.
        proc = getattr(self, "_local_proxy_proc", None)
        port = getattr(self, "_local_proxy_port", None)
        if proc is not None:
            print(f"[*] Stopping local proxy forwarder (port={port}, pid={proc.pid})")
            try:
                proc.terminate()
                try:
                    proc.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=1.0)
            except Exception as e:
                print(f"[!] Error stopping local proxy forwarder: {e}")
            finally:
                self._local_proxy_proc = None
                self._local_proxy_port = None

        plugin_path = getattr(self, "plugin_path", "")
        if plugin_path:
            print(f"[*] Cleaning up proxy extension directory: {plugin_path}")
            try:
                if os.path.exists(plugin_path):
                    shutil.rmtree(plugin_path)
                    print(f"[*] Removed {plugin_path}")
            except Exception as e:
                print(f"[!] Error cleaning proxy directory: {e}")
            finally:
                self.plugin_path = ""

    # context manager
    # callers that use `with InstagramBrowser(...) as browser:` get clean
    # shutdown even on exceptions, which closes the last "user forgot to
    # call close()" gap that left stale runtime_proxy_plugin_* folders
    # piling up on disk.
    def __enter__(self) -> "InstagramBrowser":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            self.close()
        except Exception as cleanup_exc:
            # do not let cleanup hide the real exception that triggered
            # __exit__. log the cleanup failure and move on.
            print(f"[!] InstagramBrowser.__exit__ cleanup raised: {cleanup_exc}")

    # class-level janitor
    @staticmethod
    def cleanup_orphaned_extensions(
        root: str = ".",
        *,
        older_than_seconds: float = 0.0,
    ) -> int:
        """Delete dp_proxy_ext_* and runtime_proxy_plugin_* folders left behind by crashed runs.

        Args:
            root: directory to scan.
            older_than_seconds: only delete folders older than this.

        Returns:
            int: how many folders were removed.
        """
        import time as _time

        _prefixes = ("dp_proxy_ext_", "runtime_proxy_plugin_")
        removed = 0
        try:
            entries = os.listdir(root)
        except FileNotFoundError:
            return 0
        cutoff = _time.time() - max(0.0, older_than_seconds)
        for name in entries:
            if not any(name.startswith(p) for p in _prefixes):
                continue
            path = os.path.join(root, name)
            if not os.path.isdir(path):
                continue
            try:
                if older_than_seconds > 0 and os.path.getmtime(path) > cutoff:
                    continue
                shutil.rmtree(path)
                removed += 1
                print(f"[*] cleanup_orphaned_extensions: removed {path}")
            except Exception as exc:
                print(
                    f"[!] cleanup_orphaned_extensions: could not remove {path}: {exc}"
                )
        return removed
