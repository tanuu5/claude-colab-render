# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 たぬ
"""Render a frame range in chunks, one Blender per chunk; copy each finished chunk to an output folder (Google Drive
on Colab) and mark it done, so that a run that stopped (a dropped runtime, the session limit) carries on where it
stopped.  Standard library only: Colab runs it with its own Python, and it runs the same on a Mac (to try a job before
it goes to Colab, or to render the Mac's share of a job).

    python3 colab_job.py --job job.json --blender BL --root BUNDLE_DIR --out OUT_DIR [--gpu-pick gpu_pick.py]
    python3 colab_job.py --job job.json --blender BL --check [--gpu-pick gpu_pick.py]
        (--check only: can Cycles / EEVEE draw with the GPU here?  Exit 0 if the job's engine can)

job.json (an example: assets/job_example.json):
    command    Blender's arguments, with {GPU} (gpu_pick.py), {START} and {END} (the chunk's first and last frame),
               {OUT} (an empty folder for this chunk: everything written under it is copied to OUT_DIR, sub-folders
               kept) and {ROOT} (the bundle's folder; Blender runs there).  Write {{ and }} for a literal brace.
    frames     [first, last]          chunk  frames a Blender renders (default 100)
    engine     cycles | eevee | both  (what --check asks for)
    expect_per_frame   files a frame writes under {OUT} (default 1; 0 = do not count)
    timeout_per_frame  seconds (default 300; a chunk gets 600 + frames x this)
    env        extra environment variables for Blender (values may use {ROOT})
    build_args (with --build-script) the arguments of the script that makes the scene in ROOT before the first
               chunk (for a scene made by code instead of a bundle; values may use {ROOT})
    video      (optional) make an MP4 of the frames once every chunk is done, with ffmpeg:
               {"pattern": "f_%04d.png" (under OUT_DIR), "fps": 60, "name": "<file>.mp4" (into OUT_DIR),
                "start": first frame (default frames[0]), "crf": 15, "preset": "slow", "refs": 4, "level": "4.2",
                "audio": a file in the bundle (optional)}.  Colour tags: BT.709 primaries and matrix with the sRGB
               transfer curve, so colour-managed players on macOS show it like the PNG frames (a bt709 transfer tag
               makes them 6-7/255 lighter).  Skipped, with a line saying so, when a frame of the range is missing.

    python3 colab_job.py --job job.json --root BUNDLE_DIR --out OUT_DIR --video-only    (the MP4 of what is there)

State lives in OUT_DIR/_colab_render/: done_<first>_<last> for each finished chunk (it is skipped next time),
progress.txt (one line per chunk, with the time left), logs/<first>_<last>.log, summary.json.
Exit 0 when every chunk is done (and the MP4, if asked for, is made), 1 when a chunk failed twice or the MP4 could
not be made, 2 when the GPU check failed.
"""
import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time

EEVEE_PROBE = ("import bpy, gpu; sc = bpy.context.scene; sc.render.engine = 'BLENDER_EEVEE'; "
               "sc.render.resolution_percentage = 10; sc.eevee.taa_render_samples = 1; "
               "sc.render.filepath = {path!r}; bpy.ops.render.render(write_still=True); "
               "print('EEVEE_PROBE', gpu.platform.vendor_get(), '|', gpu.platform.renderer_get(), '|', "
               "gpu.platform.version_get())")


def say(line, progress=None):
    print(line, flush=True)
    if progress:
        with open(progress, "a", encoding="utf-8") as fh:
            fh.write(time.strftime("%Y-%m-%d %H:%M:%S ") + line + "\n")


def hms(s):
    s = int(round(s))
    return f"{s // 3600}:{s // 60 % 60:02d}:{s % 60:02d}"


def check(a, job):
    """Can the engine(s) of the job draw with the GPU on this machine?"""
    engine = job.get("engine", "cycles")
    ok = True
    if engine in ("cycles", "both"):
        out = subprocess.run([a.blender, "-b", "--factory-startup", "-P", a.gpu_pick],
                             capture_output=True, text=True, timeout=600).stdout
        m = re.search(r"GPU_PICK device ([^\n]+)", out)
        dev = m.group(1).strip() if m else "?"
        good = bool(m) and not dev.startswith("CPU")
        say(f"CHECK cycles: {dev} -> {'GPU' if good else 'NOT ON THE GPU'}")
        ok &= good
    if engine in ("eevee", "both"):
        tmp = tempfile.mkdtemp(prefix="eevee_probe_")
        expr = EEVEE_PROBE.format(path=os.path.join(tmp, "probe.png"))
        out = subprocess.run([a.blender, "-b", "--factory-startup", "--python-expr", expr],
                             capture_output=True, text=True, timeout=600).stdout
        m = re.search(r"EEVEE_PROBE ([^\n]+)", out)
        dev = m.group(1).strip() if m else "?"
        good = bool(m) and not any(w in dev.lower() for w in ("llvmpipe", "softpipe", "software"))
        say(f"CHECK eevee: {dev} -> {'GPU' if good else 'NOT ON THE GPU'}")
        ok &= good
    return ok


def chunks(job):
    first, last = job["frames"]
    n = int(job.get("chunk", 100))
    return [(s, min(s + n - 1, last)) for s in range(first, last + 1, n)]


def files_under(folder):
    return [os.path.join(d, f) for d, _, fs in os.walk(folder) for f in fs]


def run_chunk(a, job, s, e, work, log):
    shutil.rmtree(work, ignore_errors=True)
    os.makedirs(work)
    fill = {"GPU": a.gpu_pick, "START": s, "END": e, "OUT": work, "ROOT": a.root}
    cmd = [a.blender] + shlex.split(job["command"].format(**fill))
    env = dict(os.environ)
    env.update({k: str(v).format(ROOT=a.root) for k, v in job.get("env", {}).items()})
    limit = 600 + (e - s + 1) * int(job.get("timeout_per_frame", 300))
    t0 = time.time()
    with open(log, "w", encoding="utf-8") as fh:
        fh.write("$ " + shlex.join(cmd) + "\n")
        fh.flush()
        try:
            rc = subprocess.run(cmd, cwd=a.root, env=env, stdout=fh, stderr=subprocess.STDOUT, timeout=limit).returncode
        except subprocess.TimeoutExpired:
            rc = f"timeout after {limit} s"
    secs = time.time() - t0
    made = files_under(work)
    want = (e - s + 1) * int(job.get("expect_per_frame", 1))
    problem = None
    if rc != 0:
        problem = f"Blender ended with {rc}"
    elif len(made) < want:
        problem = f"{len(made)} file(s) written, {want} expected"
    return made, secs, problem


def tail(path, n=25):
    with open(path, encoding="utf-8", errors="replace") as fh:
        return "".join(fh.readlines()[-n:])


def make_video(a, job, progress):
    """The MP4 of the job's frames in OUT_DIR (job["video"]); returns (path or None, problem or None)."""
    v = job.get("video")
    if not v:
        return None, None
    first, last = job["frames"]
    start = int(v.get("start", first))
    count = last - first + 1
    pattern = os.path.join(a.out, v["pattern"])
    missing = [i for i in range(start, start + count) if not os.path.exists(pattern % i)]
    if missing:
        return None, f"{len(missing)} frame(s) missing (first {pattern % missing[0]})"
    name = v.get("name") or (os.path.basename(os.path.normpath(a.out)) + ".mp4")
    tmp = os.path.join(tempfile.mkdtemp(prefix="colab_video_"), name)
    color = ["-vf", "scale=in_range=full:out_range=tv:out_color_matrix=bt709,format=yuv420p,"
                    "setparams=color_primaries=bt709:color_trc=iec61966-2-1:colorspace=bt709:range=tv",
             "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "iec61966-2-1"]
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-framerate", str(v.get("fps", 60)), "-start_number", str(start),
           "-i", pattern]
    if v.get("audio"):
        cmd += ["-i", os.path.join(a.root, v["audio"]), "-map", "0:v", "-map", "1:a", "-c:a", "aac", "-b:a", "192k",
                "-shortest"]
    cmd += ["-frames:v", str(count), "-c:v", "libx264", "-preset", str(v.get("preset", "slow")),
            "-crf", str(v.get("crf", 15)), "-refs", str(v.get("refs", 4))]
    if v.get("level", "4.2"):
        cmd += ["-level:v", str(v.get("level", "4.2"))]
    cmd += color + ["-movflags", "+faststart", tmp]
    say(f"VIDEO making {name} from {count} frame(s) at {v.get('fps', 60)} fps", progress)
    t0 = time.time()
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0 or not os.path.exists(tmp):
        return None, "ffmpeg: " + (r.stderr.strip()[-800:] or f"exit {r.returncode}")
    dest = os.path.join(a.out, name)
    shutil.copyfile(tmp, dest)
    say(f"VIDEO written {dest} ({os.path.getsize(dest) / 1e6:.1f} MB, {time.time() - t0:.0f} s)", progress)
    return dest, None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--job", required=True)
    ap.add_argument("--blender", default="", help="the Blender to run (not needed with --video-only)")
    ap.add_argument("--gpu-pick", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "gpu_pick.py"))
    ap.add_argument("--root", default=".")
    ap.add_argument("--out", default="")
    ap.add_argument("--work", default="", help="the folder a chunk renders into (default: a temporary one)")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--video-only", action="store_true", help="only make the MP4 of the frames already in --out")
    ap.add_argument("--build-script", default="", help="a Blender script that makes the scene in --root first")
    a = ap.parse_args()
    job = json.load(open(a.job, encoding="utf-8"))
    a.root = os.path.abspath(a.root)
    a.gpu_pick = os.path.abspath(a.gpu_pick)
    if not a.blender and not a.video_only:
        sys.exit("--blender is needed")
    if a.check:
        sys.exit(0 if check(a, job) else 2)
    if not a.out:
        sys.exit("--out is needed")
    state = os.path.join(a.out, "_colab_render")
    os.makedirs(os.path.join(state, "logs"), exist_ok=True)
    progress = os.path.join(state, "progress.txt")
    if a.video_only:
        path, problem = make_video(a, job, progress)
        if problem or not path:
            say(f"VIDEO not made: {problem or 'no video in the job'}", progress)
            sys.exit(1)
        return
    work = a.work or tempfile.mkdtemp(prefix="colab_chunk_")
    logdir = tempfile.mkdtemp(prefix="colab_logs_")
    if a.build_script:
        os.makedirs(a.root, exist_ok=True)
        cmd = [a.blender, "-b", "--factory-startup", "-P", os.path.abspath(a.build_script), "--"] + \
            shlex.split(str(job.get("build_args", "")).format(ROOT=a.root))
        r = subprocess.run(cmd, cwd=a.root, capture_output=True, text=True, timeout=1800)
        lines = [l for l in (r.stdout + r.stderr).splitlines() if l.strip()]
        if r.returncode != 0:
            say(f"BUILD failed (exit {r.returncode})", progress)
            print("\n".join(lines[-25:]), flush=True)
            sys.exit(1)
        said = [l for l in lines if re.search(r"\bsaved\b", l, re.I)]
        say(f"BUILD done: {(said or lines or [''])[-1].strip()}", progress)
    todo = chunks(job)
    left = sum(e - s + 1 for s, e in todo if not os.path.exists(os.path.join(state, f"done_{s}_{e}")))
    say(f"START {job.get('title', '')}: frames {job['frames'][0]}-{job['frames'][1]} in {len(todo)} chunk(s); "
        f"{left} frame(s) to render", progress)
    t_run, f_run, skipped, device = 0.0, 0, 0, ""
    for s, e in todo:
        mark = os.path.join(state, f"done_{s}_{e}")
        if os.path.exists(mark):
            skipped += 1
            say(f"skip {s}-{e} (done before)")
            continue
        log = os.path.join(logdir, f"{s}_{e}.log")
        for attempt in (1, 2):
            made, secs, problem = run_chunk(a, job, s, e, work, log)
            shutil.copyfile(log, os.path.join(state, "logs", f"{s}_{e}.log"))
            if not problem:
                break
            say(f"FAIL {s}-{e} (try {attempt}): {problem}", progress)
            print(tail(log), flush=True)
        if problem:
            summary(state, job, todo, skipped, t_run, f_run, device, "failed", f"{s}-{e}: {problem}")
            sys.exit(1)
        if not device:
            if job.get("engine", "cycles") == "eevee":   # gpu_pick.py names the Cycles device; EEVEE draws with OpenGL
                device = "OpenGL (EEVEE)"
            else:
                m = re.search(r"GPU_PICK device ([^\n]+)", open(log, encoding="utf-8", errors="replace").read())
                device = m.group(1).strip() if m else ""
        for f in made:
            dest = os.path.join(a.out, os.path.relpath(f, work))
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            shutil.copyfile(f, dest)
        with open(mark, "w", encoding="utf-8") as fh:
            fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {secs:.1f} s {len(made)} file(s)\n")
        n = e - s + 1
        t_run, f_run, left = t_run + secs, f_run + n, left - n
        rate = t_run / f_run
        say(f"done {s}-{e}: {n} frame(s) in {hms(secs)} ({secs / n:.2f} s a frame with Blender's start), "
            f"{left} left, about {hms(left * rate)} to go", progress)
    say(f"ALL DONE: {f_run} frame(s) rendered now, {skipped} chunk(s) done before; "
        f"{(t_run / f_run if f_run else 0):.2f} s a frame; output in {a.out}", progress)
    video, problem = make_video(a, job, progress)
    if problem:
        say(f"VIDEO not made: {problem}", progress)
    summary(state, job, todo, skipped, t_run, f_run, device, "failed" if problem else "done",
            f"video: {problem}" if problem else "", video)
    if problem:
        sys.exit(1)


def summary(state, job, todo, skipped, t_run, f_run, device, status, error, video=None):
    with open(os.path.join(state, "summary.json"), "w", encoding="utf-8") as fh:
        json.dump({"title": job.get("title", ""), "status": status, "error": error,
                   "engine": job.get("engine", "cycles"), "frames": job["frames"],
                   "chunks": len(todo), "skipped_chunks": skipped, "frames_rendered_now": f_run,
                   "seconds_now": round(t_run, 1), "s_per_frame": round(t_run / f_run, 2) if f_run else None,
                   "device": device, "video": video, "finished": time.strftime("%Y-%m-%d %H:%M:%S")},
                  fh, ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
