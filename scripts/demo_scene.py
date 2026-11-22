# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 たぬ
"""Build a small sample scene and save it as a .blend, so the whole flow (render in chunks onto Drive, then an MP4)
can be tried without a scene of one's own.  Everything is made here: a grid of subdivided monkeys in five
materials turning slowly, a glossy floor, a sun and eight coloured point lights with shadows, and a camera that
circles the scene.

    blender -b --factory-startup -P demo_scene.py -- --out demo.blend [--engine eevee|cycles] [--frames 120]
            [--fps 30] [--res 1920x1080] [--samples 32] [--grid 10]

The .blend keeps the Subdivision modifier (not applied), so the file stays small; render it as an animation:
    blender -b demo.blend -P gpu_pick.py -o out/f_#### -s 1 -e 120 -a
"""
import argparse
import math
import os
import sys

import bpy
from mathutils import Vector


def args():
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--engine", default="eevee", choices=("eevee", "cycles"))
    ap.add_argument("--frames", type=int, default=120)
    ap.add_argument("--fps", type=int, default=30)
    ap.add_argument("--res", default="1920x1080")
    ap.add_argument("--samples", type=int, default=32)
    ap.add_argument("--grid", type=int, default=10)
    return ap.parse_args(argv)


def material(name, rgb, rough, metal=0.0):
    m = bpy.data.materials.new(name)
    m.use_nodes = True
    p = next(n for n in m.node_tree.nodes if n.type == "BSDF_PRINCIPLED")
    p.inputs["Base Color"].default_value = (*rgb, 1.0)
    p.inputs["Roughness"].default_value = rough
    p.inputs["Metallic"].default_value = metal
    return m


def build(a):
    for ob in list(bpy.data.objects):
        bpy.data.objects.remove(ob, do_unlink=True)
    sc = bpy.context.scene
    sc.frame_start, sc.frame_end = 1, a.frames
    sc.render.fps = a.fps
    bpy.ops.mesh.primitive_monkey_add(size=0.8)
    src = bpy.context.active_object
    me = src.data
    bpy.data.objects.remove(src, do_unlink=True)
    mats = [material(f"m{i}", c, r, mt) for i, (c, r, mt) in enumerate((
        ((0.8, 0.2, 0.15), 0.35, 0.0), ((0.15, 0.5, 0.8), 0.2, 0.0), ((0.9, 0.75, 0.3), 0.25, 1.0),
        ((0.2, 0.7, 0.3), 0.6, 0.0), ((0.85, 0.85, 0.85), 0.1, 0.0)))]
    me.materials.append(mats[0])               # one slot on the shared mesh; each object fills it with its own
    n, half = a.grid, (a.grid - 1) / 2
    for i in range(n):
        for j in range(n):
            ob = bpy.data.objects.new(f"monkey_{i}_{j}", me)
            ob.location = ((i - half) * 1.6, (j - half) * 1.6, 0.55)
            sc.collection.objects.link(ob)
            mod = ob.modifiers.new("sub", "SUBSURF")
            mod.levels = mod.render_levels = 2
            ob.material_slots[0].link = "OBJECT"
            ob.material_slots[0].material = mats[(i + j) % len(mats)]
            start = math.radians((i * 37 + j * 53) % 360)
            for f, turn in ((1, 0.0), (a.frames, math.radians(90 if (i + j) % 2 else -90))):
                ob.rotation_euler = (0.0, 0.0, start + turn)
                ob.keyframe_insert("rotation_euler", index=2, frame=f)
    bpy.ops.mesh.primitive_plane_add(size=400.0)    # wide enough that its edge stays out of the picture
    bpy.context.active_object.data.materials.append(material("floor", (0.3, 0.3, 0.32), 0.15))
    sun = bpy.data.objects.new("sun", bpy.data.lights.new("sun", "SUN"))
    sun.data.energy, sun.data.angle = 3.0, math.radians(2.0)
    sun.rotation_euler = (math.radians(50), 0.0, math.radians(30))
    sc.collection.objects.link(sun)
    for k in range(8):
        t = k / 8.0 * 2.0 * math.pi
        lamp = bpy.data.objects.new(f"pt{k}", bpy.data.lights.new(f"pt{k}", "POINT"))
        lamp.data.energy, lamp.data.shadow_soft_size = 400.0, 0.3
        lamp.data.color = (1.0, 0.6 + 0.4 * (k % 2), 0.5 + 0.5 * ((k + 1) % 2))
        lamp.location = (7.0 * math.cos(t), 7.0 * math.sin(t), 3.0)
        sc.collection.objects.link(lamp)
    cam = bpy.data.objects.new("cam", bpy.data.cameras.new("cam"))
    sc.collection.objects.link(cam)
    sc.camera = cam
    cam.data.lens = 30.0
    steps = 12
    target = Vector((0.0, 0.0, 0.6))
    for s in range(steps + 1):                 # a quarter turn round the scene, keyed every few frames
        u = s / steps
        f = 1 + round(u * (a.frames - 1))
        t = math.radians(-20 + 90 * u)
        cam.location = (13.0 * math.cos(t), 13.0 * math.sin(t), 4.8 - 1.4 * u)
        cam.rotation_euler = (target - cam.location).to_track_quat("-Z", "Y").to_euler()
        cam.keyframe_insert("location", frame=f)
        cam.keyframe_insert("rotation_euler", frame=f)
    if sc.world is None:
        sc.world = bpy.data.worlds.new("world")
    sc.world.use_nodes = True
    bg = next(n for n in sc.world.node_tree.nodes if n.type == "BACKGROUND")
    bg.inputs["Color"].default_value = (0.05, 0.06, 0.09, 1.0)
    r = sc.render
    w, h = (int(v) for v in a.res.split("x"))
    r.resolution_x, r.resolution_y, r.resolution_percentage = w, h, 100
    r.image_settings.file_format = "PNG"
    r.image_settings.color_mode = "RGB"
    r.use_stamp_filename = False
    r.use_stamp_hostname = False
    if a.engine == "eevee":
        r.engine = "BLENDER_EEVEE"
        e = sc.eevee
        e.taa_render_samples = a.samples
        for attr, val in (("use_raytracing", True), ("use_shadows", True), ("shadow_pool_size", "1024")):
            try:                                # (the default 512 MB shadow pool overflowed: "Shadow buffer full")
                setattr(e, attr, val)
            except Exception as ex:
                print("DEMO note:", attr, ex)
    else:
        r.engine = "CYCLES"
        c = sc.cycles
        c.device = "GPU"                        # gpu_pick.py chooses which GPU when it renders
        c.samples = a.samples
        c.use_adaptive_sampling = True
        c.adaptive_threshold = 0.02
        c.use_denoising = True
        c.denoising_use_gpu = True
    return n * n


def main():
    a = args()
    count = build(a)
    out = os.path.abspath(a.out)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    bpy.ops.wm.save_as_mainfile(filepath=out, compress=True)
    print(f"DEMO saved {out}: {count} monkeys, {a.engine}, {a.frames} frames at {a.fps} fps, {a.res}, "
          f"{a.samples} samples", flush=True)


main()
