import re
import os

def clean_file(filepath):
    with open(filepath, 'r', encoding='utf-8') as f:
        content = f.read()
    
    # turn lines like "# ── URLs & tunables ────────"
    # into "# --- URLs & tunables ---"
    # also lines like "# ── CRITICAL: resolve SVG..."
    # become "# --- CRITICAL: resolve SVG... ---"

    # match lines that start with optional whitespace, then #, then some
    # number of ─ or ═ chars. trailing chars may be missing.

    def repl(m):
        text = m.group(1).strip()
        # drop trailing box-drawing chars if there are any
        text = re.sub(r'[─═]+$', '', text).strip()
        if text:
            return f"{m.group(0).split('#')[0]}# --- {text} ---"
        else:
            return f"{m.group(0).split('#')[0]}# ---------------------------------------------------------"

    new_content = re.sub(r'^[ \t]*#[ \t]*[─═]+[ \t]*(.*?)[ \t]*$', repl, content, flags=re.MULTILINE)
    
    if new_content != content:
        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(new_content)
        print(f"Cleaned {filepath}")

target_dir = '/home/sk8ver/Documents/Projects/CRM/IG_CRM/backend/workers'
for root, _, files in os.walk(target_dir):
    for file in files:
        if file.endswith('.py'):
            clean_file(os.path.join(root, file))
