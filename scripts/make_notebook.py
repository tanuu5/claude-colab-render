# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 たぬ
"""Write the Colab notebook for one render job: it takes the scene's zip from Google Drive, renders the frames in
chunks onto Drive (colab_job.py), carries on where an earlier run stopped, and gives the runtime back at the end.
gpu_pick.py, colab_job.py and the job are written into the notebook, so the notebook is the only file to upload.

    python3 make_notebook.py job.json --out render_job.ipynb

job.json: colab_job.py's fields (command, frames, chunk, engine, expect_per_frame, timeout_per_frame, env, workers,
ram_limit, echo) and:
    title          shown at the top             blender   version to install (default 5.1.2)
    drive_dir      folder in My Drive (default colab_render)   bundle   the zip in it   workdir  folder in the zip
    build          instead of a bundle: a Blender script (path from the job file) that makes the scene on Colab;
                   it is written into the notebook and run with build_args before the first chunk
    out            output folder: My Drive/<drive_dir>/out/<out>/
    video          (optional) an MP4 of the frames once all are done, made on Colab (see colab_job.py)
    accept_gpus    GPU names to accept (part of the name, e.g. ["L4", "T4"]); any other is given back at once
                   (Colab may hand out another GPU than the one asked for).  Empty or left out: any
    release_at_end give the runtime back when done or stuck (default true)
    note           a line for the person who runs it (optional)
"""
import argparse
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))


def md(text):
    return {"cell_type": "markdown", "metadata": {}, "source": text.strip("\n").splitlines(keepends=True)}


def code(text):
    return {"cell_type": "code", "metadata": {}, "execution_count": None, "outputs": [],
            "source": text.strip("\n").splitlines(keepends=True)}


def notebook(job, base):
    job.setdefault("blender", "5.1.2")
    job.setdefault("drive_dir", "colab_render")
    job.setdefault("release_at_end", True)
    gpu_pick = open(os.path.join(HERE, "gpu_pick.py"), encoding="utf-8").read()
    runner = open(os.path.join(HERE, "colab_job.py"), encoding="utf-8").read()
    build = None
    if job.get("build"):
        build = open(os.path.normpath(os.path.join(base, job["build"])), encoding="utf-8").read()
    scene_row = (f"ノートブックの中でコードで組み立てる（`{os.path.basename(job['build'])}`。Drive に置く物は無い）"
                 if build else f"マイドライブ / `{job['drive_dir']}` / `{job['bundle']}`")
    step1 = ("1. Drive に置く物はありません（場面はノートブックの中で組み立てます）。描いたコマは、"
             f"Google Drive の `マイドライブ/{job['drive_dir']}/out/` に書き出します。" if build else
             f"1. 場面一式の zip を、Google Drive の `マイドライブ/{job['drive_dir']}/` に置く"
             "（ブラウザの Drive に、ドラッグで上げられます）。")
    first, last = job["frames"]
    n_chunks = -(-(last - first + 1) // int(job.get("chunk", 100)))
    accept = "、".join(job.get("accept_gpus") or []) or "どれでも"
    v = job.get("video")
    workers = max(1, int(job.get("workers", 1)))
    side = f"、Blender を {workers} 個並べて" if workers > 1 else ""
    video_row = (f"描き終えたら、書き出し先に `{v.get('name') or job['out'] + '.mp4'}` を作る（{v.get('fps', 60)}fps）"
                 if v else "作らない（連番だけ）")
    head = f"""
# {job.get('title', 'Blender の描画')}

Colab の GPU で描いて、Google Drive に書き出すノートブック（colab-render で作った）。

| | |
|---|---|
| 場面一式 | {scene_row} |
| 描くコマ | {first}〜{last}（{last - first + 1} コマを、{job.get('chunk', 100)} コマずつ {n_chunks} 回に分けて{side}） |
| 書き出し先 | マイドライブ / `{job['drive_dir']}` / `out` / `{job['out']}` |
| 動画 | {video_row} |
| 使う GPU | {accept} |
| Blender | {job['blender']}（{job.get('engine', 'cycles')}） |

**使い方**
{step1}
2. このノートブックを開いて、「ランタイム」→「ランタイムのタイプを変更」で GPU（ふだんは **L4**）を選ぶ。
3. 「ランタイム」→「すべてのセルを実行」。2 のセルで Google Drive へのアクセスを聞かれるので、許可する。**ここだけは、その場にいる必要があります。**
4. あとは離れてかまいません（ブラウザのタブは開いたまま、Mac はスリープしないように）。進み具合は、Drive の `out/{job['out']}/_colab_render/progress.txt` でも見られます。
5. 描き終わると（または先へ進めなくなると）、ランタイムは自分で手放されます。描いたコマは Drive に残ります。
6. 途中で切れたら、同じノートブックをもう一度「すべてのセルを実行」すると、描き終えた区切りは飛ばして、続きから描きます。
""" + (f"\n{job['note']}\n" if job.get("note") else "")
    cells = [
        md(head),
        md("## 1. この実行環境（GPU・CPU・メモリ）"),
        code("JOB = " + repr(job) + r'''
import os, re, json, glob, shutil, subprocess, time
from google.colab import runtime

def sh(cmd, check=False):
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    out = (r.stdout + r.stderr).strip()
    if check and r.returncode != 0:
        raise RuntimeError(out[-3000:])
    return out

def stop(msg, fail=True):
    """Print why, write everything out to Drive, give the runtime back (release_at_end), and stop a failed run."""
    print(msg)
    try:
        from google.colab import drive
        if os.path.ismount("/content/drive"):
            drive.flush_and_unmount()          # the last files reach Drive before the runtime goes
    except Exception as e:
        print("Drive:", e)
    if JOB.get("release_at_end", True):
        print("ランタイムを手放します。")
        runtime.unassign()
    if fail:
        raise SystemExit(msg)

info = {}
q = sh("nvidia-smi --query-gpu=name,driver_version,memory.total --format=csv,noheader")
if not q or "failed" in q.lower() or "not found" in q.lower():
    stop("GPU が見えません。ランタイムのタイプで GPU を選んでください。\n" + q)
info["gpu"], info["driver"], info["vram"] = [s.strip() for s in q.splitlines()[0].split(",")]
info["cpus"] = sh("nproc")
info["ram"] = sh("free -h | awk '/Mem/ {print $2}'")
for k, v in info.items():
    print(f"{k:8s} {v}")
ACCEPT = JOB.get("accept_gpus") or []
if ACCEPT and not any(a.lower() in info["gpu"].lower() for a in ACCEPT):
    stop(f"この GPU（{info['gpu']}）は、この仕事では使わない設定です（accept_gpus: {ACCEPT}）。")
'''),
        md("## 2. Google Drive をつなぐ（アクセスの許可を聞かれます）"),
        code(r'''
from google.colab import drive
try:
    drive.mount("/content/drive")
except Exception as e:
    stop(f"Google Drive をつなげませんでした: {e}")
BASE = f"/content/drive/MyDrive/{JOB['drive_dir']}"
OUT = f"{BASE}/out/{JOB['out']}"
if JOB.get("build"):
    print("場面一式: ノートブックの中で組み立てる")
else:
    BUNDLE = f"{BASE}/{JOB['bundle']}"
    if not os.path.exists(BUNDLE):
        stop(f"Drive に {JOB['drive_dir']}/{JOB['bundle']} が見つかりません。置いてから、もう一度実行してください。")
    print(f"場面一式: {BUNDLE}（{os.path.getsize(BUNDLE) / 1e6:.0f} MB）")
os.makedirs(OUT, exist_ok=True)
done = len(glob.glob(f"{OUT}/_colab_render/done_*"))
print(f"書き出し先: {OUT}" + (f"（前の回に描き終えた区切り: {done}）" if done else ""))
'''),
        md("## 3. Blender（Linux 版）と、動かすのに要る部品を入れる"),
        code(r'''
V = JOB["blender"]
BL = f"/content/blender-{V}-linux-x64/blender"
if not os.path.exists(BL):
    mm = ".".join(V.split(".")[:2])
    sh(f"cd /content && wget -q https://download.blender.org/release/Blender{mm}/blender-{V}-linux-x64.tar.xz"
       f" && tar -xf blender-{V}-linux-x64.tar.xz && rm blender-{V}-linux-x64.tar.xz", check=True)
sh("apt-get -qq update && DEBIAN_FRONTEND=noninteractive apt-get -qq install -y libxi6 libxxf86vm1 libxfixes3 "
   "libxrender1 libxkbcommon0 libsm6 libice6 libgl1 libegl1 libglvnd0 > /dev/null", check=True)
print(sh(f"{BL} --version | head -1"))
'''),
        md("## 4. NVIDIA の部品を、ドライバーと同じ版で足す（無い物だけ。2026-11 に試した T4・L4・A100 では、はじめから入っていた）"),
        code(r'''
D = info["driver"]
dst, host = "/usr/lib/x86_64-linux-gnu", "/usr/lib64-nvidia"
WANT = ("libEGL_nvidia", "libGLX_nvidia", "libnvidia-eglcore", "libnvidia-glcore", "libnvidia-glsi", "libnvidia-tls",
        "libnvidia-glvkspirv", "libnvidia-gpucomp", "libnvoptix", "libnvidia-rtcore", "libnvidia-ptxjitcompiler")
have = lambda n: os.path.exists(f"{dst}/{n}") or os.path.exists(f"{host}/{n}")
NV = "はじめから入っていた"
if not (have(f"libEGL_nvidia.so.{D}") and have(f"libnvoptix.so.{D}")):
    run = f"NVIDIA-Linux-x86_64-{D}.run"
    got = None
    for url in (f"https://us.download.nvidia.com/tesla/{D}/{run}",
                f"https://download.nvidia.com/XFree86/Linux-x86_64/{D}/{run}"):
        if sh(f"cd /content && wget -q -O {run} {url} && echo OK").endswith("OK"):
            got = url
            break
    if not got:
        NV = f"ドライバー {D} の配布ファイルが見つからない"
    else:
        sh(f"cd /content && rm -rf nv && sh {run} --extract-only --target nv > /dev/null", check=True)
        put = [n for n in sorted(os.listdir("/content/nv")) if n.endswith("." + D) and n.startswith(WANT) and not have(n)]
        for n in put:
            sh(f"cp /content/nv/{n} {dst}/")
        if os.path.exists("/content/nv/nvoptix.bin") and not os.path.exists("/usr/share/nvidia/nvoptix.bin"):
            os.makedirs("/usr/share/nvidia", exist_ok=True)
            sh("cp /content/nv/nvoptix.bin /usr/share/nvidia/")
        if os.path.exists("/content/nv/10_nvidia.json"):
            os.makedirs("/usr/share/glvnd/egl_vendor.d", exist_ok=True)
            sh("cp -n /content/nv/10_nvidia.json /usr/share/glvnd/egl_vendor.d/")
        NV = "足した: " + (", ".join(n.split(".so")[0] for n in put) or "なし")
for base, so in (("libEGL_nvidia.so", "0"), ("libGLX_nvidia.so", "0"), ("libnvoptix.so", "1"),
                 ("libnvidia-ptxjitcompiler.so", "1")):
    if os.path.exists(f"{dst}/{base}.{D}") and not have(f"{base}.{so}"):
        sh(f"ln -sf {base}.{D} {dst}/{base}.{so}")
J = "/usr/share/glvnd/egl_vendor.d/10_nvidia.json"
if not os.path.exists(J):
    os.makedirs(os.path.dirname(J), exist_ok=True)
    open(J, "w").write('{"file_format_version": "1.0.0", "ICD": {"library_path": "libEGL_nvidia.so.0"}}')
sh("ldconfig")
os.environ["__EGL_VENDOR_LIBRARY_FILENAMES"] = J
print("NVIDIA の部品:", NV)
'''),
        md("## 5. 場面一式を開いて、GPU で描けるかを確かめる"),
        code("GPU_PICK = " + repr(gpu_pick) + "\nRUNNER = " + repr(runner) + "\nBUILD = " + repr(build) + r'''
open("/content/gpu_pick.py", "w").write(GPU_PICK)
open("/content/colab_job.py", "w").write(RUNNER)
json.dump(JOB, open("/content/job.json", "w"), ensure_ascii=False)
sh("rm -rf /content/job && mkdir -p /content/job", check=True)
if BUILD:                                   # the scene is made by code in the next cell, before the first chunk
    open("/content/job/build.py", "w").write(BUILD)
    print("場面を組み立てるスクリプトを置いた: /content/job/build.py")
else:
    shutil.copyfile(BUNDLE, "/content/bundle.zip")
    sh("cd /content/job && unzip -q /content/bundle.zip", check=True)
ROOT = os.path.join("/content/job", JOB.get("workdir", ""))
print("場面一式の場所:", ROOT)
r = subprocess.run(["python3", "/content/colab_job.py", "--job", "/content/job.json", "--blender", BL,
                    "--gpu-pick", "/content/gpu_pick.py", "--check"], capture_output=True, text=True)
print(r.stdout + r.stderr)
if r.returncode != 0:
    stop("GPU で描けないので、描かずに止めます（上の CHECK の行を Claude に見せてください）。")
'''),
        md("## 6. 描く（区切りごとに Drive へ写す。終わったら、ランタイムを手放す）"),
        code(r'''
cmd = ["python3", "/content/colab_job.py", "--job", "/content/job.json", "--blender", BL, "--gpu-pick",
       "/content/gpu_pick.py", "--root", ROOT, "--out", OUT, "--work", "/content/chunk"]
if BUILD:
    cmd += ["--build-script", "/content/job/build.py"]
p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
try:
    for line in p.stdout:
        print(line, end="", flush=True)
    rc = p.wait()
except KeyboardInterrupt:          # stopped by hand: stop Blender, keep the runtime (the person is here)
    p.terminate()
    raise
summary = {}
try:
    summary = json.load(open(f"{OUT}/_colab_render/summary.json"))
except Exception as e:
    print("summary:", e)
print("\nまとめ: " + json.dumps({"gpu": info["gpu"], "cpus": info["cpus"], **summary}, ensure_ascii=False))
if rc == 0:
    stop("描き終えました。", fail=False)
else:
    stop(f"途中で止まりました（{summary.get('error', rc)}）。ログは Drive の _colab_render/logs/ にあります。")
'''),
    ]
    return {"cells": cells, "metadata": {"accelerator": "GPU", "colab": {"provenance": []},
                                         "kernelspec": {"display_name": "Python 3", "name": "python3"},
                                         "language_info": {"name": "python"}},
            "nbformat": 4, "nbformat_minor": 0}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("job")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    job = json.load(open(a.job, encoding="utf-8"))
    for k in ("out", "command", "frames"):
        if k not in job:
            raise SystemExit(f"job.json needs '{k}'")
    if not job.get("bundle") and not job.get("build"):
        raise SystemExit("job.json needs 'bundle' (a zip on Drive) or 'build' (a script that makes the scene)")
    nb = notebook(job, os.path.dirname(os.path.abspath(a.job)))
    os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
    with open(a.out, "w", encoding="utf-8") as fh:
        json.dump(nb, fh, ensure_ascii=False, indent=1)
    print("written", a.out, len(nb["cells"]), "cells")


if __name__ == "__main__":
    main()
