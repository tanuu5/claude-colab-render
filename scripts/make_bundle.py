# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 たぬ
"""Make the zip of a scene to render on Colab (it goes to Google Drive, then into the runtime), and warn about what
should not travel or will not work there.  Standard library only.

    python3 make_bundle.py --root DIR --top NAME --out bundle.zip PATH [PATH ...]

PATHs are files or folders under DIR; inside the zip they sit under NAME/ at the same relative places (so relative
paths in the scene and the scripts keep working).  Left out: __pycache__, *.pyc, .DS_Store, .git, .env and .env.*.
Warnings (the zip is still written): text files that mention this machine's user name, host name or home folder,
and .blend files with the home folder's path inside (an external file the runtime will not have: run
blend_check.py).
"""
import argparse
import getpass
import os
import re
import socket
import sys
import zipfile

SKIP_DIRS = {"__pycache__", ".git"}
TEXT = {".py", ".json", ".txt", ".md", ".sh", ".csv", ".yaml", ".yml", ".toml", ".glsl", ".osl", ".xml", ".html", ".js"}


def skip(name):
    return name == ".DS_Store" or name.endswith(".pyc") or name == ".env" or name.startswith(".env.")


def walk(root, rel):
    full = os.path.join(root, rel)
    if os.path.isfile(full):
        yield rel
        return
    for d, dirs, files in os.walk(full):
        dirs[:] = [x for x in dirs if x not in SKIP_DIRS]
        for f in files:
            if not skip(f):
                yield os.path.relpath(os.path.join(d, f), root)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("--top", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("paths", nargs="+")
    a = ap.parse_args()
    user, host = getpass.getuser(), socket.gethostname().split(".")[0]
    home = os.path.expanduser("~").rstrip("/") + "/"
    words = [w for w in {user, host, home} if w]
    pat = re.compile("|".join(re.escape(w) for w in words))
    files = sorted({p for rel in a.paths for p in walk(a.root, rel)})
    missing = [p for p in a.paths if not os.path.exists(os.path.join(a.root, p))]
    if missing:
        sys.exit("not found: " + ", ".join(missing))
    warn = 0
    for rel in files:
        full = os.path.join(a.root, rel)
        ext = os.path.splitext(rel)[1].lower()
        if ext in TEXT:
            hits = [i + 1 for i, line in enumerate(open(full, encoding="utf-8", errors="replace")) if pat.search(line)]
            if hits:
                warn += 1
                print(f"WARN {rel}: user/host name or the home folder on line(s) {hits[:8]}")
        elif ext == ".blend":
            data = open(full, "rb").read()
            n = data.count(home.encode())
            if n:
                warn += 1
                print(f"WARN {rel}: {n} path(s) in the home folder inside (external files? run blend_check.py)")
    os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
    with zipfile.ZipFile(a.out, "w", zipfile.ZIP_DEFLATED) as z:
        for rel in files:
            z.write(os.path.join(a.root, rel), os.path.join(a.top, rel))
    print(f"written {a.out}: {len(files)} file(s), {os.path.getsize(a.out) / 1e6:.1f} MB, {warn} warning(s)")


if __name__ == "__main__":
    main()
