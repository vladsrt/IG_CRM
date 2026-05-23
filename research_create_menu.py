#!/usr/bin/env python3
"""Deep dive: walk up from "Post" text to find clickable parent."""
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

b = InstagramBrowser(proxy_string=None, user_agent=UA, headless=False, user_data_dir="fresh")
b.inject_cookies(COOKIES)
page = b.page
time.sleep(1)

page.get("https://www.instagram.com/")
time.sleep(8)
dismiss_instagram_modals(page, per_selector_timeout_s=0.5, max_dismissals=3)
time.sleep(2)

# Click Create
create_svg = page.ele('css:svg[aria-label="New post"]', timeout=3)
x, y = create_svg.rect.midpoint
print(f"Clicking Create at ({x},{y})")
page.actions.move_to((x, y))
time.sleep(0.5)
page.actions.click()
time.sleep(3)

# DEEP DIVE: walk UP from the "Post" text element to find clickable ancestor
r = page.run_js("""
(()=>{
    // Find ALL elements with exact text "Post"
    var all = document.querySelectorAll('*');
    var targets = [];
    for (var e of all) {
        var txt = (e.textContent||'').trim();
        if (txt === 'Post' && e.children.length <= 2) {
            targets.push(e);
        }
    }
    
    // Take the one that appeared at y~607 (the visible menu item)
    var target = null;
    for (var t of targets) {
        var r = t.getBoundingClientRect();
        if (r.y > 550 && r.y < 700) {
            target = t;
            break;
        }
    }
    if (!target) return 'no target found';
    
    // Walk up 6 levels
    var chain = [];
    var el = target;
    for (var i = 0; i < 8; i++) {
        if (!el) break;
        chain.push({
            level: i,
            tag: el.tagName,
            role: el.getAttribute('role')||'',
            id: el.getAttribute('id')||'',
            classes: (el.className||'').toString()?.substring(0,200)||'',
            href: el.getAttribute('href')||'',
            tabIndex: el.getAttribute('tabindex')||'',
            onClick: !!el.onclick,
            childCount: el.children.length,
            rect: (function(){
                var r=el.getBoundingClientRect();
                return {x:Math.round(r.x),y:Math.round(r.y),w:Math.round(r.width),h:Math.round(r.height)};
            })(),
        });
        el = el.parentElement;
    }
    return JSON.stringify({chain: chain});
})()
""")
print(f"\n=== Walk-up chain from 'Post' text ===")
if r:
    try:
        data = json.loads(r) if isinstance(r, str) else r
        for step in data.get("chain", []):
            print(f"  L{step['level']}: <{step['tag']}> role={step['role']} href={step['href']} tabindex={step['tabIndex']} pos=({step['rect']['x']},{step['rect']['y']}) size={step['rect']['w']}x{step['rect']['h']}")
    except: print(r[:500])
else: print("  EMPTY")

# Also check: is the Post item clickable via DrissionPage?
print("\n=== DrissionPage element search ===")
# Try to find the Post span
try:
    post_span = page.ele('xpath://span[normalize-space()="Post"]', timeout=2)
    print(f"  span Post found: tag={post_span.tag if post_span else 'None'}")
    if post_span:
        p = post_span.parent()
        print(f"  parent: tag={p.tag if p else 'None'} role={p.attr('role') if p else 'None'}")
        p2 = post_span.parent(2)
        print(f"  parent(2): tag={p2.tag if p2 else 'None'} role={p2.attr('role') if p2 else 'None'}")
        p3 = post_span.parent(3)
        print(f"  parent(3): tag={p3.tag if p3 else 'None'} role={p3.attr('role') if p3 else 'None'}")
except Exception as e:
    print(f"  Error: {e}")

# Try clicking Post via JS on the nearest clickable ancestor
r2 = page.run_js("""
(()=>{
    var all = document.querySelectorAll('*');
    var target = null;
    for (var e of all) {
        if ((e.textContent||'').trim() === 'Post' && e.children.length <= 2) {
            var r = e.getBoundingClientRect();
            if (r.y > 550 && r.y < 700) { target = e; break; }
        }
    }
    if (!target) return 'no target';
    
    // Walk up to find clickable: a, button, or element with onclick/role=link
    var clickable = target;
    for (var i = 0; i < 6; i++) {
        if (!clickable) break;
        var tag = clickable.tagName.toLowerCase();
        var role = clickable.getAttribute('role')||'';
        if (tag === 'a' || tag === 'button' || role === 'link' || role === 'button') {
            // Found it! Click and report
            clickable.click();
            return JSON.stringify({clicked: true, tag: tag, role: role, level: i});
        }
        clickable = clickable.parentElement;
    }
    
    // Last resort: click the original target directly
    target.click();
    return JSON.stringify({clicked: true, tag: target.tagName, level: 0, note: 'direct click'});
})()
""")
print(f"\n=== JS click result ===")
print(f"  {r2[:300] if r2 else 'EMPTY'}")

# Check if Create modal appeared
time.sleep(3)
r3 = page.run_js("""
var h = document.querySelector('[role=\"heading\"]');
var sc = document.evaluate('//button[normalize-space()=\"Select from computer\"]', document, null, XPathResult.FIRST_ORDERED_NODE_TYPE, null).singleNodeValue;
return JSON.stringify({
    heading: h ? h.textContent?.substring(0,40) : 'none',
    selectButton: sc ? 'found' : 'not found',
    dialogs: document.querySelectorAll('div[role=\"dialog\"]').length,
});
""")
print(f"  After Post click: {r3[:300]}")

b.close()
