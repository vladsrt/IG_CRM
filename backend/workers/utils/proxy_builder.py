"""
Proxy Builder Utility
---------------------
Generates an unzipped Chrome extension (Manifest V2) on the fly to support
authenticated HTTP/SOCKS proxies in DrissionPage without crashing.
"""

import os

def create_proxy_extension(proxy_string: str, extension_folder_name: str = "proxy_auth_plugin") -> str:
    """
    Parses proxy string and creates a Chrome extension (Manifest V2) in a raw folder
    to handle proxy authentication.
    
    Args:
        proxy_string (str): Proxy in format username:password@host:port or host:port.
        extension_folder_name (str): Directory name to store the generated extension.
        
    Returns:
        str: Path to the generated extension folder.
    """
    if '@' in proxy_string:
        credentials, server = proxy_string.split('@')
        proxy_user, proxy_pass = credentials.split(':')
        proxy_host, proxy_port = server.split(':')
    else:
        # Fallback if no auth
        proxy_host, proxy_port = proxy_string.split(':')
        proxy_user, proxy_pass = "", ""

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
    
    background_js = f"""
    var config = {{
            mode: "fixed_servers",
            rules: {{
              singleProxy: {{
                scheme: "http",
                host: "{proxy_host}",
                port: parseInt({proxy_port})
              }},
              bypassList: ["localhost"]
            }}
          }};
    chrome.proxy.settings.set({{value: config, scope: "regular"}}, function() {{}});
    function callbackFn(details) {{
        return {{
            authCredentials: {{
                username: "{proxy_user}",
                password: "{proxy_pass}"
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
    
    with open(os.path.join(extension_folder_name, "manifest.json"), "w", encoding="utf-8") as f:
        f.write(manifest_json.strip())
        
    with open(os.path.join(extension_folder_name, "background.js"), "w", encoding="utf-8") as f:
        f.write(background_js.strip())
    
    return extension_folder_name
