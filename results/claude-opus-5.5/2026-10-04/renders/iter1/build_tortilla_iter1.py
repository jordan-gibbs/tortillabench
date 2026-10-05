"""
build_tortilla.py -- headless, fully procedural "folded flour tortilla on a cutting board".

Run (Blender 4.x, Cycles):

    blender --background --factory-startup --python build_tortilla.py
    blender --background --factory-startup --python build_tortilla.py -- --output-dir renders/final

Optional arguments after "--":
    --output-dir DIR    where outputs go (relative paths are relative to this script's folder)
    --width W --height H   output resolution (default 3840x2160)
    --samples N         max Cycles samples (adaptive sampling + denoising on top)
    --seed N            master seed for every procedural choice (default 7)
    --bake-only         build, simulate, shade and save the .blend, write diagnostics, skip rendering
    --no-steam          leave out the volumetric steam

Pipeline:
    1. empty scene -> procedural tortilla disc (ring-triangulated, irregular rim), cutting board,
       seamless backdrop and two out-of-focus props
    2. cloth simulation in four baked stages: fold 1 (pinned rim driven by an animated hook),
       release + settle, fold 2 (both layers), release + settle.  Each stage is baked with the
       point cache, its last frame is frozen into the mesh, and the next stage starts from there.
    3. post-sim modifiers (bubble displacement, solidify with thickness variation, subdivision)
    4. procedural node materials, food-photography light rig, procedural world, steam volume
    5. low camera with shallow depth of field focused on the leading fold, Cycles GPU render
Only Python's standard library and bpy / mathutils / bmesh are used.  No external assets.
"""

import argparse
import json
import math
import os
import platform
import random
import sys
import time
import traceback

import bmesh
import bpy
from mathutils import Euler, Matrix, Vector, noise
from mathutils.kdtree import KDTree

T_START = time.time()
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))


def log(msg):
    print(f"[tortilla {time.time() - T_START:7.1f}s] {msg}", flush=True)


# ----------------------------------------------------------------------------------------------
# configuration
# ----------------------------------------------------------------------------------------------

TORTILLA_RADIUS = 0.110        # 22 cm diameter
EDGE_LEN = 0.0033              # cloth mesh edge length
THICKNESS = 0.0017             # solidify thickness (scaled 0.88..1.0 by a vertex group)
BOARD_SIZE = (0.48, 0.32, 0.022)
FLOOR_Z = -BOARD_SIZE[2]

CLOTH = dict(
    quality=12,
    mass=0.003,                # per vertex; only the stiffness/mass ratios matter
    air_damping=1.0,
    tension=60.0, compression=60.0, shear=30.0, bending=0.8,
    tension_damping=5.0, shear_damping=5.0, bending_damping=0.5,
    collision_quality=5,
    distance_min=0.0012,       # cloth <-> board
    self_distance_min=0.0010,  # cloth <-> cloth (Blender's minimum; layers settle ~2 mm apart)
    self_friction=6.0,
    use_self_collision=True,
    self_impulse_clamp=0.0,
    fold2_shift=0.0,
    fold2_angle=180.0,
    fold2_hold=0.03,
    fold2_extra_lift=0.0025,   # fold-2 axis height above the top of the stack
    settle_air_damping=5.0,    # extra damping while released flaps drape (bleeds off stored energy)
    settle2_hold=True,
    fold2_inner_delta=0.004,
    fold2_land_gap=0.0030,     # after turning over, the pinned flap is lowered to this gap above the stack
    settle2_frames=30.0,
    settle2_soft=0.4,          # goal weight that keeps the released flap from springing (0 = free)
    settle2_pin_stiffness=1.0,
    fold2_land_pct=0.97,       # stack height percentile the flap is lowered onto (includes the fold-1 bulb)
    settle2_air_damping=10.0,     # inner (fold-1 flap) layer is pinned this much closer to the axis         # keep the stationary half held while the second flap drapes           # stationary half held down beyond this distance from the hinge band (<0: off)         # degrees (diagnostics: 0 = hold still)           # fold-2 axis moved into the flap by this many lifts (layer length balance)
    board_thickness_outer=0.0003,
    board_friction=8.0,
)

# fold stages: (label, frames, kind)
FOLD_FRAMES = 90
SETTLE_FRAMES = 60
FOLD_START, FOLD_END = 12, 70

# final placement of the tortilla on the board and the camera relative to it
TORTILLA_POS = Vector((0.0, 0.025, 0.0))
TORTILLA_YAW = math.radians(-72.0)
CAM_OFFSET = Vector((0.035, -0.52, 0.080))
CAM_AIM_Z = 0.027
LENS_MM = 85.0
FSTOP = 2.8


def parse_args():
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    p = argparse.ArgumentParser(prog="build_tortilla.py")
    p.add_argument("--output-dir", default="output")
    p.add_argument("--width", type=int, default=3840)
    p.add_argument("--height", type=int, default=2160)
    p.add_argument("--samples", type=int, default=1024)
    p.add_argument("--seed", type=int, default=7)
    p.add_argument("--bake-only", action="store_true")
    p.add_argument("--no-steam", action="store_true")
    p.add_argument("--stages", type=int, default=4, help="cloth stages to run (diagnostics; default all 4)")
    p.add_argument("--cloth", action="append", default=[], metavar="KEY=VALUE",
                   help="override a CLOTH parameter (diagnostics)")
    a = p.parse_args(argv)
    for kv in a.cloth:
        k, v = kv.split("=", 1)
        if k not in CLOTH:
            p.error(f"unknown cloth parameter {k}")
        CLOTH[k] = type(CLOTH[k])(float(v)) if not isinstance(CLOTH[k], bool) else v.lower() in ("1", "true")
    if not os.path.isabs(a.output_dir):
        a.output_dir = os.path.join(SCRIPT_DIR, a.output_dir)
    os.makedirs(a.output_dir, exist_ok=True)
    return a


def srgb(r, g, b, a=1.0):
    """sRGB display values -> linear RGBA for node colors."""
    def lin(c):
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4
    return (lin(r), lin(g), lin(b), a)


def link_object(obj):
    bpy.context.scene.collection.objects.link(obj)
    return obj


# ----------------------------------------------------------------------------------------------
# scene reset
# ----------------------------------------------------------------------------------------------

def reset_scene():
    for ob in list(bpy.data.objects):
        bpy.data.objects.remove(ob, do_unlink=True)
    for coll in (bpy.data.meshes, bpy.data.materials, bpy.data.lights, bpy.data.cameras,
                 bpy.data.textures, bpy.data.images, bpy.data.curves, bpy.data.worlds):
        for blk in list(coll):
            coll.remove(blk)
    for c in list(bpy.data.collections):
        bpy.data.collections.remove(c)
    scene = bpy.context.scene
    scene.frame_start, scene.frame_end = 1, 250
    scene.render.fps = 24
    scene.unit_settings.system = 'METRIC'
    scene.unit_settings.scale_length = 1.0
    scene.use_gravity = True
    scene.gravity = (0.0, 0.0, -9.81)
    log(f"scene cleared ({len(bpy.data.objects)} objects left)")
    return scene


# ----------------------------------------------------------------------------------------------
# geometry
# ----------------------------------------------------------------------------------------------

def build_tortilla(seed):
    """Disc of near-equilateral triangles in concentric rings (6k vertices on ring k)."""
    rng = random.Random(seed)
    rings_n = max(4, round(TORTILLA_RADIUS / EDGE_LEN))
    pts, rings, angles = [(0.0, 0.0)], [[0]], [[0.0]]
    for k in range(1, rings_n + 1):
        n = 6 * k
        off = (math.pi / n) * 0.5 * (k % 2)
        idx, ang = [], []
        for i in range(n):
            a = off + 2.0 * math.pi * i / n
            idx.append(len(pts))
            ang.append(a)
            pts.append((k * EDGE_LEN * math.cos(a), k * EDGE_LEN * math.sin(a)))
        rings.append(idx)
        angles.append(ang)

    tris = [(0, rings[1][i], rings[1][(i + 1) % 6]) for i in range(6)]
    for k in range(2, rings_n + 1):
        A, aa, B, bb = rings[k - 1], angles[k - 1], rings[k], angles[k]
        na, nb = len(A), len(B)
        ua = lambda i: aa[i % na] + 2.0 * math.pi * (i // na)
        ub = lambda j: bb[j % nb] + 2.0 * math.pi * (j // nb)
        i = j = 0
        while i < na or j < nb:
            if j >= nb or (i < na and ua(i + 1) <= ub(j + 1)):
                tris.append((A[i % na], B[j % nb], A[(i + 1) % na]))
                i += 1
            else:
                tris.append((A[i % na], B[j % nb], B[(j + 1) % nb]))
                j += 1

    # irregular rim: low-order harmonics + a few fine wobbles, blended in toward the edge
    harmonics = []
    for m in range(2, 11):
        harmonics.append((m, rng.uniform(0.35, 1.0) * 0.011 / m ** 0.75, rng.uniform(0, 2 * math.pi)))
    for m in range(11, 25):
        harmonics.append((m, rng.uniform(0.0, 1.0) * 0.0016, rng.uniform(0, 2 * math.pi)))
    R = TORTILLA_RADIUS
    noise.seed_set(seed)
    verts = []
    for (x, y) in pts:
        r = math.hypot(x, y)
        th = math.atan2(y, x)
        f = 1.0 + sum(a * math.sin(m * th + ph) for (m, a, ph) in harmonics)
        s = 1.0 + (f - 1.0) * (r / R) ** 2
        x, y = x * s, y * s
        # very gentle natural waviness (sub-millimetre) -> also becomes part of the rest shape
        z = 0.0026 + 0.0005 * noise.noise(Vector((x * 38.0, y * 38.0, 0.37)))
        verts.append((x, y, z))

    faces = []
    for (a, b, c) in tris:
        (x0, y0, _), (x1, y1, _), (x2, y2, _) = verts[a], verts[b], verts[c]
        cross = (x1 - x0) * (y2 - y0) - (y1 - y0) * (x2 - x0)
        faces.append((a, b, c) if cross > 0 else (a, c, b))

    me = bpy.data.meshes.new("Tortilla")
    me.from_pydata(verts, [], faces)
    me.validate(clean_customdata=False)
    me.update()
    obj = link_object(bpy.data.objects.new("Tortilla", me))

    # UVs = flat rest position (1 UV unit = 25 cm); textures follow the cloth through the folds
    uv = me.uv_layers.new(name="UVMap")
    loop_uv = []
    for loop in me.loops:
        x, y, _ = verts[loop.vertex_index]
        loop_uv += [x / 0.25 + 0.5, y / 0.25 + 0.5]
    uv.data.foreach_set("uv", loop_uv)

    # non-uniform thickness: weights 0.80..1.0, slightly thinner toward the rim
    vg = obj.vertex_groups.new(name="thickness")
    for i, (x, y, _) in enumerate(verts):
        n = noise.noise(Vector((x * 22.0, y * 22.0, 3.1)))
        r = math.hypot(x, y) / R
        w = 0.955 + 0.05 * n - 0.05 * r * r
        vg.add([i], max(0.88, min(1.0, w)), 'REPLACE')

    for poly in me.polygons:
        poly.use_smooth = True
    log(f"tortilla mesh: {len(me.vertices)} verts, {len(me.polygons)} tris, {rings_n} rings, "
        f"edge {EDGE_LEN * 1000:.1f} mm, diameter ~{2 * R * 100:.0f} cm")
    return obj, [Vector(v) for v in verts]


def build_board():
    lx, ly, lz = BOARD_SIZE
    bm = bmesh.new()
    bmesh.ops.create_cube(bm, size=1.0)
    bmesh.ops.scale(bm, vec=(lx, ly, lz), verts=bm.verts)
    vertical = [e for e in bm.edges
                if abs((e.verts[0].co - e.verts[1].co).normalized().z) > 0.99]
    bmesh.ops.bevel(bm, geom=vertical, offset=0.022, segments=10, profile=0.5,
                    affect='EDGES', clamp_overlap=True)
    horizontal = [e for e in bm.edges
                  if abs((e.verts[0].co - e.verts[1].co).normalized().z) < 0.01
                  and abs(abs(e.verts[0].co.z) - lz / 2) < 1e-6]
    bmesh.ops.bevel(bm, geom=horizontal, offset=0.0035, segments=4, profile=0.5,
                    affect='EDGES', clamp_overlap=True)
    bmesh.ops.recalc_face_normals(bm, faces=bm.faces)
    for f in bm.faces:
        f.smooth = f.calc_area() < 2e-3
    for e in bm.edges:
        if len(e.link_faces) == 2:
            f0, f1 = e.link_faces
            if (not f0.smooth) or (not f1.smooth) or f0.normal.angle(f1.normal, 0) > math.radians(35):
                e.smooth = False
    me = bpy.data.meshes.new("CuttingBoard")
    bm.to_mesh(me)
    bm.free()
    obj = link_object(bpy.data.objects.new("CuttingBoard", me))
    obj.location = (0.0, 0.0, -lz / 2)
    obj.rotation_euler = (0.0, 0.0, math.radians(-6.0))
    obj.modifiers.new("Collision", 'COLLISION')
    obj.collision.thickness_outer = CLOTH["board_thickness_outer"]
    obj.collision.cloth_friction = CLOTH["board_friction"]
    obj.collision.damping = 0.6
    log(f"cutting board {lx * 100:.0f} x {ly * 100:.0f} x {lz * 100:.1f} cm, rounded corners, "
        f"bevelled edges, {len(me.polygons)} faces (collider)")
    return obj


def build_backdrop():
    """Seamless 'infinity cove': flat floor under the board curving up into a wall behind."""
    width, floor_front, floor_back, radius, height = 4.0, -1.5, 1.05, 0.55, 1.6
    prof = [(floor_front, 0.0)]
    steps = 24
    for i in range(steps + 1):
        a = -math.pi / 2 + (math.pi / 2) * i / steps
        prof.append((floor_back + radius * math.cos(a), radius + radius * math.sin(a)))
    prof.append((floor_back + radius, height))
    verts, faces = [], []
    for (y, z) in prof:
        verts.append((-width / 2, y, FLOOR_Z + z))
        verts.append((width / 2, y, FLOOR_Z + z))
    for i in range(len(prof) - 1):
        faces.append((2 * i, 2 * i + 1, 2 * i + 3, 2 * i + 2))
    me = bpy.data.meshes.new("Backdrop")
    me.from_pydata(verts, [], faces)
    for p in me.polygons:
        p.use_smooth = True
    me.update()
    obj = link_object(bpy.data.objects.new("Backdrop", me))
    log("seamless backdrop built")
    return obj


def build_bowl():
    prof = [(0.0, 0.0), (0.030, 0.0), (0.034, 0.003), (0.032, 0.006), (0.045, 0.010),
            (0.058, 0.024), (0.064, 0.043), (0.0645, 0.0485), (0.062, 0.050),
            (0.0595, 0.046), (0.054, 0.027), (0.042, 0.015), (0.020, 0.0105), (0.0, 0.010)]
    bm = bmesh.new()
    vs = [bm.verts.new((r, 0.0, z)) for (r, z) in prof]
    es = [bm.edges.new((vs[i], vs[i + 1])) for i in range(len(vs) - 1)]
    bmesh.ops.spin(bm, geom=vs + es, cent=(0, 0, 0), axis=(0, 0, 1), angle=2 * math.pi,
                   steps=72, use_merge=True)
    bmesh.ops.remove_doubles(bm, verts=bm.verts, dist=1e-6)
    bmesh.ops.recalc_face_normals(bm, faces=bm.faces)
    me = bpy.data.meshes.new("Bowl")
    bm.to_mesh(me)
    bm.free()
    for p in me.polygons:
        p.use_smooth = True
    obj = link_object(bpy.data.objects.new("Bowl", me))
    sub = obj.modifiers.new("Subsurf", 'SUBSURF')
    sub.levels, sub.render_levels = 1, 2
    return obj


def build_lime(name, seed):
    bm = bmesh.new()
    bmesh.ops.create_uvsphere(bm, u_segments=48, v_segments=24, radius=1.0)
    noise.seed_set(seed)
    for v in bm.verts:
        c = v.co.copy()
        n = noise.noise(c * 2.2 + Vector((seed, 0, 0)))
        tip = max(0.0, abs(c.x) - 0.88) * 0.9       # little nubs at both ends
        v.co = Vector((c.x * (1.10 + tip), c.y, c.z)) * (1.0 + 0.012 * n)
    me = bpy.data.meshes.new(name)
    bm.to_mesh(me)
    bm.free()
    for p in me.polygons:
        p.use_smooth = True
    obj = link_object(bpy.data.objects.new(name, me))
    obj.scale = (0.030, 0.030, 0.029)
    sub = obj.modifiers.new("Subsurf", 'SUBSURF')
    sub.levels, sub.render_levels = 1, 2
    return obj


# ----------------------------------------------------------------------------------------------
# cloth simulation
# ----------------------------------------------------------------------------------------------

def mesh_coords(me):
    co = [0.0] * (3 * len(me.vertices))
    me.vertices.foreach_get("co", co)
    return [Vector(co[i:i + 3]) for i in range(0, len(co), 3)]


def evaluated_coords(obj):
    dg = bpy.context.evaluated_depsgraph_get()
    ev = obj.evaluated_get(dg)
    me = ev.to_mesh()
    co = mesh_coords(me)
    ev.to_mesh_clear()
    return co


def set_coords(obj, coords):
    flat = [c for v in coords for c in v]
    obj.data.vertices.foreach_set("co", flat)
    obj.data.update()


def configure_cloth(mod, pinned, settle=False, air=None, pin_stiffness=1.0):
    s = mod.settings
    s.quality = CLOTH["quality"]
    s.mass = CLOTH["mass"]
    s.air_damping = air if air is not None else (CLOTH["settle_air_damping"] if settle else CLOTH["air_damping"])
    s.bending_model = 'ANGULAR'
    s.tension_stiffness = CLOTH["tension"]
    s.compression_stiffness = CLOTH["compression"]
    s.shear_stiffness = CLOTH["shear"]
    s.bending_stiffness = CLOTH["bending"]
    s.tension_damping = CLOTH["tension_damping"]
    s.compression_damping = CLOTH["tension_damping"]
    s.shear_damping = CLOTH["shear_damping"]
    s.bending_damping = CLOTH["bending_damping"]
    s.vertex_group_mass = "pin" if pinned else ""
    s.pin_stiffness = pin_stiffness
    c = mod.collision_settings
    c.use_collision = True
    c.collision_quality = CLOTH["collision_quality"]
    c.distance_min = CLOTH["distance_min"]
    c.use_self_collision = CLOTH["use_self_collision"]
    c.self_distance_min = CLOTH["self_distance_min"]
    c.self_friction = CLOTH["self_friction"]
    c.self_impulse_clamp = CLOTH["self_impulse_clamp"]


def bake_point_cache(scene, obj, mod, frames, label):
    pc = mod.point_cache
    pc.frame_start, pc.frame_end = 1, frames
    scene.frame_start, scene.frame_end = 1, frames
    scene.frame_set(1)
    t0 = time.time()
    baked = False
    try:
        with bpy.context.temp_override(scene=scene, view_layer=scene.view_layers[0],
                                       active_object=obj, object=obj):
            bpy.ops.ptcache.bake_all(bake=True)
        baked = bool(pc.is_baked)
    except Exception as exc:  # pragma: no cover - depends on Blender build
        log(f"  [{label}] ptcache.bake_all raised {exc!r}")
    if not baked:
        log(f"  [{label}] point-cache bake unavailable, stepping frames instead")
        for f in range(1, frames + 1):
            scene.frame_set(f)
            if f % 10 == 0:
                log(f"  [{label}] simulated frame {f}/{frames}")
    log(f"  [{label}] {'baked' if baked else 'simulated'} {frames} frames in {time.time() - t0:.1f}s")
    return baked


def cloth_stage(scene, obj, label, frames, fold=None, snapshots=()):
    """One baked cloth stage.  fold = dict(pin=[indices], origin, yaw, axis 'X'|'Y', angle)."""
    scene.frame_set(1)
    for g in ("pin", "hook"):
        if g in obj.vertex_groups:
            obj.vertex_groups.remove(obj.vertex_groups[g])
    hinge = None
    if fold:
        vg = obj.vertex_groups.new(name="pin")
        vg.add(fold["pin"] + fold.get("hold", []), 1.0, 'REPLACE')
        if fold.get("soft"):
            vg.add(fold["soft"], fold["soft_weight"], 'REPLACE')
    if fold and fold["pin"]:
        obj.vertex_groups.new(name="hook").add(fold["pin"], 1.0, 'REPLACE')
        hinge = link_object(bpy.data.objects.new(f"Hinge_{label}", None))
        hinge.empty_display_type = 'ARROWS'
        hinge.empty_display_size = 0.05
        hinge.location = fold["origin"]
        base = Euler((0.0, 0.0, fold["yaw"]), 'XYZ')
        end = base.copy()
        if fold["axis"] == 'X':
            end.x = fold["angle"]
        else:
            end.y = fold["angle"]
        hinge.rotation_euler = base
        hinge.keyframe_insert("rotation_euler", frame=FOLD_START)
        hinge.rotation_euler = end
        hinge.keyframe_insert("rotation_euler", frame=FOLD_END)
        if fold.get("drop", 0.0) > 0.0:
            # lay the turned-over flap down onto the stack instead of dropping it
            hinge.keyframe_insert("location", frame=FOLD_END)
            hinge.location.z -= fold["drop"]
            hinge.keyframe_insert("location", frame=FOLD_END + 12)
        scene.frame_set(1)
        bpy.context.view_layer.update()
        hook = obj.modifiers.new("FoldHook", 'HOOK')
        hook.object = hinge
        hook.vertex_group = "hook"
        hook.falloff_type = 'NONE'
        hook.strength = 1.0
        hook.matrix_inverse = hinge.matrix_world.inverted()
    mod = obj.modifiers.new("Cloth", 'CLOTH')
    configure_cloth(mod, pinned=bool(fold), settle=not (fold and fold["pin"]),
                    air=fold.get("air") if fold else None,
                    pin_stiffness=fold.get("pin_stiffness", 1.0) if fold else 1.0)
    baked = bake_point_cache(scene, obj, mod, frames, label)

    snaps = {}
    for f in snapshots:
        scene.frame_set(f)
        snaps[f] = evaluated_coords(obj)
    scene.frame_set(frames)
    co = evaluated_coords(obj)
    bad = [v for v in co if not all(math.isfinite(c) for c in v) or v.length > 0.6]
    if bad:
        raise RuntimeError(f"cloth stage {label}: simulation exploded ({len(bad)} bad vertices)")
    for m in list(obj.modifiers):
        obj.modifiers.remove(m)
    if hinge:
        bpy.data.objects.remove(hinge, do_unlink=True)
    for g in ("pin", "hook"):
        if g in obj.vertex_groups:
            obj.vertex_groups.remove(obj.vertex_groups[g])
    set_coords(obj, co)
    scene.frame_set(1)
    zs = sorted(v.z for v in co)
    log(f"  [{label}] frozen frame {frames}: z min {zs[0] * 1000:.2f} mm, median "
        f"{zs[len(zs) // 2] * 1000:.2f} mm, max {zs[-1] * 1000:.2f} mm, "
        f"verts below 0.3 mm: {sum(1 for z in zs if z < 0.0003)}")
    return dict(label=label, frames=frames, baked=baked, z_min=zs[0], z_max=zs[-1]), snaps


def simulate_folds(scene, obj, seed, rest, max_stages=4):
    rng = random.Random(seed * 31 + 5)
    R = TORTILLA_RADIUS
    stats, snaps = [], {}
    log("cloth simulation: 4 stages (fold 1, settle, fold 2, settle)")

    # Each fold pins the whole flap except a narrow hinge band and swings it rigidly through 180 deg
    # about an axis at the band centre, raised by the intended loop radius.  The band (width ~ pi *
    # radius) is the only part that has to bend, so it rolls into a round fold instead of buckling;
    # the settle stage then releases the flap so it drapes down onto the layer below.
    # fold 1: the +y half (rotated a little) flips over onto the -y half
    co = mesh_coords(obj.data)
    yaw1 = math.radians(rng.uniform(-4.0, 4.0))
    off1 = rng.uniform(0.002, 0.005)
    n1 = Vector((-math.sin(yaw1), math.cos(yaw1), 0.0))
    z_rest = 0.0015
    lift1 = 0.0035
    band1 = math.pi * lift1 + 0.003
    origin1 = n1 * off1
    origin1.z = z_rest + lift1
    pin1 = [i for i, v in enumerate(co) if (v - origin1).dot(n1) > band1 / 2]
    log(f"  fold 1: yaw {math.degrees(yaw1):+.1f} deg, offset {off1 * 1000:.1f} mm, hinge band "
        f"{band1 * 1000:.1f} mm, {len(pin1)} pinned flap verts, axis height {origin1.z * 1000:.1f} mm")
    s, sn = cloth_stage(scene, obj, "fold1", FOLD_FRAMES,
                        dict(pin=pin1, origin=origin1, yaw=yaw1, axis='X', angle=math.pi),
                        snapshots=(40,))
    stats.append(s)
    snaps["fold1_f40"] = sn[40]
    s, _ = cloth_stage(scene, obj, "settle1", SETTLE_FRAMES)
    stats.append(s)
    snaps["after_fold1"] = mesh_coords(obj.data)
    if max_stages <= 2:
        return stats, snaps

    # fold 2: the +x half of the semicircle (both layers) flips over onto the -x half
    co = mesh_coords(obj.data)
    yaw2 = math.radians(rng.uniform(-4.0, 4.0))
    off2 = rng.uniform(-0.004, -0.001)
    n2 = Vector((math.cos(yaw2), math.sin(yaw2), 0.0))
    zs = sorted(v.z for v in co)
    z_top = zs[int(len(zs) * 0.75)]
    lift2 = (z_top - z_rest) + CLOTH["fold2_extra_lift"]
    band2 = 0.85 * math.pi * lift2 + 0.003
    origin2 = n2 * (off2 + CLOTH["fold2_shift"] * lift2)
    origin2.z = z_rest + lift2
    # the inner layer of this fold (the fold-1 flap, now on top) needs ~pi*gap less material in
    # its loop than the outer layer, so its pinned region starts closer to the axis
    flap1 = {i for i, v in enumerate(rest) if (v - origin1).dot(n1) > 0.0}
    pin2 = [i for i, v in enumerate(co)
            if (v - origin2).dot(n2) > band2 / 2 - (CLOTH["fold2_inner_delta"] if i in flap1 else 0.0)]
    # a "second hand" holds the stationary half down while the flap goes over, so the length
    # mismatch between inner and outer layer stays in the fold instead of peeling the top layer up
    hold2 = []
    if CLOTH["fold2_hold"] >= 0:
        hold2 = [i for i, v in enumerate(co) if (v - origin2).dot(n2) < -band2 / 2 - CLOTH["fold2_hold"]]
    # inner flap layer lands at 2*axis - z_top; lower it to land_gap above the stack top
    z_land = zs[int(len(zs) * CLOTH["fold2_land_pct"])]
    drop2 = max(0.0, (2 * origin2.z - z_top) - (z_land + CLOTH["fold2_land_gap"]))
    log(f"  fold 2: yaw {math.degrees(yaw2):+.1f} deg, offset {off2 * 1000:.1f} mm, "
        f"drop {drop2 * 1000:.1f} mm, hinge band {band2 * 1000:.1f} mm, {len(pin2)} pinned + {len(hold2)} held verts, axis height {origin2.z * 1000:.1f} mm")
    s, sn = cloth_stage(scene, obj, "fold2", FOLD_FRAMES,
                        dict(pin=pin2, hold=hold2, drop=drop2, origin=origin2, yaw=yaw2, axis='Y', angle=-math.radians(CLOTH["fold2_angle"])),
                        snapshots=(40, 55, 70))
    stats.append(s)
    for f in (40, 55, 70):
        snaps[f"fold2_f{f}"] = sn[f]
    snaps["fold2_f90"] = mesh_coords(obj.data)
    s, sn = cloth_stage(scene, obj, "settle2", int(CLOTH["settle2_frames"]),
                        dict(pin=[], hold=hold2 if CLOTH["settle2_hold"] else [],
                             soft=pin2 if CLOTH["settle2_soft"] > 0 else [],
                             soft_weight=CLOTH["settle2_soft"],
                             pin_stiffness=CLOTH["settle2_pin_stiffness"],
                             air=CLOTH["settle2_air_damping"]), snapshots=(10, 15))
    stats.append(s)
    for f in (10, 15):
        snaps[f"settle2_f{f}"] = sn[f]
    return stats, snaps


def layer_report(coords, rest):
    """Count vertex pairs from different layers (far apart at rest) that ended up too close."""
    kd = KDTree(len(coords))
    for i, v in enumerate(coords):
        kd.insert(v, i)
    kd.balance()
    close, dmin, gaps = 0, 1.0, []
    for i, v in enumerate(coords):
        best = None
        for (c, j, d) in kd.find_range(v, 0.008):
            if j != i and (rest[i] - rest[j]).length > 0.02:
                if best is None or d < best:
                    best = d
        if best is not None:
            gaps.append(best)
            dmin = min(dmin, best)
            if best < 0.0019:
                close += 1
    gaps.sort()
    pct = lambda q: gaps[int(q * (len(gaps) - 1))] if gaps else None
    return dict(stacked_verts=len(gaps), too_close=close, min_gap=dmin if gaps else None,
                gap_p05=pct(0.05), gap_p50=pct(0.5))


def write_obj(path, coords, me):
    with open(path, "w") as f:
        for v in coords:
            f.write(f"v {v.x:.6f} {v.y:.6f} {v.z:.6f}\n")
        for p in me.polygons:
            f.write("f " + " ".join(str(i + 1) for i in p.vertices) + "\n")


# ----------------------------------------------------------------------------------------------
# shader-node helper
# ----------------------------------------------------------------------------------------------

class Nodes:
    def __init__(self, tree):
        self.tree = tree
        tree.nodes.clear()

    def new(self, kind, **props):
        nd = self.tree.nodes.new(kind)
        for k, v in props.items():
            setattr(nd, k, v)
        return nd

    def feed(self, sock, val):
        if isinstance(val, bpy.types.NodeSocket):
            self.tree.links.new(val, sock)
        elif val is not None:
            sock.default_value = val

    def math(self, op, a, b=None, c=None, clamp=False):
        nd = self.new('ShaderNodeMath', operation=op, use_clamp=clamp)
        self.feed(nd.inputs[0], a)
        if b is not None:
            self.feed(nd.inputs[1], b)
        if c is not None:
            self.feed(nd.inputs[2], c)
        return nd.outputs[0]

    def add(self, *vals):
        out = vals[0]
        for v in vals[1:]:
            out = self.math('ADD', out, v)
        return out

    def mul(self, a, b, clamp=False):
        return self.math('MULTIPLY', a, b, clamp=clamp)

    def vmath(self, op, a, b=None, scale=None):
        nd = self.new('ShaderNodeVectorMath', operation=op)
        self.feed(nd.inputs[0], a)
        if b is not None:
            self.feed(nd.inputs[1], b)
        if scale is not None:
            self.feed(nd.inputs['Scale'], scale)
        if op in ('LENGTH', 'DOT_PRODUCT', 'DISTANCE'):
            return nd.outputs['Value']
        return nd.outputs['Vector']

    def mapping(self, vec, loc=(0, 0, 0), rot=(0, 0, 0), scale=(1, 1, 1)):
        nd = self.new('ShaderNodeMapping', vector_type='POINT')
        self.feed(nd.inputs['Vector'], vec)
        nd.inputs['Location'].default_value = loc
        nd.inputs['Rotation'].default_value = rot
        nd.inputs['Scale'].default_value = scale
        return nd.outputs['Vector']

    def noise(self, vec, scale, detail=2.0, rough=0.5, distortion=0.0, lacunarity=2.0):
        nd = self.new('ShaderNodeTexNoise')
        nd.noise_dimensions = '3D'
        self.feed(nd.inputs['Vector'], vec)
        nd.inputs['Scale'].default_value = scale
        nd.inputs['Detail'].default_value = detail
        nd.inputs['Roughness'].default_value = rough
        nd.inputs['Lacunarity'].default_value = lacunarity
        nd.inputs['Distortion'].default_value = distortion
        return nd

    def voronoi(self, vec, scale, randomness=1.0):
        nd = self.new('ShaderNodeTexVoronoi')
        nd.voronoi_dimensions = '3D'
        nd.feature = 'F1'
        nd.distance = 'EUCLIDEAN'
        self.feed(nd.inputs['Vector'], vec)
        nd.inputs['Scale'].default_value = scale
        nd.inputs['Randomness'].default_value = randomness
        return nd

    def maprange(self, v, fmin, fmax, tmin=0.0, tmax=1.0, smooth=False, clamp=True):
        nd = self.new('ShaderNodeMapRange', data_type='FLOAT',
                      interpolation_type='SMOOTHSTEP' if smooth else 'LINEAR')
        nd.clamp = clamp
        for idx, val in enumerate((v, fmin, fmax, tmin, tmax)):
            self.feed(nd.inputs[idx], val)
        return nd.outputs[0]

    def ramp(self, fac, stops, interp='LINEAR'):
        nd = self.new('ShaderNodeValToRGB')
        cr = nd.color_ramp
        cr.interpolation = interp
        els = cr.elements
        while len(els) > 1:
            els.remove(els[-1])
        els[0].position, els[0].color = stops[0]
        for pos, col in stops[1:]:
            e = els.new(pos)
            e.color = col
        self.feed(nd.inputs['Fac'], fac)
        return nd.outputs['Color']

    def mix(self, fac, a, b, blend='MIX'):
        nd = self.new('ShaderNodeMix', data_type='RGBA', blend_type=blend)
        nd.clamp_result = True
        fs = next(s for s in nd.inputs if s.name == 'Factor' and s.type == 'VALUE')
        cs = [s for s in nd.inputs if s.type == 'RGBA']
        self.feed(fs, fac)
        self.feed(cs[0], a)
        self.feed(cs[1], b)
        return next(s for s in nd.outputs if s.type == 'RGBA')

    def sep(self, vec):
        nd = self.new('ShaderNodeSeparateXYZ')
        self.feed(nd.inputs[0], vec)
        return nd.outputs[0], nd.outputs[1], nd.outputs[2]

    def comb(self, x, y, z):
        nd = self.new('ShaderNodeCombineXYZ')
        for i, v in enumerate((x, y, z)):
            self.feed(nd.inputs[i], v)
        return nd.outputs[0]

    def bump(self, height, strength, distance, normal=None):
        nd = self.new('ShaderNodeBump')
        self.feed(nd.inputs['Height'], height)
        nd.inputs['Strength'].default_value = strength
        nd.inputs['Distance'].default_value = distance
        if normal is not None:
            self.feed(nd.inputs['Normal'], normal)
        return nd.outputs['Normal']

    def principled(self, **inputs):
        nd = self.new('ShaderNodeBsdfPrincipled')
        for name, val in inputs.items():
            self.feed(nd.inputs[name.replace('_', ' ')], val)
        return nd

    def output(self, surface=None, volume=None):
        nd = self.new('ShaderNodeOutputMaterial', target='ALL')
        if surface is not None:
            self.feed(nd.inputs['Surface'], surface)
        if volume is not None:
            self.feed(nd.inputs['Volume'], volume)
        return nd


def new_material(name):
    mat = bpy.data.materials.new(name)
    mat.use_nodes = True
    return mat, Nodes(mat.node_tree)


# ----------------------------------------------------------------------------------------------
# materials
# ----------------------------------------------------------------------------------------------

def tortilla_material(name, seed):
    mat, N = new_material(name)
    uv = N.new('ShaderNodeUVMap', uv_map="UVMap").outputs['UV']
    p = N.mapping(uv, loc=(0.0, 0.0, 3.0 + seed * 13.37), scale=(25.0, 25.0, 0.0))   # 1 unit = 1 cm
    warp = N.vmath('SUBTRACT', N.noise(p, 0.35, 2.0, 0.5).outputs['Color'], (0.5, 0.5, 0.5))
    pw = N.vmath('ADD', p, N.vmath('SCALE', warp, scale=1.3))
    ragged = N.math('SUBTRACT', N.noise(p, 2.4, 4.0, 0.62).outputs['Fac'], 0.5)

    def spots(vec, scale, r0, r1, pres_lo, pres_hi, i0, i1, rag, core=0.22):
        v = N.voronoi(vec, scale)
        cx, cy, cz = N.sep(v.outputs['Color'])
        pres = N.maprange(cx, pres_lo, pres_hi, smooth=True)
        rad = N.maprange(cy, 0.0, 1.0, r0, r1)
        d = N.math('ADD', v.outputs['Distance'], N.mul(ragged, rag))
        prof = N.maprange(N.math('DIVIDE', d, rad), 1.0, core, smooth=True)
        return N.mul(N.mul(prof, pres), N.maprange(cz, 0.0, 1.0, i0, i1))

    big = spots(pw, 0.55, 0.14, 0.44, 0.50, 0.62, 0.45, 1.0, 0.16)
    mid = spots(N.vmath('ADD', pw, (3.7, 1.3, 0.0)), 1.45, 0.10, 0.36, 0.45, 0.58, 0.35, 0.9, 0.20)
    speck = spots(N.vmath('ADD', p, (8.1, 2.9, 0.0)), 4.6, 0.07, 0.20, 0.80, 0.87, 0.6, 1.0, 0.10)
    dens = N.maprange(N.noise(N.vmath('ADD', p, (1.7, 9.2, 0.0)), 0.16, 2.0).outputs['Fac'],
                      0.32, 0.68, 0.30, 1.0, smooth=True)
    toast = N.mul(N.math('MAXIMUM', big, N.mul(mid, 0.82)), dens)
    core = N.maprange(toast, 0.62, 1.0, 0.0, 1.0, smooth=True)
    char = N.math('MAXIMUM', N.mul(speck, dens), N.mul(core, 0.65))
    val = N.math('ADD', N.mul(toast, 0.80), N.mul(char, 0.38), clamp=True)

    col = N.ramp(val, [
        (0.00, srgb(0.93, 0.87, 0.75)),
        (0.12, srgb(0.91, 0.82, 0.63)),
        (0.30, srgb(0.83, 0.65, 0.39)),
        (0.50, srgb(0.67, 0.45, 0.21)),
        (0.70, srgb(0.47, 0.28, 0.12)),
        (0.88, srgb(0.29, 0.16, 0.07)),
        (1.00, srgb(0.15, 0.08, 0.04)),
    ])
    mottle = N.maprange(N.noise(p, 1.3, 3.0, 0.55).outputs['Fac'], 0.35, 0.65, 0.93, 1.04)
    col = N.mix(1.0, col, N.comb(mottle, mottle, mottle), blend='MULTIPLY')

    fpatch = N.maprange(N.noise(N.vmath('ADD', p, (11.0, 4.0, 0.0)), 0.45, 3.0).outputs['Fac'],
                        0.50, 0.66, smooth=True)
    fgrain = N.maprange(N.noise(p, 14.0, 2.0, 0.6).outputs['Fac'], 0.45, 0.72, smooth=True)
    flour = N.mul(N.mul(fpatch, N.add(0.25, N.mul(fgrain, 0.75))), 0.62, clamp=True)
    col = N.mix(flour, col, srgb(0.96, 0.95, 0.91))

    rough = N.math('ADD', N.add(0.47, N.mul(flour, 0.32)), N.mul(toast, -0.08), clamp=True)
    sss = N.math('ADD', 0.32, N.mul(toast, -0.20), clamp=True)
    grain = N.noise(p, 6.0, 5.0, 0.6).outputs['Fac']
    height = N.add(N.mul(big, 0.5), N.mul(mid, 0.3), N.mul(grain, 0.35), N.mul(flour, 0.12))
    nrm = N.bump(height, 0.38, 0.0004)
    bsdf = N.principled(Base_Color=col, Roughness=rough, Subsurface_Weight=sss,
                        Subsurface_Radius=(1.0, 0.62, 0.35), Subsurface_Scale=0.0022,
                        Specular_IOR_Level=0.5, IOR=1.45, Sheen_Weight=0.22, Sheen_Roughness=0.4,
                        Sheen_Tint=srgb(1.0, 0.97, 0.92), Normal=nrm)
    bsdf.subsurface_method = 'RANDOM_WALK'
    N.output(bsdf.outputs[0])
    return mat


def board_material():
    mat, N = new_material("OiledWood")
    p = N.new('ShaderNodeTexCoord').outputs['Object']
    # anisotropic wobble so the grain lines meander along the board's long axis (x)
    wob = N.vmath('SUBTRACT', N.noise(N.mapping(p, scale=(3.0, 26.0, 26.0)), 1.0, 3.0, 0.5).outputs['Color'],
                  (0.5, 0.5, 0.5))
    pg = N.vmath('ADD', p, N.vmath('SCALE', wob, scale=0.010))
    # growth rings around a log axis far below the board, tilted slightly -> cathedral arches
    ring_co = N.mapping(pg, loc=(0.0, 0.21, 0.17), rot=(0.0, math.radians(1.6), math.radians(0.8)))
    wave = N.new('ShaderNodeTexWave', wave_type='RINGS', rings_direction='X', wave_profile='SAW')
    N.feed(wave.inputs['Vector'], ring_co)
    wave.inputs['Scale'].default_value = 62.0
    wave.inputs['Distortion'].default_value = 3.0
    wave.inputs['Detail'].default_value = 3.0
    wave.inputs['Detail Scale'].default_value = 0.6
    rings = wave.outputs['Fac']
    ring_col = N.ramp(rings, [
        (0.00, srgb(0.53, 0.35, 0.21)),
        (0.55, srgb(0.47, 0.30, 0.17)),
        (0.86, srgb(0.31, 0.18, 0.09)),
        (1.00, srgb(0.27, 0.15, 0.075)),
    ])
    fibers = N.noise(N.mapping(pg, scale=(4.0, 950.0, 950.0)), 1.0, 2.0, 0.6).outputs['Fac']
    pores = N.maprange(N.noise(N.mapping(pg, scale=(70.0, 1900.0, 1900.0)), 1.0, 1.0).outputs['Fac'],
                       0.64, 0.74, smooth=True)
    tone = N.maprange(N.noise(p, 3.0, 2.0).outputs['Fac'], 0.3, 0.7, 0.86, 1.10)
    fib_mul = N.maprange(fibers, 0.3, 0.7, 0.86, 1.05)
    shade = N.mul(N.mul(tone, fib_mul), N.maprange(pores, 0.0, 1.0, 1.0, 0.72))
    col = N.mix(1.0, ring_col, N.comb(shade, shade, shade), blend='MULTIPLY')

    # knife marks: thin contour lines of strongly stretched noise, in several directions,
    # masked to short segments; denser toward the middle of the board where it gets used
    usage = N.maprange(N.vmath('LENGTH', N.mapping(p, scale=(1.0, 1.5, 0.0))), 0.06, 0.22, 1.0, 0.35,
                       smooth=True)
    scratch = None
    for k, ang in enumerate((14.0, -22.0, 63.0, 101.0, 152.0, 37.0)):
        q = N.mapping(p, loc=(k * 1.7, k * 0.9, 0.0), rot=(0.0, 0.0, math.radians(ang)))
        line_n = N.noise(N.mapping(q, scale=(6.0, 260.0, 1.0)), 1.0, 1.0, 0.5).outputs['Fac']
        dist = N.math('ABSOLUTE', N.math('SUBTRACT', line_n, 0.5))
        line = N.maprange(dist, 0.0, 0.010, 1.0, 0.0, smooth=True)
        seg = N.maprange(N.noise(N.mapping(q, loc=(5.0, 2.0, 0.0), scale=(14.0, 34.0, 1.0)),
                                 1.0, 2.0).outputs['Fac'], 0.60, 0.68, smooth=True)
        s = N.mul(line, seg)
        scratch = s if scratch is None else N.math('MAXIMUM', scratch, s)
    scratch = N.mul(scratch, usage)
    col = N.mix(N.mul(scratch, 0.35), col, srgb(0.66, 0.50, 0.34))
    wear = N.maprange(N.noise(N.vmath('ADD', p, (4.0, 4.0, 4.0)), 7.0, 3.0).outputs['Fac'], 0.55, 0.75,
                      smooth=True)
    col = N.mix(N.mul(wear, 0.18), col, srgb(0.36, 0.22, 0.12))

    rough = N.add(0.36, N.mul(fibers, 0.10), N.mul(scratch, 0.22), N.mul(wear, 0.08))
    dents = N.noise(p, 45.0, 3.0, 0.5).outputs['Fac']
    height = N.add(N.mul(scratch, -1.0), N.mul(fibers, 0.18), N.mul(rings, 0.06),
                   N.mul(pores, -0.25), N.mul(dents, 0.12))
    nrm = N.bump(height, 0.28, 0.0004)
    bsdf = N.principled(Base_Color=col, Roughness=rough, Specular_IOR_Level=0.5,
                        Coat_Weight=0.22, Coat_Roughness=0.28, Normal=nrm)
    N.output(bsdf.outputs[0])
    return mat


def backdrop_material():
    mat, N = new_material("SeamlessBackdrop")
    p = N.new('ShaderNodeTexCoord').outputs['Object']
    v = N.maprange(N.noise(p, 3.0, 4.0, 0.55).outputs['Fac'], 0.3, 0.7, 0.92, 1.07)
    col = N.mix(1.0, srgb(0.33, 0.31, 0.29), N.comb(v, v, v), blend='MULTIPLY')
    nrm = N.bump(N.noise(p, 180.0, 3.0).outputs['Fac'], 0.15, 0.001)
    bsdf = N.principled(Base_Color=col, Roughness=0.82, Normal=nrm)
    N.output(bsdf.outputs[0])
    return mat


def bowl_material():
    mat, N = new_material("SpeckledStoneware")
    p = N.new('ShaderNodeTexCoord').outputs['Object']
    sp = N.voronoi(p, 260.0)
    speck = N.mul(N.maprange(sp.outputs['Distance'], 0.12, 0.05, smooth=True),
                  N.maprange(N.sep(sp.outputs['Color'])[0], 0.7, 0.75))
    col = N.mix(speck, srgb(0.80, 0.76, 0.69), srgb(0.30, 0.25, 0.21))
    bsdf = N.principled(Base_Color=col, Roughness=0.38, Coat_Weight=0.3, Coat_Roughness=0.15,
                        Normal=N.bump(N.noise(p, 90.0, 3.0).outputs['Fac'], 0.1, 0.001))
    N.output(bsdf.outputs[0])
    return mat


def lime_material():
    mat, N = new_material("LimePeel")
    p = N.new('ShaderNodeTexCoord').outputs['Object']
    var = N.noise(p, 2.5, 3.0).outputs['Fac']
    col = N.ramp(var, [(0.30, srgb(0.20, 0.40, 0.07)), (0.75, srgb(0.42, 0.60, 0.13))])
    pores = N.voronoi(p, 45.0).outputs['Distance']
    nrm = N.bump(N.add(pores, N.mul(N.noise(p, 30.0, 3.0).outputs['Fac'], 0.3)), 0.25, 0.002)
    bsdf = N.principled(Base_Color=col, Roughness=0.32, Subsurface_Weight=0.08,
                        Subsurface_Radius=(0.5, 1.0, 0.3), Subsurface_Scale=0.002, Normal=nrm)
    N.output(bsdf.outputs[0])
    return mat


def steam_material(seed):
    mat, N = new_material("Steam")
    co = N.new('ShaderNodeTexCoord').outputs['Object']        # -1..1 inside the steam box
    x, y, z = N.sep(co)
    zn = N.mul(N.add(z, 1.0), 0.5)
    warp = N.vmath('SUBTRACT', N.noise(N.mapping(co, loc=(seed, 0, 0), scale=(1.4, 1.4, 0.8)),
                                       1.0, 2.0).outputs['Color'], (0.5, 0.5, 0.5))
    cw = N.vmath('ADD', co, N.vmath('SCALE', warp, scale=0.55))
    wisp = N.maprange(N.noise(N.mapping(cw, scale=(2.3, 2.3, 0.9)), 1.0, 5.0, 0.55).outputs['Fac'],
                      0.52, 0.74, smooth=True)
    rad = N.vmath('LENGTH', N.comb(x, y, 0.0))
    fall_r = N.maprange(rad, 0.35, 0.95, 1.0, 0.0, smooth=True)
    fall_z = N.mul(N.maprange(zn, 0.0, 0.18, smooth=True), N.math('POWER', N.math('SUBTRACT', 1.0, zn), 2.0))
    dens = N.mul(N.mul(N.mul(wisp, fall_r), fall_z), 2.6)
    vol = N.new('ShaderNodeVolumePrincipled')
    vol.inputs['Color'].default_value = (0.95, 0.95, 0.95, 1.0)
    vol.inputs['Anisotropy'].default_value = 0.55
    N.feed(vol.inputs['Density'], dens)
    N.output(volume=vol.outputs[0])
    return mat


# ----------------------------------------------------------------------------------------------
# finishing the tortilla, scene dressing, lights, camera, render
# ----------------------------------------------------------------------------------------------

def finish_tortilla(obj, seed):
    co = mesh_coords(obj.data)
    cx = sum(v.x for v in co) / len(co)
    cy = sum(v.y for v in co) / len(co)
    rot = Matrix.Rotation(TORTILLA_YAW, 3, 'Z')
    moved = []
    for v in co:
        d = rot @ Vector((v.x - cx, v.y - cy, 0.0))
        moved.append(Vector((d.x + TORTILLA_POS.x, d.y + TORTILLA_POS.y, v.z)))
    set_coords(obj, moved)

    tex = bpy.data.textures.new("TortillaBubbles", 'CLOUDS')
    tex.noise_scale = 0.045
    tex.noise_depth = 2
    disp = obj.modifiers.new("Bubbles", 'DISPLACE')
    disp.texture = tex
    disp.texture_coords = 'UV'
    disp.uv_layer = "UVMap"
    disp.direction = 'NORMAL'
    disp.mid_level = 0.5
    disp.strength = 0.0007
    sol = obj.modifiers.new("Thickness", 'SOLIDIFY')
    sol.thickness = THICKNESS
    sol.offset = 0.0
    sol.use_even_offset = False      # 'even' thickness spikes at tight folds
    sol.use_quality_normals = True
    sol.use_rim = True
    sol.vertex_group = "thickness"
    sol.thickness_vertex_group = 0.0
    sol.material_offset = 1
    sub = obj.modifiers.new("Smooth", 'SUBSURF')
    sub.levels, sub.render_levels = 1, 2
    obj.data.materials.append(tortilla_material("TortillaSideA", seed))
    obj.data.materials.append(tortilla_material("TortillaSideB", seed + 101))

    # rest it on the board: lowest point of the final (solidified, smoothed) surface touches z=0
    bpy.context.view_layer.update()
    ev = evaluated_coords(obj)
    zmin = min(v.z for v in ev)
    obj.location.z = -zmin + 0.00015
    bpy.context.view_layer.update()
    log(f"tortilla finished: lowest surface point moved from {zmin * 1000:.2f} mm to 0.15 mm above "
        f"the board; final thickness {THICKNESS * 880:.2f}-{THICKNESS * 1000:.2f} mm")


def dress_scene(scene, seed, steam):
    board = bpy.data.objects["CuttingBoard"]
    board.data.materials.append(board_material())
    backdrop = build_backdrop()
    backdrop.data.materials.append(backdrop_material())
    bowl = build_bowl()
    bowl.location = (-0.15, 0.40, FLOOR_Z)
    bowl.data.materials.append(bowl_material())
    lmat = lime_material()
    for i, (x, y, rz) in enumerate(((0.13, 0.33, 0.4), (0.20, 0.43, 1.9))):
        lime = build_lime(f"Lime{i + 1}", seed + i)
        lime.location = (x, y, FLOOR_Z + 0.029)
        lime.rotation_euler = (0.0, 0.0, rz)
        lime.data.materials.append(lmat)
    if steam:
        tort = bpy.data.objects["Tortilla"]
        ev = evaluated_coords(tort)
        cx = sum(v.x for v in ev) / len(ev) + tort.location.x
        cy = sum(v.y for v in ev) / len(ev) + tort.location.y
        bm = bmesh.new()
        bmesh.ops.create_cube(bm, size=2.0)
        me = bpy.data.meshes.new("Steam")
        bm.to_mesh(me)
        bm.free()
        st = link_object(bpy.data.objects.new("Steam", me))
        st.location = (cx, cy + 0.01, 0.014 + 0.075)
        st.scale = (0.085, 0.085, 0.075)
        st.data.materials.append(steam_material(seed))
        st.visible_shadow = False
        log("steam volume added")
    log("scene dressed: board material, backdrop, bowl, two limes")


def add_area_light(name, target, direction, distance, size, irradiance, color=(1, 1, 1)):
    d = Vector(direction).normalized()
    loc = Vector(target) + d * distance
    ld = bpy.data.lights.new(name, 'AREA')
    ld.shape = 'RECTANGLE'
    ld.size, ld.size_y = size
    ld.energy = irradiance * math.pi * distance ** 2      # P = E * pi * d^2 for a Lambertian panel
    ld.color = color
    ob = link_object(bpy.data.objects.new(name, ld))
    ob.location = loc
    ob.rotation_euler = (Vector(target) - loc).to_track_quat('-Z', 'Y').to_euler()
    log(f"light {name}: {ld.energy:.2f} W at {distance:.2f} m, {size[0]:.2f}x{size[1]:.2f} m")
    return ob


def light_rig(center):
    c = Vector((center.x, center.y, 0.01))
    add_area_light("Key_Softbox", c, (-0.70, 0.55, 0.62), 0.75, (0.70, 0.50), 2.6, (1.0, 0.97, 0.92))
    add_area_light("Fill_Bounce", c, (0.65, -0.80, 0.35), 1.00, (1.20, 1.00), 0.45, (1.0, 0.99, 0.97))
    add_area_light("Rim_Back", c, (0.40, 1.00, 0.36), 0.55, (0.28, 0.22), 3.0, (1.0, 0.95, 0.88))
    wall = Vector((center.x - 0.05, 1.60, 0.25))
    add_area_light("Backdrop_Glow", wall, (0.0, -1.0, 0.45), 0.75, (0.9, 0.6), 1.8, (1.0, 0.93, 0.84))


def world_setup(scene):
    w = bpy.data.worlds.new("StudioWorld")
    scene.world = w
    w.use_nodes = True
    N = Nodes(w.node_tree)
    tc = N.new('ShaderNodeTexCoord')
    _, _, z = N.sep(tc.outputs['Generated'])
    col = N.ramp(N.maprange(z, -0.2, 0.8), [(0.0, srgb(0.20, 0.19, 0.18)), (1.0, srgb(0.34, 0.33, 0.32))])
    bg = N.new('ShaderNodeBackground')
    N.feed(bg.inputs['Color'], col)
    bg.inputs['Strength'].default_value = 0.35
    out = N.new('ShaderNodeOutputWorld')
    N.feed(out.inputs['Surface'], bg.outputs[0])
    log("procedural studio world (gradient, no HDRI)")


def camera_setup(scene):
    tort = bpy.data.objects["Tortilla"]
    ev = [v + tort.location for v in evaluated_coords(tort)]
    cx = sum(v.x for v in ev) / len(ev)
    cy = sum(v.y for v in ev) / len(ev)
    loc = Vector((cx, cy, 0.0)) + CAM_OFFSET
    aim = Vector((cx, cy, CAM_AIM_Z))
    cd = bpy.data.cameras.new("Camera")
    cd.lens = LENS_MM
    cd.sensor_width = 36.0
    cd.sensor_fit = 'HORIZONTAL'
    cd.clip_start = 0.01
    cam = link_object(bpy.data.objects.new("Camera", cd))
    cam.location = loc
    cam.rotation_euler = (aim - loc).to_track_quat('-Z', 'Y').to_euler()
    scene.camera = cam
    bpy.context.view_layer.update()

    fwd = (cam.matrix_world.to_3x3() @ Vector((0, 0, -1))).normalized()
    depth = [(v - loc).dot(fwd) for v in ev]
    dmin = min(depth)
    front = [v for v, d in zip(ev, depth) if d < dmin + 0.008]
    fpt = sum(front, Vector()) / len(front)
    fdist = sum(d for d in depth if d < dmin + 0.008) / len(front)
    focus = link_object(bpy.data.objects.new("FocusTarget", None))
    focus.location = fpt
    focus.empty_display_size = 0.01
    cd.dof.use_dof = True
    cd.dof.focus_object = focus
    cd.dof.aperture_fstop = FSTOP
    cd.dof.aperture_blades = 0
    log(f"camera: {LENS_MM:.0f} mm f/{FSTOP}, at {tuple(round(c, 3) for c in loc)}, "
        f"pitch {math.degrees(math.asin(-fwd.z)):.1f} deg down, focus on leading fold "
        f"{fdist * 100:.1f} cm away ({len(front)} front verts)")
    return cam, dict(cam_loc=list(loc), focus=list(fpt), focus_dist=fdist)


def projection_report(scene, cam):
    from bpy_extras.object_utils import world_to_camera_view
    out = {}
    tort = bpy.data.objects["Tortilla"]
    pts = [v + tort.location for v in evaluated_coords(tort)]
    pr = [world_to_camera_view(scene, cam, v) for v in pts]
    out["tortilla_frame_bbox"] = [min(p.x for p in pr), min(p.y for p in pr),
                                  max(p.x for p in pr), max(p.y for p in pr)]
    for name in ("Bowl", "Lime1", "Lime2", "CuttingBoard"):
        ob = bpy.data.objects[name]
        cs = [ob.matrix_world @ Vector(c) for c in ob.bound_box]
        pr = [world_to_camera_view(scene, cam, c) for c in cs]
        out[name + "_frame_bbox"] = [min(p.x for p in pr), min(p.y for p in pr),
                                     max(p.x for p in pr), max(p.y for p in pr)]
    for k, v in out.items():
        log(f"  frame coverage {k}: x {v[0]:.2f}..{v[2]:.2f}, y {v[1]:.2f}..{v[3]:.2f}")
    return out


def setup_render(scene, args):
    scene.render.engine = 'CYCLES'
    device = 'CPU'
    names = []
    try:
        prefs = bpy.context.preferences.addons['cycles'].preferences
        for backend in ('OPTIX', 'CUDA', 'HIP', 'ONEAPI', 'METAL'):
            try:
                prefs.compute_device_type = backend
            except TypeError:
                continue
            try:
                prefs.refresh_devices()
            except AttributeError:
                prefs.get_devices()
            devs = [d for d in prefs.devices if d.type == backend]
            if devs:
                for d in prefs.devices:
                    d.use = d.type == backend
                device = backend
                names = [d.name for d in devs]
                break
        if device == 'CPU':
            prefs.compute_device_type = 'NONE'
    except Exception as exc:
        log(f"GPU setup failed ({exc!r}); falling back to CPU")
        device = 'CPU'
    scene.cycles.device = 'GPU' if device != 'CPU' else 'CPU'
    log(f"render device: {device} {names or ''} (cpu threads: {os.cpu_count()})")

    c = scene.cycles
    c.samples = args.samples
    c.use_adaptive_sampling = True
    c.adaptive_threshold = 0.006
    c.adaptive_min_samples = 96
    c.use_denoising = True
    c.denoiser = 'OPENIMAGEDENOISE'
    c.denoising_input_passes = 'RGB_ALBEDO_NORMAL'
    c.denoising_prefilter = 'ACCURATE'
    if hasattr(c, "denoising_use_gpu"):
        c.denoising_use_gpu = device != 'CPU'
    c.seed = args.seed
    c.use_animated_seed = False
    c.max_bounces = 12
    c.diffuse_bounces = 4
    c.glossy_bounces = 4
    c.transmission_bounces = 8
    c.volume_bounces = 2
    c.transparent_max_bounces = 8
    c.caustics_reflective = False
    c.caustics_refractive = False
    c.blur_glossy = 1.0
    c.volume_step_rate = 1.0
    c.volume_max_steps = 512

    r = scene.render
    r.resolution_x, r.resolution_y, r.resolution_percentage = args.width, args.height, 100
    r.film_transparent = False
    r.image_settings.file_format = 'PNG'
    r.image_settings.color_mode = 'RGB'
    r.image_settings.color_depth = '16'
    r.image_settings.compression = 15
    vs = scene.view_settings
    vs.view_transform = 'AgX'
    try:
        vs.look = 'AgX - Medium High Contrast'
    except TypeError:
        vs.look = 'None'
    vs.exposure = 0.0
    vs.gamma = 1.0
    scene.display_settings.display_device = 'sRGB'
    log(f"Cycles {args.width}x{args.height}, {args.samples} max samples, adaptive 0.006, OIDN, "
        f"AgX ({vs.look}), seed {args.seed}")
    return device, names


# ----------------------------------------------------------------------------------------------
# main
# ----------------------------------------------------------------------------------------------

def main():
    args = parse_args()
    log(f"Blender {bpy.app.version_string} | Python {platform.python_version()} | "
        f"{platform.system()} {platform.machine()} | output dir {args.output_dir}")
    random.seed(args.seed)
    noise.seed_set(args.seed)
    scene = reset_scene()

    tortilla, rest = build_tortilla(args.seed)
    build_board()

    t_sim = time.time()
    stage_stats, snaps = simulate_folds(scene, tortilla, args.seed, rest, args.stages)
    t_sim = time.time() - t_sim
    final_cloth = mesh_coords(tortilla.data)
    layers = layer_report(final_cloth, rest)
    log(f"cloth done in {t_sim:.1f}s; layer check: {layers}")

    out = args.output_dir
    write_obj(os.path.join(out, "sim_final_cloth.obj"), final_cloth, tortilla.data)
    write_obj(os.path.join(out, "sim_rest.obj"), rest, tortilla.data)
    for k, co in snaps.items():
        write_obj(os.path.join(out, f"sim_{k}.obj"), co, tortilla.data)

    finish_tortilla(tortilla, args.seed)
    dg = bpy.context.evaluated_depsgraph_get()
    ev = tortilla.evaluated_get(dg)
    em = ev.to_mesh()
    write_obj(os.path.join(out, "final_tortilla_surface.obj"),
              [v.co + tortilla.location for v in em.vertices], em)
    ev.to_mesh_clear()
    dress_scene(scene, args.seed, steam=not args.no_steam)
    world_setup(scene)
    light_rig(Vector((TORTILLA_POS.x, TORTILLA_POS.y, 0.0)))
    cam, cam_info = camera_setup(scene)
    proj = projection_report(scene, cam)
    device, gpus = setup_render(scene, args)
    scene.frame_set(1)

    stats = dict(blender=bpy.app.version_string, seed=args.seed, sim_seconds=t_sim, stages=stage_stats,
                 layers=layers, camera=cam_info, frame=proj, device=device, gpus=gpus,
                 cpu_threads=os.cpu_count(), cloth=CLOTH)
    with open(os.path.join(out, "sim_stats.json"), "w") as f:
        json.dump(stats, f, indent=2, default=str)

    blend_path = os.path.join(out, "tortilla_scene.blend")
    bpy.ops.wm.save_as_mainfile(filepath=blend_path, compress=True, copy=True)
    log(f"saved {blend_path}")

    if args.bake_only:
        log("bake-only run: skipping render")
    else:
        img = os.path.join(out, "tortilla_render.png")
        scene.render.filepath = img
        t_r = time.time()
        log("rendering ...")
        bpy.ops.render.render(write_still=True)
        if not os.path.isfile(img):
            raise RuntimeError("render finished but no image was written")
        log(f"render done in {time.time() - t_r:.1f}s -> {img}")
    log(f"TOTAL RUNTIME {time.time() - T_START:.1f}s on {device} {gpus}")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        log("FAILED")
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(1)
