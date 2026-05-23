#!/usr/bin/env python3
"""Исследование: найти КОНКРЕТНО кнопку Next после загрузки файла.
Кликаем Create → Post → загружаем файл → смотрим что на странице."""
import json, os, sys, time

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.join(_HERE, "backend")
sys.path.insert(0, _BACKEND)

from workers.core.browser_core import InstagramBrowser
from workers.core.behavior import dismiss_instagram_modals

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

FILE = "/home/sk8ver/Documents/Projects/CRM/IG_CRM/media/test_ig__uniq_991a7748.MP4"

def js(page, code):
    try: return (page.run_js(code) or "").strip()
    except: return ""

b = InstagramBrowser(proxy_string=None, user_agent=UA, headless=False, user_data_dir="fresh")
b.inject_cookies(COOKIES)
page = b.page
time.sleep(1)

# Feed
page.get("https://www.instagram.com/")
time.sleep(8)
dismiss_instagram_modals(page, per_selector_timeout_s=0.5, max_dismissals=3)
time.sleep(2)

# Create → Post
from workers.core.behavior import safe_coordinate_click
safe_coordinate_click(page, 'css:svg[aria-label="New post"]', timeout=6)
time.sleep(2)
post_span = page.ele('xpath://span[normalize-space()="Post"]', timeout=3)
post_span.click(by_js=True)
time.sleep(4)

# Upload file
page.set.upload_files(FILE)
sel = page.ele('xpath://button[normalize-space()="Select from computer"]', timeout=5)
sel.click(by_js=True)
time.sleep(10)

# Handle post-upload modals
dismiss_instagram_modals(page, per_selector_timeout_s=0.5, max_dismissals=3)
time.sleep(2)

print("=== AFTER FILE UPLOAD — looking for Next buttons ===")

# DUMP: find ALL elements with "Next" text
all_next = js(page, """
var all=document.querySelectorAll('*');
var result=[];
for(var e of all){
    var txt=(e.textContent||'').trim();
    if(txt==='Next'){
        var r=e.getBoundingClientRect();
        result.push({
            tag:e.tagName,
            role:e.getAttribute('role')||'',
            classes:(e.className||'').toString().substring(0,100),
            parentTag:e.parentElement?.tagName||'',
            x:Math.round(r.x),y:Math.round(r.y),w:Math.round(r.width),h:Math.round(r.height),
            visible:r.width>0&&r.height>0,
        });
    }
}
return JSON.stringify(result.slice(0,10));
""")
print(f"  All 'Next' elements: {all_next[:600] if all_next else 'none'}")

# DUMP: all buttons in the dialog/modal
all_btns = js(page, """
var all=document.querySelectorAll('button,[role="button"]');
var result=[];
for(var e of all){
    var txt=(e.textContent||'').trim();
    var r=e.getBoundingClientRect();
    if(txt&&r.width>0&&r.y>100&&r.y<window.innerHeight-100){
        result.push({text:txt.substring(0,40),tag:e.tagName,x:Math.round(r.x),y:Math.round(r.y)});
    }
}
return JSON.stringify(result.slice(0,20));
""")
print(f"  All visible buttons: {all_btns[:600] if all_btns else 'none'}")

# DUMP: headings
all_h = js(page, """
var all=document.querySelectorAll('[role="heading"],h1,h2,h3');
var result=[];
for(var e of all){
    result.push({text:e.textContent?.trim()?.substring(0,60)||'',tag:e.tagName});
}
return JSON.stringify(result);
""")
print(f"  Headings: {all_h[:400] if all_h else 'none'}")

# Try clicking Next by TEXT (not div selector)
print("\n=== Trying to click Next button ===")
# Find via DrissionPage
for sel in ['t:button@text()=Next', 't:div@text()=Next', 'xpath://button[normalize-space()="Next"]', 'xpath://div[@role="button"][normalize-space()="Next"]']:
    try:
        el = page.ele(sel, timeout=2)
        if el:
            x, y = el.rect.midpoint
            print(f"  FOUND: {sel} at ({x},{y})")
            page.actions.move_to((x, y))
            time.sleep(0.3)
            page.actions.click()
            print(f"  CLICKED")
            time.sleep(3)
            break
    except Exception as e:
        print(f"  {sel}: {e}")

# Check what appeared
time.sleep(2)
r = js(page, "var d=document.querySelector('div[aria-label=\"Write a caption...\"]'); return d?'FOUND':'NOT FOUND'")
print(f"  Caption editor: {r}")

# Handle Discard if popped up
discard = page.ele('xpath://button[normalize-space()="Discard"]', timeout=1) if False else None
try: discard = page.ele('xpath://button[normalize-space()="Discard"]', timeout=2)
except: discard = None
if discard:
    print("  Discard modal present!")
else:
    print("  No Discard modal")

all_btns2 = js(page, """
var all=document.querySelectorAll('button,[role="button"]');
var result=[];
for(var e of all){
    var txt=(e.textContent||'').trim();
    var r=e.getBoundingClientRect();
    if(txt&&r.width>0&&r.y>100&&r.y<window.innerHeight-100){
        result.push({text:txt.substring(0,40),tag:e.tagName,x:Math.round(r.x),y:Math.round(r.y)});
    }
}
return JSON.stringify(result.slice(0,20));
""")
print(f"  Buttons after Next click: {all_btns2[:400] if all_btns2 else 'none'}")

b.close()
