# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 たぬ
"""Make Cycles render on this machine's GPU, whatever the scripts after this one set: Metal on a Mac, OptiX (else CUDA)
on Linux with NVIDIA, such as Google Colab.  Put it before a project's own scripts:

    blender -b scene.blend -P gpu_pick.py -P render.py -- ...
    blender -b --factory-startup -P gpu_pick.py          (alone: only prints the GPU Cycles would use)

Why: scripts written on a Mac often set `prefs.compute_device_type = "METAL"`.  On Linux that raises TypeError, and a
script that catches it falls back to the CPU without a word (a 1080p Cycles frame then takes minutes, not seconds).
This script picks the GPU at once, and again right before every render (render_init / render_pre handlers, kept
across file loads), so a later script's fallback is undone at render time.  EEVEE needs nothing from here: it draws
with the GPU through OpenGL (EGL when there is no screen).

Prints `GPU_PICK device <kind>: <names>` once, and `GPU_PICK fixed: ...` when it had to undo a CPU fallback (then the
denoiser goes to the GPU too, as the script would have set for a GPU).
"""
import sys

import bpy

KINDS = ("METAL",) if sys.platform == "darwin" else ("OPTIX", "CUDA", "HIP", "ONEAPI")
_state = {"kind": None, "said": False}


def _say(line):
    if not _state["said"]:
        print(line, flush=True)
        _state["said"] = True


def pick():
    """Choose the first kind of GPU that has a device here; return the kind, or None (CPU only)."""
    prefs = bpy.context.preferences.addons["cycles"].preferences
    for kind in KINDS:
        try:
            prefs.compute_device_type = kind
        except TypeError:                      # a kind this build or platform does not know
            continue
        prefs.get_devices()
        gpus = [d for d in prefs.devices if d.type == kind]
        if not gpus:
            continue
        for d in prefs.devices:
            d.use = d.type == kind
        _state["kind"] = kind
        _say(f"GPU_PICK device {kind}: {', '.join(d.name for d in gpus)}")
        return kind
    _state["kind"] = None
    _say("GPU_PICK device CPU (no GPU that Cycles can use)")
    return None


@bpy.app.handlers.persistent
def _before_render(scene, *_):
    if scene.render.engine != "CYCLES":
        return
    prefs = bpy.context.preferences.addons["cycles"].preferences
    kind = _state["kind"] if prefs.compute_device_type == _state["kind"] else pick()
    if kind and scene.cycles.device != "GPU":
        scene.cycles.device = "GPU"
        scene.cycles.denoising_use_gpu = True
        print(f"GPU_PICK fixed: scene '{scene.name}' was set to render on the CPU; rendering on {kind}", flush=True)


pick()
for _h in (bpy.app.handlers.render_init, bpy.app.handlers.render_pre):
    if _before_render not in _h:
        _h.append(_before_render)
