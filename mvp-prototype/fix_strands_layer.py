"""
Fix strands layer by iteratively finding and copying missing modules from venv.
Run: python fix_strands_layer.py
"""
import subprocess, sys, re, shutil
from pathlib import Path

LAYER = Path(r"c:\Users\prajw\OneDrive\Desktop\amazon nova\novaguard\mvp-prototype\lambda\strands_layer\python")
VENV  = Path(r"c:\Users\prajw\OneDrive\Desktop\amazon nova\.venv\Lib\site-packages")
PY    = sys.executable

def try_import():
    code = f"""
import sys, re
sys.path=[r'{LAYER}']+[p for p in sys.path if '.venv' not in p and 'site-packages' not in p]
try:
    from strands import Agent, tool
    print('SUCCESS')
except ModuleNotFoundError as e:
    m = re.search(r"No module named '([^']+)'", str(e))
    print('MISSING:' + (m.group(1) if m else str(e)))
except Exception as e:
    print('ERROR:' + str(e)[:150])
"""
    r = subprocess.run([PY, "-c", code], capture_output=True, text=True)
    out = (r.stdout + r.stderr).strip()
    for line in out.splitlines():
        if line.startswith(("SUCCESS","MISSING:","ERROR:")):
            return line
    return f"UNKNOWN: {out[:200]}"

def copy_pkg(name):
    # Try directory first
    d = VENV / name
    if d.exists():
        shutil.copytree(d, LAYER / name, dirs_exist_ok=True)
        print(f"  Copied dir: {name}")
        return True
    # Try glob
    matches = list(VENV.glob(f"{name}*"))
    if matches:
        for m in matches:
            if m.is_dir():
                shutil.copytree(m, LAYER / m.name, dirs_exist_ok=True)
            else:
                shutil.copy2(m, LAYER)
        print(f"  Copied (glob): {name}* -> {[m.name for m in matches]}")
        return True
    print(f"  NOT FOUND in venv: {name}")
    return False

for i in range(30):
    result = try_import()
    print(f"[{i}] {result}")
    if result.startswith("SUCCESS"):
        print("ALL DEPENDENCIES RESOLVED!")
        break
    elif result.startswith("MISSING:"):
        pkg = result[8:].split(".")[0].strip()
        copy_pkg(pkg)
    else:
        print("Non-import error:", result)
        break

total_mb = sum(f.stat().st_size for f in LAYER.rglob("*") if f.is_file()) / 1e6
print(f"\nLayer total size: {total_mb:.1f} MB")
