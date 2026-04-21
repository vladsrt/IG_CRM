"""
Core Browser Engine
-------------------
Handles the instantiation of DrissionPage Chromium instances with proxies,
custom user agents, and performance tweaks. Manages session injection and clean teardown.
"""

import os
import shutil
import time
from typing import List, Dict

from DrissionPage import ChromiumPage, ChromiumOptions
from workers.utils.proxy_builder import create_proxy_extension


class InstagramBrowser:
    """
    Manages a Chromium browser instance customized for Instagram automation.
    Handles proxy extension generation and safe teardown to prevent leaks.
    """

    def __init__(self, proxy_string: str, user_agent: str, headless: bool = False):
        """
        Initializes the browser environment.
        
        Args:
            proxy_string (str): Proxy string in IP:PORT or USER:PASS@IP:PORT format.
            user_agent (str): User-Agent string to spoof.
            headless (bool): Whether to run the browser in headless mode.
        """
        self.proxy_string = proxy_string
        self.user_agent = user_agent
        self.plugin_folder = "runtime_proxy_plugin"
        
        # 1. Generate Proxy Extension
        self.plugin_path = create_proxy_extension(self.proxy_string, self.plugin_folder)
        print(f"[*] Proxy extension generated at {self.plugin_path}")
        
        # 2. Setup Options
        self.co = ChromiumOptions()
        self.co.add_extension(self.plugin_path)
        self.co.set_user_agent(self.user_agent)
        
        # Disable images to speed up page loading
        self.co.set_pref("profile.default_content_setting_values.images", 2)
        # Block browser notifications
        self.co.set_pref("profile.default_content_setting_values.notifications", 2)
        
        # Set headless preference
        self.co.headless(headless)
        
        # 3. Launch Page
        self.page = ChromiumPage(self.co)
        print("[*] Browser launched successfully.")

    def inject_cookies(self, cookies_list: List[Dict[str, str]]) -> None:
        """
        Injects a list of cookies into the browser context.
        Navigates to robots.txt first to ensure the domain context is correct.
        
        Args:
            cookies_list: A list of cookie dictionaries.
        """
        print("[*] Navigating to robots.txt to set domain context...")
        self.page.get("https://www.instagram.com/robots.txt")
        time.sleep(2)

        print("[*] Injecting session cookies...")
        for cookie in cookies_list:
            self.page.set.cookies(cookie)
            
        print(f"[*] Injected {len(cookies_list)} cookies.")
        time.sleep(2)

    def close(self) -> None:
        """
        Safely shuts down the browser and cleans up temporary files.
        Crucial for preventing memory leaks and orphaned proxy folders.
        """
        print("[*] Shutting down browser...")
        try:
            if hasattr(self, 'page') and self.page:
                self.page.quit()
        except Exception as e:
            print(f"[!] Error closing browser: {e}")
            
        print("[*] Cleaning up proxy extension directory...")
        try:
            if os.path.exists(self.plugin_path):
                shutil.rmtree(self.plugin_path)
                print(f"[*] Removed {self.plugin_path}")
        except Exception as e:
            print(f"[!] Error cleaning proxy directory: {e}")
