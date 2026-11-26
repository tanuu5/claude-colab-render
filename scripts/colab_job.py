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
    workers    Blenders that render at the same time, each a chunk of its own (default 1).  For a scene whose frames
               wait on the CPU (heavy scene evaluation, Python that places things every frame) rather than on the
               GPU: one GPU then feeds several Blenders.  Each needs its own share of the GPU's and the machine's
               memory; the summary's gpu_util_mean, gpu_mem_peak_mb, ram_peak_gb and load_mean tell whether there is
               room (too many for the machine's memory and Colab ends the runtime without a word)
    ram_limit  (with workers > 1; default 0.85) the share of the machine's memory in use at which the youngest
               Blender is ended and the job goes on with one Blender fewer; its chunk is rendered again when there
               is room, and it does not count as a failed try.  Linux only (/proc/meminfo)
    echo       (optional) a regular expression: the lines of a finished chunk's log that match it are printed
               (the last 8), e.g. the render script's own timing lines
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
progress.txt (one line per chunk, with the time left), logs/<first>_<last>.log, summary.json, watch.txt (a line a
minute while chunks render: load, memory in use, the GPU's utilisation and memory).
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
import threading
import time
from concurrent.futures import ThreadPoolExecutor

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


def run_chunk(a, job, s, e, work, log, running=None):
    """running: a dict that holds (the Blender's process, when it started) under (s, e) while it runs."""
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
        proc = subprocess.Popen(cmd, cwd=a.root, env=env, stdout=fh, stderr=subprocess.STDOUT)
        if running is not None:
            running[(s, e)] = (proc, t0)
        try:
            rc = proc.wait(timeout=limit)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            rc = f"timeout after {limit} s"
        finally:
            if running is not None:
                running.pop((s, e), None)
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


def echo(path, pattern, n=8):
    """The last lines of a chunk's log that match the job's "echo"."""
    with open(path, encoding="utf-8", errors="replace") as fh:
        return [l.rstrip() for l in fh if re.search(pattern, l)][-n:]


def ram_used():
    """(GB in use, GB in all) from /proc/meminfo (Linux); None elsewhere."""
    try:
        kb = {}
        with open("/proc/meminfo") as fh:
            for line in fh:
                k, v = line.split(":", 1)
                kb[k] = float(v.split()[0])
        return (kb["MemTotal"] - kb["MemAvailable"]) / 1e6, kb["MemTotal"] / 1e6
    except (OSError, KeyError, ValueError, IndexError):
        return None


def watch(done, got, every=10.0, trace=None, on_ram=None):
    """While the chunks render: how busy the machine and the GPU are (the load average, the machine's memory in use;
    with nvidia-smi the GPU's utilisation and memory).  It tells a scene that waits on the CPU from one that waits on
    the GPU, and whether another Blender would fit.  trace: a file that gets a line every minute (in OUT_DIR, so that
    after a runtime that died there is something to read: what was in use shortly before).  on_ram(GB in use, GB in
    all): called with every reading of the memory (main()'s guard)."""
    smi = shutil.which("nvidia-smi")
    k = 0
    while not done.wait(every):
        k += 1
        line = time.strftime("%Y-%m-%d %H:%M:%S")
        try:
            load = os.getloadavg()[0]
            got["load"] += load
            got["n"] += 1
            line += f" load {load:.1f}"
        except OSError:
            pass
        r = ram_used()
        if r:
            got["ram"] = max(got.get("ram", 0.0), r[0])
            got["ram_total"] = r[1]
            line += f" ram {r[0]:.1f}/{r[1]:.1f} GB"
            if on_ram:
                try:
                    on_ram(*r)
                except Exception as ex:                      # the watcher must outlive its guard
                    print(f"(memory guard: {ex})", flush=True)
        if smi:
            try:
                out = subprocess.run([smi, "--query-gpu=utilization.gpu,memory.used", "--format=csv,noheader,nounits"],
                                     capture_output=True, text=True, timeout=20).stdout
                u, m = [float(x) for x in out.splitlines()[0].split(",")]
                got["gpu_n"] += 1
                got["util"] += u
                got["mem"] = max(got["mem"], m)
                line += f" gpu {u:.0f}% {m:.0f} MB"
            except Exception:
                pass
        if trace and k % max(1, round(60 / every)) == 0:
            try:
                with open(trace, "a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
            except OSError:
                pass


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
    workers = max(1, int(job.get("workers", 1)))
    left = sum(e - s + 1 for s, e in todo if not os.path.exists(os.path.join(state, f"done_{s}_{e}")))
    say(f"START {job.get('title', '')}: frames {job['frames'][0]}-{job['frames'][1]} in {len(todo)} chunk(s); "
        f"{left} frame(s) to render" + (f", {workers} Blenders at a time" if workers > 1 else ""), progress)
    st = {"t": 0.0, "f": 0, "left": left, "device": "", "fail": "", "shed": 0}
    lock = threading.Lock()
    t_wall = time.time()
    got = {"n": 0, "load": 0.0, "gpu_n": 0, "util": 0.0, "mem": 0.0}
    running, shed = {}, set()
    slots = {"max": workers, "busy": 0, "quiet": 0.0}
    room = threading.Condition()
    ram_limit = float(job.get("ram_limit", 0.85))

    def guard(used, total):
        """The machine's memory is nearly full while several Blenders run: end the youngest one and go on with one
        Blender fewer (its chunk is rendered again when there is room).  Colab ends a runtime whose memory is full
        without a word, and everything in flight is lost."""
        if used < ram_limit * total or time.time() < slots["quiet"]:
            return
        with room:
            now = list(running.items())
            if len(now) < 2:
                return
            key, (proc, _) = max(now, key=lambda kv: kv[1][1])
            shed.add(key)
            slots["max"] = max(1, len(now) - 1)
            slots["quiet"] = time.time() + 60
            st["shed"] += 1
            proc.kill()
        with lock:
            say(f"MEMORY {used:.1f} of {total:.1f} GB in use with {len(now)} Blenders: ended {key[0]}-{key[1]} "
                f"(it is rendered again later), {slots['max']} at a time from here", progress)

    over = threading.Event()
    threading.Thread(target=watch, args=(over, got),
                     kwargs={"trace": os.path.join(state, "watch.txt"), "on_ram": guard}, daemon=True).start()

    def extra():
        x = {}
        if workers > 1:
            x.update(workers=workers, wall_seconds=round(time.time() - t_wall, 1),
                     s_per_frame_each=round(st["t"] / st["f"], 2) if st["f"] else None)
        if got["n"]:
            x["load_mean"] = round(got["load"] / got["n"], 1)
        if got["gpu_n"]:
            x.update(gpu_util_mean=round(got["util"] / got["gpu_n"]), gpu_mem_peak_mb=round(got["mem"]))
        if got.get("ram"):
            x.update(ram_peak_gb=round(got["ram"], 1), ram_total_gb=round(got["ram_total"], 1))
        if st["shed"]:
            x.update(memory_guard=st["shed"], workers_at_end=slots["max"])
        return x

    def one(s, e):
        """Render a chunk (twice if the first try fails), copy what it wrote to OUT_DIR, mark it done."""
        if st["fail"]:
            return
        wdir = work if workers == 1 else os.path.join(work, f"{s}_{e}")
        log = os.path.join(logdir, f"{s}_{e}.log")
        attempt = 0
        while True:
            with room:                                   # a place among the Blenders (the guard may have taken one away)
                while slots["busy"] >= slots["max"]:
                    room.wait()
                slots["busy"] += 1
            try:
                if st["fail"]:
                    return
                made, secs, problem = run_chunk(a, job, s, e, wdir, log, running)
            finally:
                with room:
                    slots["busy"] -= 1
                    room.notify_all()
            shutil.copyfile(log, os.path.join(state, "logs", f"{s}_{e}.log"))
            if (s, e) in shed:
                shed.discard((s, e))
                if problem:                              # ended by the guard: not a failed try
                    continue
            attempt += 1
            if not problem:
                break
            with lock:
                say(f"FAIL {s}-{e} (try {attempt}): {problem}", progress)
                print(tail(log), flush=True)
            if attempt == 2:
                break
        if problem:
            with lock:
                st["fail"] = st["fail"] or f"{s}-{e}: {problem}"
            return
        for f in made:
            dest = os.path.join(a.out, os.path.relpath(f, wdir))
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            shutil.copyfile(f, dest)
        with open(os.path.join(state, f"done_{s}_{e}"), "w", encoding="utf-8") as fh:
            fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {secs:.1f} s {len(made)} file(s)\n")
        with lock:
            if not st["device"]:
                if job.get("engine", "cycles") == "eevee":   # gpu_pick.py names the Cycles device; EEVEE draws with OpenGL
                    st["device"] = "OpenGL (EEVEE)"
                else:
                    m = re.search(r"GPU_PICK device ([^\n]+)", open(log, encoding="utf-8", errors="replace").read())
                    st["device"] = m.group(1).strip() if m else ""
            n = e - s + 1
            st["t"], st["f"], st["left"] = st["t"] + secs, st["f"] + n, st["left"] - n
            rate = (time.time() - t_wall if workers > 1 else st["t"]) / st["f"]
            say(f"done {s}-{e}: {n} frame(s) in {hms(secs)} ({secs / n:.2f} s a frame with Blender's start), "
                f"{st['left']} left, about {hms(st['left'] * rate)} to go", progress)
            if job.get("echo"):
                for line in echo(log, job["echo"]):
                    print("  | " + line, flush=True)

    skipped, rest = 0, []
    for s, e in todo:
        if os.path.exists(os.path.join(state, f"done_{s}_{e}")):
            skipped += 1
            say(f"skip {s}-{e} (done before)")
        else:
            rest.append((s, e))
    if workers == 1:
        for s, e in rest:
            one(s, e)
            if st["fail"]:
                break
    else:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for r in [pool.submit(one, s, e) for s, e in rest]:
                r.result()
    over.set()
    t_run, f_run, device = st["t"], st["f"], st["device"]
    if st["fail"]:
        summary(state, job, todo, skipped, t_run, f_run, device, "failed", st["fail"], extra=extra())
        sys.exit(1)
    if workers > 1:
        wall = time.time() - t_wall
        say(f"ALL DONE: {f_run} frame(s) rendered now, {skipped} chunk(s) done before; "
            f"{(wall / f_run if f_run else 0):.2f} s a frame with {workers} Blenders at a time "
            f"({(t_run / f_run if f_run else 0):.2f} s a frame in each); output in {a.out}", progress)
    else:
        say(f"ALL DONE: {f_run} frame(s) rendered now, {skipped} chunk(s) done before; "
            f"{(t_run / f_run if f_run else 0):.2f} s a frame; output in {a.out}", progress)
    video, problem = make_video(a, job, progress)
    if problem:
        say(f"VIDEO not made: {problem}", progress)
    summary(state, job, todo, skipped, t_run, f_run, device, "failed" if problem else "done",
            f"video: {problem}" if problem else "", video, extra=extra())
    if problem:
        sys.exit(1)


def summary(state, job, todo, skipped, t_run, f_run, device, status, error, video=None, extra=None):
    """seconds_now and s_per_frame: of the Blenders' own times, one after another; with several workers of the
    wall clock (extra["wall_seconds"]), and s_per_frame_each is what a frame took inside one Blender."""
    extra = extra or {}
    secs = extra.get("wall_seconds", t_run)
    with open(os.path.join(state, "summary.json"), "w", encoding="utf-8") as fh:
        json.dump({"title": job.get("title", ""), "status": status, "error": error,
                   "engine": job.get("engine", "cycles"), "frames": job["frames"],
                   "chunks": len(todo), "skipped_chunks": skipped, "frames_rendered_now": f_run,
                   "seconds_now": round(secs, 1), "s_per_frame": round(secs / f_run, 2) if f_run else None,
                   "device": device, "video": video, "finished": time.strftime("%Y-%m-%d %H:%M:%S"),
                   **{k: v for k, v in extra.items() if k != "wall_seconds"}},
                  fh, ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
