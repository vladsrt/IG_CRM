"""
Warmup Action Script
--------------------
Demonstrates integrating the OOP browser instance to perform a simple behavior
like scrolling the Instagram feed to generate realistic activity.
"""

import time
import random
import sys
import os

# Add backend directory to module search path so `workers` is resolvable
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '../../')))

from workers.core.browser_core import InstagramBrowser

# ─── CONFIGURATION ──────────────────────────────────────────────────────────

PROXY = "8d1f77cde74f6dffffea__cr.us:80fe1a46ee235b27@gw.dataimpulse.com:823"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)

INSTAGRAM_COOKIES = [
    {"domain": ".instagram.com", "name": "ps_n",       "value": "1",                                                                                                                                "path": "/"},
    {"domain": ".instagram.com", "name": "datr",       "value": "4PnXaAUhBF6H4VaGhlf1j1g0",                                                                                                        "path": "/"},
    {"domain": ".instagram.com", "name": "ds_user_id", "value": "77203602829",                                                                                                                      "path": "/"},
    {"domain": ".instagram.com", "name": "csrftoken",  "value": "30jG0bFQ9XloSZWb1TK7BaoYs83jmkzU",                                                                                               "path": "/"},
    {"domain": ".instagram.com", "name": "mid",        "value": "aNf54AAEAAGwnGqXcIkj68TtKnh4",                                                                                                    "path": "/"},
    {"domain": ".instagram.com", "name": "sessionid",  "value": "77203602829%3ABV1b0aNRX4sWwg%3A12%3AAYhp6okkj6yOVAozVcwhIbkgh3bDMnloQ1mlUSz6Xzk", "path": "/"},
    {"domain": ".instagram.com", "name": "ps_l",       "value": "1",                                                                                                                                "path": "/"},
    {"domain": ".instagram.com", "name": "dpr",        "value": "1",                                                                                                                                "path": "/"},
    {"domain": ".instagram.com", "name": "rur",        "value": '"NHA\\05477203602829\\0541808322467:01fe0b0984dcba8b6cc3f7dfa5743dd979aa75cc0cfba7d1c44eef3d301cffab39339cb7"', "path": "/"},
]

def run_warmup():
    """
    Executes a warmup sequence using the OOP InstagramBrowser.
    """
    print("=" * 55)
    print("  Instagram Worker - Action: Warmup Feed")
    print("=" * 55)

    browser = None
    try:
        # 1. Initialize OOP Browser
        browser = InstagramBrowser(proxy_string=PROXY, user_agent=USER_AGENT, headless=False)

        # 2. Inject Cookies
        browser.inject_cookies(INSTAGRAM_COOKIES)

        # 3. Navigate to Main Feed
        print("[*] Navigating to Instagram home feed...")
        browser.page.get("https://www.instagram.com/")
        
        # Wait for page load
        time.sleep(5)

        # 4. Perform Warmup: Random Scrolling
        scroll_steps = random.randint(2, 4)
        print(f"[*] Performing warmup: Scrolling {scroll_steps} times.")
        
        for i in range(scroll_steps):
            # Scroll down by random amount
            scroll_amount = random.randint(300, 800)
            print(f"    - Scroll step {i+1}/{scroll_steps}: down {scroll_amount}px")
            browser.page.scroll.down(scroll_amount)
            
            # Pause randomly like a human reading
            sleep_time = random.uniform(2.5, 6.0)
            time.sleep(sleep_time)

        print("[+] Warmup completed successfully. ✅")

    except Exception as e:
        print(f"[!] Critical Error during Warmup: {e}")
        import traceback
        traceback.print_exc()

    finally:
        # 5. Guaranteed Teardown (Crucial for preventing leaks)
        print("\n[*] Commencing teardown...")
        if browser:
            browser.close()
        print("=" * 55)

if __name__ == "__main__":
    run_warmup()
