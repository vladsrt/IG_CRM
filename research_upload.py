#!/usr/bin/env python3
"""Research: upload flow — dump DOM at each step to find correct selectors."""
import json, os, sys, time

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.join(_HERE, "backend")
sys.path.insert(0, _BACKEND)

from workers.core.browser_core import InstagramBrowser
from workers.core.behavior import dismiss_instagram_modals, safe_coordinate_click

COOKIES = [
    {"name":"datr","value":"hsdvabaLFXims1PZzQrFtubo","domain":".instagram.com","path":"/"},
    {"name":"ds_user_id","value":"66998291245","domain":".instagram.com","path":"/"},
    {"name":"csrftoken","value":"sljjAJ4xxm6KXrD4r0enyuZCYGvnkuzQ","domain":".instagram.com","path":"/"},
    {"name":"ig_did","value":"F9841847-BA9C-405E-947B-9D2D9D937D2C","domain":".instagram.com","path":"/"},
    {"name":"wd","value":"1806x967","domain":".instagram.com","path":"/"},
    {"name":"mid","value":"aW_HhgAEAAG-A2g3tEgSI9ruSOL7","domain":".instagram.com","path":"/"},
    {"name":"sessionid","value":"66998291245%3Aok3FfItZNeROyP%3A14%3AAYhCKuKte0AEzJsE-Ez4aH8-2YY4cQqAKu3Er4V5Qg","domain":".instagram.com","path":"/"},
    {"name":"rur","value":'"LDC\\\\05466998291245\\\\0541809865019:***"',"domain":".instagram.com","path":"/"},
]
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36"

# Create tiny test mp4
TEST_FILE = "/tmp/test_upload.mp4"
with open(TEST_FILE, "wb") as f:
    f.write(b'\x00\x00\x00\x1cftypmp42\x00\x00\x00\x00mp42mp41' * 500)

b = InstagramBrowser(proxy_string=None, user_agent=UA, headless=False, user_data_dir="fresh")
b.inject_cookies(COOKIES)
page = b.page
time.sleep(1)

def dump_state(label):
    """Dump all headings, buttons, and dialog info."""
    r = page.run_js(f"""
    (()=>{{
        var headings=[];
        document.querySelectorAll('[role="heading"],h1,h2,h3,[aria-level]').forEach(h=>{{
            headings.push({{text:h.textContent?.trim()?.substring(0,80)||'',tag:h.tagName,role:h.getAttribute('role')||'',level:h.getAttribute('aria-level')||''}});
        }});
        var buttons=[];
        document.querySelectorAll('button,[role="button"]').forEach(b=>{{
            var txt=(b.textContent||'').trim().substring(0,50);
            if(txt) buttons.push({{text:txt,tag:b.tagName,role:b.getAttribute('role')||''}});
        }});
        var dialogs=document.querySelectorAll('div[role="dialog"]').length;
        var dialogSvgs=[];
        (document.querySelector('div[role="dialog"]')||{{querySelectorAll:()=>[]}}).querySelectorAll('svg[aria-label]').forEach(s=>{{
            dialogSvgs.push(s.getAttribute('aria-label')||'');
        }});
        return JSON.stringify({{label:'{label}',headings,buttons:buttons.slice(0,15),dialogs,dialogSvgs}});
    }})()
    """)
    print(f"\n=== {label} ===")
    if r:
        try:
            d = json.loads(r) if isinstance(r, str) else r
            print(f"  Headings: {[h['text'][:60] for h in d.get('headings',[]) if h['text']]}")
            print(f"  Buttons: {[b['text'][:40] for b in d.get('buttons',[])]}")
            print(f"  Dialogs: {d.get('dialogs',0)}")
            if d.get('dialogSvgs'): print(f"  Dialog SVGs: {d['dialogSvgs']}")
        except: print(f"  RAW: {r[:300]}")
    else: print("  EMPTY")

# 1. Feed
page.get("https://www.instagram.com/")
time.sleep(8)
dismiss_instagram_modals(page, per_selector_timeout_s=0.5, max_dismissals=3)
time.sleep(2)
dump_state("FEED")

# 2. Click Create → Post
print("\n=== Clicking Create ===")
safe_coordinate_click(page, 'css:svg[aria-label="New post"]', timeout=8)
time.sleep(2.5)
dump_state("AFTER_CREATE_CLICK")

print("\n=== Clicking Post ===")
post_span = page.ele('xpath://span[normalize-space()="Post"]', timeout=3)
if post_span:
    post_span.click(by_js=True)
    print("  Post clicked via JS")
else:
    print("  Post NOT FOUND!")
time.sleep(4)
dump_state("AFTER_POST_CLICK")

# 3. Arm file upload + click Select from computer
print("\n=== Uploading file ===")
page.set.upload_files(TEST_FILE)
sel = page.ele('xpath://button[normalize-space()="Select from computer"]', timeout=5)
if sel:
    sel.click(by_js=True)
    print("  Select from computer clicked")
else:
    print("  Select from computer NOT FOUND")
time.sleep(8)
dump_state("AFTER_FILE_UPLOAD")

# 4. Handle any modal that appears after upload
print("\n=== Handling post-upload modal ===")
dismiss_instagram_modals(page, per_selector_timeout_s=1.0, max_dismissals=3)
time.sleep(2)
dump_state("AFTER_MODAL_DISMISS")

# 5. Find crop button and try Next
print("\n=== Crop / Next screen ===")
safe_coordinate_click(page, 'css:svg[aria-label="Select crop"]', timeout=4)
time.sleep(1)
safe_coordinate_click(page, 't:span@text()=Original', timeout=3)
time.sleep(1)
dump_state("AFTER_CROP")

# Try clicking Next
print("\n=== Clicking Next ===")
safe_coordinate_click(page, 't:div@text()=Next', timeout=6)
time.sleep(4)
dump_state("AFTER_NEXT_CLICK")

b.close()
