# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 たぬ
"""Can this .blend be rendered on another machine (Google Colab) as it is?  Lists everything it reads from outside
the file, flags what is missing, absolute (it will not exist there) or kept on disk, and says whether it needs `-y`
(Python drivers, or text blocks that run on load).  With --pack it packs what can be packed, makes the rest relative
and saves a copy.

    blender -b scene.blend -P blend_check.py
    blender -b scene.blend -P blend_check.py -- --pack --save bundle/scene.blend

Prints one line per item (`CHECK <kind> <state> <name> <path>`) and a last line `CHECK result: ...`.  Exit code 0
when nothing needs attention, 1 when something does.  A relative file that cannot be packed (an image sequence, a
movie, a volume, an Alembic/USD cache) is fine as long as it travels in the bundle at the same relative place.
"""
import argparse
import os
import sys

import bpy


def args():
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    ap = argparse.ArgumentParser()
    ap.add_argument("--pack", action="store_true")
    ap.add_argument("--save", default="")
    return ap.parse_args(argv)


def items():
    """(kind, name, path, packed, can_pack) for every datablock that reads a file."""
    for im in bpy.data.images:
        if im.source in ("FILE", "SEQUENCE", "MOVIE", "TILED") and im.filepath:
            yield "image", im.name, im.filepath, bool(im.packed_file), im.source in ("FILE", "TILED")
    for f in bpy.data.fonts:
        if f.filepath and f.filepath != "<builtin>":
            yield "font", f.name, f.filepath, bool(f.packed_file), True
    for s in bpy.data.sounds:
        yield "sound", s.name, s.filepath, bool(s.packed_file), True
    for v in bpy.data.volumes:
        yield "volume", v.name, v.filepath, bool(getattr(v, "packed_file", None)), False
    for c in bpy.data.cache_files:
        yield "cache", c.name, c.filepath, False, False
    for m in bpy.data.movieclips:
        yield "clip", m.name, m.filepath, False, False
    for lib in bpy.data.libraries:
        yield "library", lib.name, lib.filepath, bool(lib.packed_file), True


def disk_caches():
    """Simulation caches that live in a folder beside the .blend instead of inside it."""
    for ob in bpy.data.objects:
        for md in ob.modifiers:
            if md.type == "FLUID" and md.fluid_type == "DOMAIN":
                yield f"{ob.name}/{md.name}", md.domain_settings.cache_directory
                continue
            caches = []
            if hasattr(md, "point_cache"):
                caches.append(md.point_cache)
            if md.type == "PARTICLE_SYSTEM":
                caches.append(md.particle_system.point_cache)
            if any(pc.use_disk_cache for pc in caches):
                yield f"{ob.name}/{md.name}", "//blendcache_<file name>/"


def check():
    bad = 0
    for kind, name, path, packed, can_pack in items():
        if packed:
            state = "packed"
        elif not os.path.exists(bpy.path.abspath(path)):
            state, bad = "MISSING", bad + 1
        elif not path.startswith("//"):
            state, bad = "ABSOLUTE", bad + 1
        else:
            state = "relative" if can_pack else "relative-not-packable(bring-it-along)"
        print(f"CHECK {kind} {state} {name!r} {path!r}", flush=True)
    for name, folder in disk_caches():
        print(f"CHECK diskcache ON-DISK {name!r} {folder!r} (bring the folder along, or bake into the file)", flush=True)
        bad += 1
    py_drivers = 0
    for coll in (bpy.data.objects, bpy.data.materials, bpy.data.shape_keys, bpy.data.node_groups, bpy.data.scenes,
                 bpy.data.meshes, bpy.data.armatures, bpy.data.cameras, bpy.data.lights, bpy.data.worlds):
        for idblock in coll:
            ad = getattr(idblock, "animation_data", None)
            for fc in (ad.drivers if ad else []):
                if fc.driver.type == "SCRIPTED" and not fc.driver.is_simple_expression:
                    py_drivers += 1
    on_load = [t.name for t in bpy.data.texts if t.use_module]
    need_y = py_drivers or on_load
    print(f"CHECK result: {bad} item(s) need attention; {py_drivers} Python driver(s), {len(on_load)} text block(s) "
          f"run on load -> {'run Blender with -y' if need_y else 'no -y needed'}; "
          f"output path in the file {bpy.context.scene.render.filepath!r} (set your own with -o or the script)",
          flush=True)
    return bad


def main():
    a = args()
    bad = check()
    if a.pack:
        for op, label in ((bpy.ops.file.pack_all, "pack all"), (bpy.ops.file.pack_libraries, "pack libraries"),
                          (bpy.ops.file.make_paths_relative, "make paths relative")):
            try:
                op()
                print(f"CHECK {label}: done", flush=True)
            except Exception as e:                  # e.g. nothing to pack
                print(f"CHECK {label}: {e}", flush=True)
        bad = check()
    if a.save:
        path = os.path.abspath(a.save)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        bpy.ops.wm.save_as_mainfile(filepath=path, copy=True, compress=True)
        print(f"CHECK saved a copy: {path}", flush=True)
    sys.exit(1 if bad else 0)


main()
