#!/usr/bin/env python3
"""
build_tortilla.py  --  PROCEDURAL-PHOTOREAL-01
Headless end-to-end construction of a freshly cooked flour tortilla, folded into
quarters by a baked cloth simulation, resting on an oiled wooden cutting board.

Run (single command, no GUI, no external assets):

    blender --background --factory-startup --python build_tortilla.py

Optional arguments after "--":
    --out-dir DIR        output directory (default: <script dir>/output)
    --res W H            render resolution (default 3840 2160)
    --samples N          Cycles max samples (default 512)
    --bake-only          build + bake + freeze + save .blend, but do NOT render
    --cpu                force CPU rendering
    --dump PATH          write cloth vertex positions of key frames to a JSON file
    --set KEY=VALUE      override a cloth / collision setting (tuning aid)
    --seed N             global seed (default 1337)

Pipeline: empty scene -> board + backdrop -> tortilla grid mesh -> hook-driven pin
animation -> Cloth bake (two successive folds) -> freeze final frame ->
solidify/subsurf -> procedural materials -> lights + camera (DOF) -> Cycles.
Only bpy / bmesh / mathutils and the Python standard library are used.
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

import bpy
import bmesh
from mathutils import Vector, Matrix, noise
from mathutils.bvhtree import BVHTree

T0 = time.time()


def log(msg):
    print("[%7.1fs] %s" % (time.time() - T0, msg), flush=True)


# --------------------------------------------------------------------------- #
# Configuration (metres, seconds)
# --------------------------------------------------------------------------- #
SEED = 1337
TORT_RADIUS = 0.115           # 23 cm diameter
GRID_M = 32                   # half-resolution of the cloth grid  -> 89 x 89 verts (~2.6 mm)
SIM_SCALE = 6.0               # cloth is simulated at 6x size (stable collision distances), scaled back after the bake
CX, CY = 0.045, 0.045         # centre of the flat tortilla; folded quarter ends up around the origin
Z0 = 0.003                    # initial height of the flat sheet above the board
H1, H2 = 0.0045, 0.0085        # heights of the two fold axes (control layer separation)
THICK = 0.0021                # nominal tortilla thickness (solidify)
THICK_MIN_FACTOR = 0.72       # thinnest areas = 68 % of nominal  -> ~1.8 .. 2.6 mm

BASE_HOLD = 0.3
FPS = 24
F_START = 1
F_FOLD1_END = 46
F_FOLD2_START = 68
F_FOLD2_END = 114
F_END = 170

BOARD_L, BOARD_W, BOARD_T = 0.50, 0.32, 0.024   # cutting board, top surface at z = 0

CLOTH = dict(quality=8, mass=0.3, air_damping=1.0,
             tension_stiffness=60.0, compression_stiffness=60.0,
             shear_stiffness=40.0, bending_stiffness=180.0,
             tension_damping=5.0, compression_damping=5.0,
             shear_damping=5.0, bending_damping=1.0,
             pin_stiffness=1.0)
COLL = dict(distance_min=0.004, collision_quality=3, self_distance_min=0.006,
            self_friction=5.0, self_collision=1, self_impulse_clamp=0.0)


def smoothstep(a, b, x):
    t = max(0.0, min(1.0, (x - a) / (b - a)))
    return t * t * (3.0 - 2.0 * t)


# --------------------------------------------------------------------------- #
# Arguments
# --------------------------------------------------------------------------- #
def parse_args():
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    try:
        base = os.path.dirname(os.path.abspath(__file__))
    except NameError:
        base = os.getcwd()
    ap = argparse.ArgumentParser(prog="build_tortilla.py")
    ap.add_argument("--out-dir", default=os.path.join(base, "output"))
    ap.add_argument("--res", nargs=2, type=int, default=[3840, 2160])
    ap.add_argument("--samples", type=int, default=512)
    ap.add_argument("--bake-only", action="store_true")
    ap.add_argument("--cpu", action="store_true")
    ap.add_argument("--dump", default=None)
    ap.add_argument("--set", action="append", default=[])
    ap.add_argument("--f-end", type=int, default=None)
    ap.add_argument("--seed", type=int, default=SEED)
    a = ap.parse_args(argv)
    a.out_dir = os.path.abspath(a.out_dir)
    return a


# --------------------------------------------------------------------------- #
# Scene helpers
# --------------------------------------------------------------------------- #
def reset_scene():
    """Start from a truly empty scene: delete every object and orphan datablock."""
    for o in list(bpy.data.objects):
        bpy.data.objects.remove(o, do_unlink=True)
    for coll in (bpy.data.meshes, bpy.data.materials, bpy.data.lights,
                 bpy.data.cameras, bpy.data.curves, bpy.data.images):
        for d in list(coll):
            coll.remove(d)
    sc = bpy.context.scene
    sc.unit_settings.system = 'METRIC'
    sc.unit_settings.scale_length = 1.0
    sc.render.fps = FPS
    sc.frame_start, sc.frame_end = F_START, F_END
    sc.gravity = (0.0, 0.0, -9.81)
    return sc


def link_obj(obj, coll_name="Scene"):
    sc = bpy.context.scene
    coll = bpy.data.collections.get(coll_name)
    if coll is None:
        coll = bpy.data.collections.new(coll_name)
        sc.collection.children.link(coll)
    coll.objects.link(obj)
    return obj


def aim(obj, target):
    d = Vector(target) - obj.location
    obj.rotation_euler = d.to_track_quat('-Z', 'Y').to_euler()


def smooth_shade(mesh, angle_deg=None):
    mesh.polygons.foreach_set("use_smooth", [True] * len(mesh.polygons))
    if angle_deg is not None:
        try:
            mesh.set_sharp_from_angle(angle=math.radians(angle_deg))
        except Exception as e:  # pragma: no cover
            log("WARN set_sharp_from_angle failed: %s" % e)
    mesh.update()


# --------------------------------------------------------------------------- #
# Geometry: cutting board, backdrop
# --------------------------------------------------------------------------- #
def build_board():
    log("Building cutting board")
    bm = bmesh.new()
    bmesh.ops.create_cube(bm, size=1.0)
    for v in bm.verts:
        v.co.x *= BOARD_L
        v.co.y *= BOARD_W
        v.co.z = v.co.z * BOARD_T - BOARD_T * 0.5   # top face at z = 0
    bmesh.ops.bevel(bm, geom=list(bm.verts) + list(bm.edges) + list(bm.faces),
                    offset=0.0045, offset_type='OFFSET', segments=4, profile=0.55,
                    affect='EDGES')
    me = bpy.data.meshes.new("BoardMesh")
    bm.to_mesh(me)
    bm.free()
    smooth_shade(me, 30)
    ob = link_obj(bpy.data.objects.new("CuttingBoard", me))
    return ob


def build_sim_floor():
    """Invisible collider (plane at z=0) used only by the scaled-up cloth simulation."""
    bm = bmesh.new()
    bmesh.ops.create_grid(bm, x_segments=1, y_segments=1, size=3.0)
    me = bpy.data.meshes.new("SimFloorMesh")
    bm.to_mesh(me)
    bm.free()
    ob = link_obj(bpy.data.objects.new("SimFloor", me), "Simulation")
    ob.modifiers.new("Collision", 'COLLISION')
    c = ob.collision
    c.thickness_outer = 0.006
    c.thickness_inner = 0.006
    c.cloth_friction = 80.0
    c.damping = 0.4
    c.use_culling = False
    ob.hide_render = True
    return ob


def build_backdrop():
    """Seamless cyclorama (floor curving up into a wall) - clean minimal background."""
    log("Building backdrop")
    zf = -BOARD_T
    prof = [(1.6, zf), (0.2, zf), (-0.9, zf)]
    rad = 0.55
    for k in range(1, 13):
        a = k / 12.0 * math.pi * 0.5
        prof.append((-0.9 - math.sin(a) * rad, zf + rad - math.cos(a) * rad))
    prof.append((-0.9 - rad, zf + rad + 1.6))
    bm = bmesh.new()
    xs = (-2.0, 2.0)
    rows = []
    for (y, z) in prof:
        rows.append([bm.verts.new((x, y, z)) for x in xs])
    for i in range(len(rows) - 1):
        bm.faces.new((rows[i][0], rows[i][1], rows[i + 1][1], rows[i + 1][0]))
    bmesh.ops.recalc_face_normals(bm, faces=bm.faces[:])
    me = bpy.data.meshes.new("BackdropMesh")
    bm.to_mesh(me)
    bm.free()
    smooth_shade(me)
    return link_obj(bpy.data.objects.new("Backdrop", me))


# --------------------------------------------------------------------------- #
# Geometry: tortilla cloth mesh
# --------------------------------------------------------------------------- #
def build_tortilla_sim(rng):
    """Flat, irregular disc (grid mapped to a disc -> all quads) + vertex groups
    (F1/F2 = the two flaps, Pin = pinned outer strips, Thick = thickness map)."""
    log("Building tortilla cloth mesh (%dx%d grid)" % (2 * GRID_M + 1, 2 * GRID_M + 1))
    m = GRID_M
    n = 2 * m + 1
    freqs = [2, 3, 5, 8, 13]
    amps = [0.011, 0.008, 0.005, 0.003, 0.002]
    phs = [rng.uniform(0, 2 * math.pi) for _ in freqs]
    noise.seed_set(SEED)

    def irregular(th):
        return sum(a * math.sin(f * th + p) for a, f, p in zip(amps, freqs, phs))

    bm = bmesh.new()
    g = {}
    for j in range(n):
        for i in range(n):
            u, v = (i - m) / m, (j - m) / m
            x = u * math.sqrt(max(0.0, 1.0 - v * v / 2.0))   # square -> disc
            y = v * math.sqrt(max(0.0, 1.0 - u * u / 2.0))
            r = math.hypot(x, y)
            s = 1.0 + irregular(math.atan2(y, x)) * smoothstep(0.55, 1.0, r)
            g[i, j] = bm.verts.new(((CX + x * TORT_RADIUS * s) * SIM_SCALE, (CY + y * TORT_RADIUS * s) * SIM_SCALE, Z0 * SIM_SCALE))
    for j in range(n - 1):
        for i in range(n - 1):
            bm.faces.new((g[i, j], g[i + 1, j], g[i + 1, j + 1], g[i, j + 1]))
    bm.verts.ensure_lookup_table()
    uv = bm.loops.layers.uv.new("UVMap")
    for f in bm.faces:
        for l in f.loops:
            l[uv].uv = (l.vert.co.x / SIM_SCALE - CX, l.vert.co.y / SIM_SCALE - CY)   # rest-pose UVs in metres
    me = bpy.data.meshes.new("TortillaSimMesh")
    bm.to_mesh(me)
    bm.free()
    smooth_shade(me)
    ob = link_obj(bpy.data.objects.new("TortillaSim", me), "Simulation")

    vg_f1 = ob.vertex_groups.new(name="F1")
    vg_f2 = ob.vertex_groups.new(name="F2")
    vg_pin = ob.vertex_groups.new(name="Pin")
    vg_th = ob.vertex_groups.new(name="Thick")
    thick_w = []
    for j in range(n):
        for i in range(n):
            idx = j * n + i
            co = me.vertices[idx].co
            dx = (co.x / SIM_SCALE - CX) / TORT_RADIUS
            dy = (co.y / SIM_SCALE - CY) / TORT_RADIUS
            if i > m:
                vg_f1.add([idx], 1.0, 'REPLACE')
            if j > m:
                vg_f2.add([idx], 1.0, 'REPLACE')
            w1 = smoothstep(0.42, 0.80, dx) if i > m else 0.0
            w2 = smoothstep(0.42, 0.80, dy) if j > m else 0.0
            pw = max(w1, w2)
            if i <= m and j <= m:
                pw = BASE_HOLD          # static quadrant: softly held so it does not slide/buckle
            if pw > 0.0:
                vg_pin.add([idx], pw, 'REPLACE')
            # non-uniform thickness: low-frequency blotches, slightly thinner toward the rim
            nz = noise.fractal((co.x / SIM_SCALE * 22, co.y / SIM_SCALE * 22, 3.7), 0.5, 2.0, 3)
            nz2 = noise.noise((co.x / SIM_SCALE * 6, co.y / SIM_SCALE * 6, 11.3))
            r = math.hypot(dx, dy)
            w = 0.62 + 0.30 * nz + 0.20 * nz2 - 0.18 * smoothstep(0.75, 1.0, r)
            w = max(0.0, min(1.0, w))
            thick_w.append(w)
            vg_th.add([idx], w, 'REPLACE')
    ob["thick_weights"] = thick_w   # kept for the frozen mesh
    return ob


def add_fold_animation(sim):
    """Two successive folds: hook empties (animated pin targets) rotate the flap groups.
    Fold 1: half x>CX flips over the fold line x=CX (rotation about Y).
    Fold 2: half y>CY flips over the fold line y=CY (rotation about X)."""
    log("Adding hook-driven fold animation (2 successive folds)")
    sc = bpy.context.scene
    e1 = bpy.data.objects.new("Fold1_Axis", None)
    e1.location = (CX * SIM_SCALE, CY * SIM_SCALE, H1 * SIM_SCALE)
    e2 = bpy.data.objects.new("Fold2_Axis", None)
    e2.location = (CX * SIM_SCALE, CY * SIM_SCALE, H2 * SIM_SCALE)
    for e in (e1, e2):
        e.empty_display_type = 'ARROWS'
        e.empty_display_size = 0.08
        link_obj(e, "Simulation")
    sc.frame_set(F_START)
    for e, grp in ((e1, "F1"), (e2, "F2")):
        h = sim.modifiers.new("Hook_" + grp, 'HOOK')
        h.object = e
        h.vertex_group = grp
        h.strength = 1.0
        h.falloff_type = 'NONE'
        h.matrix_inverse = Matrix.Translation(e.location).inverted()
    # keyframes
    def key(e, axis, frame, val):
        e.rotation_euler[axis] = val
        e.keyframe_insert("rotation_euler", index=axis, frame=frame)
    key(e1, 1, F_START, 0.0)
    key(e1, 1, F_FOLD1_END, -math.pi)
    key(e2, 0, F_START, 0.0)
    key(e2, 0, F_FOLD2_START, 0.0)
    key(e2, 0, F_FOLD2_END, math.pi)
    for e in (e1, e2):
        e.rotation_euler = (0, 0, 0)
    return e1, e2


def add_cloth(sim, overrides):
    log("Configuring cloth")
    cm = sim.modifiers.new("Cloth", 'CLOTH')
    s = cm.settings
    cfg = dict(CLOTH)
    coll = dict(COLL)
    for kv in overrides:
        k, v = kv.split("=")
        (cfg if k in cfg else coll)[k] = float(v)
    s.quality = int(cfg["quality"])
    s.mass = cfg["mass"]
    s.air_damping = cfg["air_damping"]
    s.bending_model = 'ANGULAR'
    s.tension_stiffness = cfg["tension_stiffness"]
    s.compression_stiffness = cfg["compression_stiffness"]
    s.shear_stiffness = cfg["shear_stiffness"]
    s.bending_stiffness = cfg["bending_stiffness"]
    s.tension_damping = cfg["tension_damping"]
    s.compression_damping = cfg["compression_damping"]
    s.shear_damping = cfg["shear_damping"]
    s.bending_damping = cfg["bending_damping"]
    s.vertex_group_mass = "Pin"
    s.pin_stiffness = cfg["pin_stiffness"]
    cs = cm.collision_settings
    cs.use_collision = True
    cs.distance_min = coll["distance_min"]
    cs.collision_quality = int(coll["collision_quality"])
    cs.use_self_collision = bool(coll["self_collision"])
    cs.self_distance_min = coll["self_distance_min"]
    cs.self_friction = coll["self_friction"]
    cs.self_impulse_clamp = coll["self_impulse_clamp"]
    pc = cm.point_cache
    pc.frame_start, pc.frame_end = F_START, F_END
    log("  cloth=%s" % json.dumps(cfg))
    log("  collision=%s" % json.dumps(coll))
    return cm


# --------------------------------------------------------------------------- #
# Simulation, validation, freeze
# --------------------------------------------------------------------------- #
def mesh_positions(obj):
    dg = bpy.context.evaluated_depsgraph_get()
    ev = obj.evaluated_get(dg)
    me = ev.to_mesh()
    pts = [tuple(v.co) for v in me.vertices]
    ev.to_mesh_clear()
    return pts


def bake(sim, cm, dump_frames, dump):
    sc = bpy.context.scene
    log("Baking cloth (frames %d-%d, %d fps)" % (F_START, F_END, FPS))
    t = time.time()
    sc.frame_set(F_START)
    for f in range(F_START, F_END + 1):
        sc.frame_set(f)
        if f % 10 == 0:
            log("  simulated frame %d (%.1f s)" % (f, time.time() - t))
    log("Cloth bake done in %.1f s" % (time.time() - t))
    for f in dump_frames:
        sc.frame_set(f)
        dump[str(f)] = mesh_positions(sim)
    sc.frame_set(F_END)


def report_mesh(name, pts, sim_mesh_edges, rest):
    zs = [p[2] for p in pts]
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    ratios = []
    for a, b in sim_mesh_edges:
        d = (Vector(pts[a]) - Vector(pts[b])).length
        ratios.append(d / max(1e-9, rest[(a, b)]))
    ratios.sort()
    log("  %s bbox x[%.4f,%.4f] y[%.4f,%.4f] z[%.4f,%.4f]" %
        (name, min(xs), max(xs), min(ys), max(ys), min(zs), max(zs)))
    log("  edge length ratio mean %.3f  p1 %.3f  p99 %.3f  max %.3f" %
        (sum(ratios) / len(ratios), ratios[len(ratios) // 100],
         ratios[len(ratios) * 99 // 100], ratios[-1]))


def count_self_intersections(pts, quads):
    tris = []
    for q in quads:
        tris.append((q[0], q[1], q[2]))
        tris.append((q[0], q[2], q[3]))
    tree = BVHTree.FromPolygons([Vector(p) for p in pts], tris, all_triangles=True)
    bad = 0
    for a, b in tree.overlap(tree):
        if a < b and not (set(tris[a]) & set(tris[b])):
            bad += 1
    return bad


def freeze_and_thicken(sim):
    """Take the baked final frame, add thickness, remove board interpenetration."""
    sc = bpy.context.scene
    sc.frame_set(F_END)
    dg = bpy.context.evaluated_depsgraph_get()
    ev = sim.evaluated_get(dg)
    cloth_me = bpy.data.meshes.new_from_object(ev, depsgraph=dg)
    cloth_me.name = "TortillaFoldedSurface"
    cloth_me.transform(Matrix.Scale(1.0 / SIM_SCALE, 4))
    pts = [tuple(v.co) for v in cloth_me.vertices]
    src = sim.data
    edges = [tuple(e.vertices) for e in src.edges]
    rest = {e: (src.vertices[e[0]].co - src.vertices[e[1]].co).length / SIM_SCALE for e in edges}
    quads = [tuple(p.vertices) for p in src.polygons]
    log("Frozen cloth surface at frame %d:" % F_END)
    report_mesh("cloth", pts, edges, rest)
    # repair isolated stretched-out vertices (cloth solver outliers) before thickening
    nbrs = {}
    for a_, b_ in edges:
        nbrs.setdefault(a_, []).append(b_)
        nbrs.setdefault(b_, []).append(a_)
    fixed = 0
    for _ in range(4):
        bad = set()
        for (a_, b_) in edges:
            d = (Vector(pts[a_]) - Vector(pts[b_])).length
            if d > 1.5 * rest[(a_, b_)]:
                bad.update((a_, b_))
        if not bad:
            break
        for v in bad:
            ns = [Vector(pts[k]) for k in nbrs[v] if k not in bad]
            if ns:
                c = sum(ns, Vector()) / len(ns)
                pts[v] = tuple(c)
                cloth_me.vertices[v].co = c
                fixed += 1
    log("  repaired %d outlier vertices" % fixed)
    n_int = count_self_intersections(pts, quads)
    log("  self-intersecting triangle pairs: %d" % n_int)
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    if max(xs) > CX + 0.02 or max(ys) > CY + 0.02 or max(xs) - min(xs) > TORT_RADIUS * 1.3 \
            or max(ys) - min(ys) > TORT_RADIUS * 1.3:
        raise RuntimeError("Cloth result is not a folded quarter (bake failed?)")
    if max(p[2] for p in pts) > 0.03:
        raise RuntimeError("Cloth result is implausibly tall (sim exploded?)")

    # thickness weights -> solidify
    tmp_me = cloth_me
    tmp = bpy.data.objects.new("TortillaTmp", tmp_me)
    link_obj(tmp, "Simulation")
    vg = tmp.vertex_groups.new(name="Thick")
    for i, w in enumerate(sim["thick_weights"]):
        vg.add([i], w, 'REPLACE')
    sol = tmp.modifiers.new("Solidify", 'SOLIDIFY')
    sol.thickness = THICK
    sol.offset = 0.0
    sol.use_even_offset = False
    sol.use_rim = True
    sol.use_quality_normals = True
    sol.vertex_group = "Thick"
    sol.thickness_vertex_group = THICK_MIN_FACTOR
    dg = bpy.context.evaluated_depsgraph_get()
    thick_me = bpy.data.meshes.new_from_object(tmp.evaluated_get(dg), depsgraph=dg)
    thick_me.name = "TortillaMesh"
    bpy.data.objects.remove(tmp, do_unlink=True)
    bpy.data.meshes.remove(tmp_me)

    zmin = min(v.co.z for v in thick_me.vertices)
    if zmin > 0.0:
        for v in thick_me.vertices:
            v.co.z -= zmin - 0.00005     # seat the lowest point on the board (no floating)
    clipped = 0
    for v in thick_me.vertices:
        if v.co.z < 0.0:
            v.co.z = 0.0            # remove board interpenetration (deterministic contact)
            clipped += 1
    log("Solidified: min z before contact clamp %.5f m, %d verts clamped to board plane" % (zmin, clipped))
    if max(zs_ for zs_ in (v.co.z for v in thick_me.vertices)) > 0.03:
        raise RuntimeError("Solidified mesh has spikes")
    smooth_shade(thick_me)
    ob = link_obj(bpy.data.objects.new("Tortilla", thick_me))
    sub = ob.modifiers.new("Subsurf", 'SUBSURF')
    sub.levels = 1
    sub.render_levels = 2
    sub.subdivision_type = 'CATMULL_CLARK'
    sim.hide_render = True    # keep the baked simulation in the .blend for inspection only
    sim.hide_set(True)
    for o in bpy.data.objects:
        if o.name.startswith("Fold"):
            o.hide_render = True
    zs = [v.co.z for v in thick_me.vertices]
    log("  final tortilla: %d verts, height range [%.4f, %.4f]" % (len(zs), min(zs), max(zs)))
    return ob, cloth_me


# --------------------------------------------------------------------------- #
# Shader helpers
# --------------------------------------------------------------------------- #
def new_material(name):
    mat = bpy.data.materials.new(name)
    mat.use_nodes = True
    nt = mat.node_tree
    nt.nodes.clear()
    return mat, nt


def node(nt, typ, **props):
    n = nt.nodes.new(typ)
    for k, v in props.items():
        setattr(n, k, v)
    return n


def sock(n, name, kind='inputs'):
    return getattr(n, kind)[name]


def link(nt, out, inp):
    nt.links.new(out, inp)


def setin(n, names, value):
    if isinstance(names, str):
        names = [names]
    for nm in names:
        if nm in n.inputs:
            n.inputs[nm].default_value = value
            return True
    log("WARN: no input %s on %s" % (names, n.name))
    return False


def math_node(nt, op, a=None, b=None, clamp=False):
    n = node(nt, 'ShaderNodeMath', operation=op, use_clamp=clamp)
    for i, v in enumerate((a, b)):
        if v is None:
            continue
        if isinstance(v, (int, float)):
            n.inputs[i].default_value = v
        else:
            link(nt, v, n.inputs[i])
    return n.outputs[0]


def vmath(nt, op, a, b=None, scale=None):
    n = node(nt, 'ShaderNodeVectorMath', operation=op)
    for i, v in enumerate((a, b)):
        if v is None:
            continue
        if isinstance(v, (tuple, list)):
            n.inputs[i].default_value = v
        else:
            link(nt, v, n.inputs[i])
    if scale is not None:
        n.inputs['Scale'].default_value = scale
    return n.outputs['Vector'] if op not in ('LENGTH', 'DOT_PRODUCT') else n.outputs['Value']


def map_range(nt, val, fmin, fmax, tmin=0.0, tmax=1.0, clamp=True):
    n = node(nt, 'ShaderNodeMapRange', data_type='FLOAT', clamp=clamp)
    for name, v in (("Value", val), ("From Min", fmin), ("From Max", fmax),
                    ("To Min", tmin), ("To Max", tmax)):
        if isinstance(v, (int, float)):
            n.inputs[name].default_value = v
        else:
            link(nt, v, n.inputs[name])
    return n.outputs['Result']


def ramp(nt, fac, stops, interp='LINEAR'):
    n = node(nt, 'ShaderNodeValToRGB')
    n.color_ramp.interpolation = interp
    els = n.color_ramp.elements
    while len(els) < len(stops):
        els.new(0.5)
    for e, (pos, col) in zip(els, stops):
        e.position = pos
        e.color = col
    link(nt, fac, n.inputs['Fac'])
    return n.outputs['Color']


def mix_color(nt, fac, a, b):
    n = node(nt, 'ShaderNodeMix', data_type='RGBA', blend_type='MIX')
    if isinstance(fac, (int, float)):
        n.inputs[0].default_value = fac
    else:
        link(nt, fac, n.inputs[0])
    for idx, v in ((6, a), (7, b)):
        if isinstance(v, tuple):
            n.inputs[idx].default_value = v
        else:
            link(nt, v, n.inputs[idx])
    return n.outputs[2]


def noise_tex(nt, vec, scale, detail=2.0, rough=0.5, distortion=0.0, out='Fac'):
    n = node(nt, 'ShaderNodeTexNoise', noise_dimensions='3D')
    n.inputs['Scale'].default_value = scale
    n.inputs['Detail'].default_value = detail
    n.inputs['Roughness'].default_value = rough
    n.inputs['Distortion'].default_value = distortion
    if vec is not None:
        link(nt, vec, n.inputs['Vector'])
    return n.outputs[out]


def mapping(nt, vec, loc=(0, 0, 0), rot=(0, 0, 0), scale=(1, 1, 1)):
    n = node(nt, 'ShaderNodeMapping', vector_type='POINT')
    n.inputs['Location'].default_value = loc
    n.inputs['Rotation'].default_value = rot
    n.inputs['Scale'].default_value = scale
    link(nt, vec, n.inputs['Vector'])
    return n.outputs['Vector']


def principled(nt):
    return node(nt, 'ShaderNodeBsdfPrincipled')


# --------------------------------------------------------------------------- #
# Materials
# --------------------------------------------------------------------------- #
def tortilla_material():
    log("Building tortilla material")
    mat, nt = new_material("TortillaFlour")
    out = node(nt, 'ShaderNodeOutputMaterial')
    bsdf = principled(nt)
    link(nt, bsdf.outputs['BSDF'], out.inputs['Surface'])
    uvn = node(nt, 'ShaderNodeUVMap', uv_map="UVMap")
    uv = uvn.outputs['UV']

    # domain-warped coordinates so spots are organic, not perfectly round
    wn = noise_tex(nt, mapping(nt, uv, scale=(1, 1, 1)), 38.0, 3.0, 0.55, out='Color')
    warp = vmath(nt, 'SCALE', vmath(nt, 'SUBTRACT', wn, (0.5, 0.5, 0.5)), scale=0.014)
    wuv = vmath(nt, 'ADD', uv, warp)

    def spots(scale, presence, rmin, rmax):
        vor = node(nt, 'ShaderNodeTexVoronoi', feature='F1', voronoi_dimensions='3D')
        vor.inputs['Scale'].default_value = scale
        vor.inputs['Randomness'].default_value = 1.0
        link(nt, wuv, vor.inputs['Vector'])
        sep = node(nt, 'ShaderNodeSeparateColor')
        link(nt, vor.outputs['Color'], sep.inputs['Color'])
        rad = map_range(nt, sep.outputs['Red'], 0.0, 1.0, rmin, rmax)
        inner = math_node(nt, 'MULTIPLY', rad, 0.15)
        core = map_range(nt, vor.outputs['Distance'], rad, inner, 0.0, 1.0)
        pres = math_node(nt, 'GREATER_THAN', sep.outputs['Green'], presence)
        return math_node(nt, 'MULTIPLY', core, pres)

    big = spots(18.0, 0.55, 0.20, 0.60)
    med = spots(42.0, 0.48, 0.14, 0.48)
    small = spots(100.0, 0.60, 0.12, 0.42)
    cluster = map_range(nt, noise_tex(nt, mapping(nt, uv, loc=(3, 1, 0)), 7.0, 2.0),
                        0.30, 0.65, 0.45, 1.0)
    allspots = math_node(nt, 'MAXIMUM',
                         math_node(nt, 'MAXIMUM', big, math_node(nt, 'MULTIPLY', med, 0.85)),
                         math_node(nt, 'MULTIPLY', small, 0.7))
    allspots = math_node(nt, 'MULTIPLY', allspots, cluster)
    # ragged edge on top of warp
    fine = noise_tex(nt, mapping(nt, uv), 260.0, 2.0, 0.6)
    allspots = math_node(nt, 'MULTIPLY', allspots, map_range(nt, fine, 0.2, 0.7, 0.55, 1.15), clamp=True)
    brown_f = map_range(nt, allspots, 0.04, 0.40)
    char_f = map_range(nt, allspots, 0.38, 0.80)

    # dough colour with soft mottling and a lightly toasted rim
    cream_a = (0.70, 0.50, 0.28, 1.0)
    cream_b = (0.62, 0.42, 0.22, 1.0)
    mottle = noise_tex(nt, mapping(nt, uv, loc=(9, 4, 2)), 9.0, 3.0, 0.5)
    base = mix_color(nt, map_range(nt, mottle, 0.3, 0.7, 0.0, 1.0), cream_a, cream_b)
    rr = vmath(nt, 'LENGTH', uv)
    rim = math_node(nt, 'MULTIPLY',
                    map_range(nt, rr, TORT_RADIUS * 0.80, TORT_RADIUS * 0.98),
                    map_range(nt, noise_tex(nt, mapping(nt, uv), 30.0, 3.0), 0.3, 0.7, 0.2, 0.9))
    base = mix_color(nt, math_node(nt, 'MULTIPLY', rim, 0.55), base, (0.45, 0.24, 0.09, 1.0))
    toasted = mix_color(nt, math_node(nt, 'MULTIPLY', brown_f, 0.92), base, (0.40, 0.20, 0.07, 1.0))
    charred = mix_color(nt, char_f, toasted, (0.045, 0.02, 0.008, 1.0))

    # flour dusting: fine specks + soft patches
    specks = map_range(nt, noise_tex(nt, mapping(nt, uv), 420.0, 1.0, 0.5), 0.60, 0.72)
    patches = map_range(nt, noise_tex(nt, mapping(nt, uv, loc=(2, 7, 1)), 14.0, 3.0), 0.42, 0.78)
    dust = math_node(nt, 'MULTIPLY', specks, patches)
    dust = math_node(nt, 'ADD', dust, math_node(nt, 'MULTIPLY', patches, 0.10), clamp=True)
    color = mix_color(nt, math_node(nt, 'MULTIPLY', dust, 0.75), charred, (0.86, 0.78, 0.64, 1.0))
    link(nt, color, bsdf.inputs['Base Color'])

    rough = math_node(nt, 'ADD', 0.60,
                      math_node(nt, 'ADD', math_node(nt, 'MULTIPLY', allspots, 0.18),
                                math_node(nt, 'MULTIPLY', dust, 0.28)), clamp=True)
    link(nt, rough, bsdf.inputs['Roughness'])

    # surface relief: fine grain + broad blisters, spots slightly recessed
    h_fine = noise_tex(nt, mapping(nt, uv), 110.0, 6.0, 0.55)
    h_blis = noise_tex(nt, mapping(nt, uv, loc=(5, 5, 5)), 26.0, 2.0, 0.5)
    height = math_node(nt, 'ADD', math_node(nt, 'MULTIPLY', h_fine, 0.55),
                       math_node(nt, 'SUBTRACT', math_node(nt, 'MULTIPLY', h_blis, 0.75),
                                 math_node(nt, 'MULTIPLY', allspots, 0.15)))
    bump = node(nt, 'ShaderNodeBump')
    bump.inputs['Strength'].default_value = 0.6
    bump.inputs['Distance'].default_value = 0.0015
    link(nt, height, bump.inputs['Height'])
    link(nt, bump.outputs['Normal'], bsdf.inputs['Normal'])

    setin(bsdf, 'Subsurface Weight', 0.15)
    setin(bsdf, 'Subsurface Radius', (1.0, 0.55, 0.30))
    setin(bsdf, 'Subsurface Scale', 0.0035)
    setin(bsdf, 'Specular IOR Level', 0.35)
    setin(bsdf, 'Sheen Weight', 0.18)
    setin(bsdf, 'Sheen Roughness', 0.45)
    setin(bsdf, 'Coat Weight', 0.03)
    setin(bsdf, 'Coat Roughness', 0.30)
    return mat


def wood_material():
    log("Building wood material")
    mat, nt = new_material("OiledWood")
    out = node(nt, 'ShaderNodeOutputMaterial')
    bsdf = principled(nt)
    link(nt, bsdf.outputs['BSDF'], out.inputs['Surface'])
    tc = node(nt, 'ShaderNodeTexCoord')
    p = tc.outputs['Object']

    # grain runs along X: wavy growth-ring bands vary along Y
    wv = noise_tex(nt, mapping(nt, p, scale=(2.0, 9.0, 2.0)), 1.6, 3.0, 0.5, out='Color')
    warp = vmath(nt, 'MULTIPLY', vmath(nt, 'SUBTRACT', wv, (0.5, 0.5, 0.5)), (0.006, 0.05, 0.006))
    pw = vmath(nt, 'ADD', p, warp)
    wave = node(nt, 'ShaderNodeTexWave', wave_type='BANDS', bands_direction='Y',
                wave_profile='SIN')
    wave.inputs['Scale'].default_value = 26.0
    wave.inputs['Distortion'].default_value = 3.5
    wave.inputs['Detail'].default_value = 2.0
    wave.inputs['Detail Scale'].default_value = 1.6
    wave.inputs['Detail Roughness'].default_value = 0.55
    link(nt, pw, wave.inputs['Vector'])
    ring = map_range(nt, wave.outputs['Fac'], 0.35, 0.80)
    streak = noise_tex(nt, mapping(nt, p, scale=(2.5, 420.0, 420.0)), 1.0, 5.0, 0.62)
    streak2 = noise_tex(nt, mapping(nt, p, loc=(4, 1, 2), scale=(5.0, 150.0, 150.0)), 1.0, 3.0, 0.5)
    blotch = noise_tex(nt, mapping(nt, p, scale=(1.2, 6.0, 6.0)), 2.0, 3.0, 0.5)

    dark_f = math_node(nt, 'ADD',
                       math_node(nt, 'MULTIPLY', ring, 0.42),
                       math_node(nt, 'ADD',
                                 math_node(nt, 'MULTIPLY', map_range(nt, streak, 0.3, 0.75), 0.30),
                                 math_node(nt, 'MULTIPLY', blotch, 0.35)), clamp=True)
    light = (0.50, 0.26, 0.10, 1.0)
    dark = (0.19, 0.075, 0.028, 1.0)
    col = mix_color(nt, dark_f, light, dark)

    # knife scratches: three sets of thin streaks at different angles, localised to the cutting zone
    scr = None
    for k, (ang, sx, sy, off) in enumerate(((0.35, 9.0, 1300.0, (1, 2, 0)),
                                            (-0.62, 7.0, 1600.0, (5, 1, 0)),
                                            (1.25, 10.0, 1400.0, (2, 6, 0)))):
        nz = noise_tex(nt, mapping(nt, p, loc=off, rot=(0, 0, ang), scale=(sx, sy, 1.0)), 1.0, 1.0, 0.5)
        line = map_range(nt, nz, 0.665, 0.705)
        scr = line if scr is None else math_node(nt, 'MAXIMUM', scr, line)
    zone = map_range(nt, noise_tex(nt, mapping(nt, p, loc=(7, 3, 0)), 5.0, 2.0), 0.35, 0.65, 0.25, 1.0)
    scr = math_node(nt, 'MULTIPLY', scr, zone)
    col = mix_color(nt, math_node(nt, 'MULTIPLY', scr, 0.55), col, (0.62, 0.40, 0.22, 1.0))
    link(nt, col, bsdf.inputs['Base Color'])

    rough = math_node(nt, 'ADD', 0.38,
                      math_node(nt, 'ADD', math_node(nt, 'MULTIPLY', streak2, 0.16),
                                math_node(nt, 'MULTIPLY', scr, 0.30)), clamp=True)
    link(nt, rough, bsdf.inputs['Roughness'])
    height = math_node(nt, 'SUBTRACT',
                       math_node(nt, 'ADD', math_node(nt, 'MULTIPLY', streak, 0.6),
                                 math_node(nt, 'MULTIPLY', ring, 0.25)),
                       math_node(nt, 'MULTIPLY', scr, 1.4))
    bump = node(nt, 'ShaderNodeBump')
    bump.inputs['Strength'].default_value = 0.35
    bump.inputs['Distance'].default_value = 0.0005
    link(nt, height, bump.inputs['Height'])
    link(nt, bump.outputs['Normal'], bsdf.inputs['Normal'])
    setin(bsdf, 'Specular IOR Level', 0.5)
    setin(bsdf, 'Coat Weight', 0.14)          # oiled / satin finish
    setin(bsdf, 'Coat Roughness', 0.25)
    return mat


def backdrop_material():
    mat, nt = new_material("BackdropMatte")
    out = node(nt, 'ShaderNodeOutputMaterial')
    bsdf = principled(nt)
    link(nt, bsdf.outputs['BSDF'], out.inputs['Surface'])
    tc = node(nt, 'ShaderNodeTexCoord')
    n = noise_tex(nt, tc.outputs['Object'], 3.0, 2.0)
    col = mix_color(nt, n, (0.070, 0.085, 0.082, 1.0), (0.055, 0.068, 0.068, 1.0))
    link(nt, col, bsdf.inputs['Base Color'])
    bsdf.inputs['Roughness'].default_value = 0.9
    return mat


def world_setup():
    log("Building procedural world")
    sc = bpy.context.scene
    w = bpy.data.worlds.new("StudioWorld")
    sc.world = w
    w.use_nodes = True
    nt = w.node_tree
    nt.nodes.clear()
    out = node(nt, 'ShaderNodeOutputWorld')
    bg = node(nt, 'ShaderNodeBackground')
    tc = node(nt, 'ShaderNodeTexCoord')
    sep = node(nt, 'ShaderNodeSeparateXYZ')
    link(nt, tc.outputs['Generated'], sep.inputs['Vector'])
    grad = ramp(nt, map_range(nt, sep.outputs['Z'], -0.4, 1.0),
                [(0.0, (0.17, 0.15, 0.13, 1.0)), (1.0, (0.55, 0.62, 0.72, 1.0))])
    link(nt, grad, bg.inputs['Color'])
    bg.inputs['Strength'].default_value = 0.20
    link(nt, bg.outputs['Background'], out.inputs['Surface'])


# --------------------------------------------------------------------------- #
# Lights + camera
# --------------------------------------------------------------------------- #
def area_light(name, loc, target, size, size_y, irradiance, color):
    """Area light whose power is chosen for a target irradiance at the subject
    (Cycles convention: pixel ~ albedo * P / (pi^2 d^2) for a small area light)."""
    d = (Vector(loc) - Vector(target)).length
    power = irradiance * math.pi ** 2 * d * d
    ld = bpy.data.lights.new(name, 'AREA')
    ld.shape = 'RECTANGLE'
    ld.size, ld.size_y = size, size_y
    ld.energy = power
    ld.color = color
    ob = link_obj(bpy.data.objects.new(name, ld), "Lighting")
    ob.location = loc
    aim(ob, target)
    log("  light %-5s d=%.2f m  size %.2fx%.2f  %.1f W" % (name, d, size, size_y, power))
    return ob


def build_lights(subject):
    log("Building lighting rig")
    s = Vector(subject)
    area_light("Key", s + Vector((-0.42, -0.30, 0.38)), s, 0.60, 0.45, 3.0, (1.0, 0.92, 0.80))
    area_light("Fill", s + Vector((0.50, 0.42, 0.22)), s, 0.55, 0.55, 0.55, (0.88, 0.94, 1.0))
    area_light("Rim", s + Vector((0.30, -0.58, 0.28)), s, 0.55, 0.16, 2.4, (1.0, 0.95, 0.88))


def build_camera(subject, focus):
    log("Building camera (low angle, shallow DOF)")
    sc = bpy.context.scene
    cam = bpy.data.cameras.new("Cam")
    cam.lens = 85.0
    cam.sensor_width = 36.0
    cam.clip_start, cam.clip_end = 0.02, 20.0
    az = math.radians(24.0)
    dist, elev = 0.55, math.radians(17.0)
    s = Vector(subject)
    ob = link_obj(bpy.data.objects.new("Camera", cam), "Camera")
    ob.location = s + Vector((dist * math.sin(az), dist * math.cos(az), dist * math.tan(elev)))
    aim(ob, s + Vector((0.0, 0.0, 0.006)))
    fo = bpy.data.objects.new("FocusTarget", None)
    fo.empty_display_type = 'PLAIN_AXES'
    fo.empty_display_size = 0.005
    fo.location = focus
    link_obj(fo, "Camera")
    cam.dof.use_dof = True
    cam.dof.focus_object = fo
    cam.dof.aperture_fstop = 4.0
    cam.dof.aperture_blades = 8
    cam.dof.aperture_ratio = 1.0
    sc.camera = ob
    log("  camera at (%.3f, %.3f, %.3f), focus at (%.3f, %.3f, %.3f), f/%.1f %dmm" %
        (*ob.location, *focus, cam.dof.aperture_fstop, cam.lens))


# --------------------------------------------------------------------------- #
# Render setup
# --------------------------------------------------------------------------- #
def setup_devices(sc, force_cpu):
    sc.cycles.device = 'CPU'
    used = []
    if force_cpu:
        return "CPU (forced)"
    try:
        prefs = bpy.context.preferences.addons['cycles'].preferences
        for dtype in ('OPTIX', 'CUDA', 'HIP', 'ONEAPI', 'METAL'):
            try:
                prefs.compute_device_type = dtype
                prefs.get_devices()
            except Exception:
                continue
            gpus = [d for d in prefs.devices if d.type == dtype]
            if gpus:
                for d in prefs.devices:
                    d.use = (d.type == dtype)
                used = [d.name for d in gpus]
                sc.cycles.device = 'GPU'
                return "GPU %s: %s" % (dtype, ", ".join(used))
    except Exception as e:
        log("GPU setup failed (%s) - using CPU" % e)
    sc.cycles.device = 'CPU'
    return "CPU"


def setup_render(args):
    sc = bpy.context.scene
    r = sc.render
    r.engine = 'CYCLES'
    r.resolution_x, r.resolution_y = args.res
    r.resolution_percentage = 100
    r.image_settings.file_format = 'PNG'
    r.image_settings.color_mode = 'RGB'
    r.image_settings.color_depth = '16'
    r.image_settings.compression = 15
    r.film_transparent = False
    r.use_persistent_data = False
    cy = sc.cycles
    cy.samples = args.samples
    cy.use_adaptive_sampling = True
    cy.adaptive_threshold = 0.004
    cy.adaptive_min_samples = 64
    cy.seed = args.seed
    cy.use_animated_seed = False
    cy.max_bounces = 10
    cy.diffuse_bounces = 4
    cy.glossy_bounces = 5
    cy.transmission_bounces = 4
    cy.transparent_max_bounces = 4
    cy.sample_clamp_indirect = 8.0
    cy.caustics_reflective = False
    cy.caustics_refractive = False
    cy.use_denoising = True
    cy.denoiser = 'OPENIMAGEDENOISE'
    cy.denoising_input_passes = 'RGB_ALBEDO_NORMAL'
    cy.denoising_prefilter = 'ACCURATE'
    vs = sc.view_settings
    vs.view_transform = 'AgX'
    try:
        vs.look = 'AgX - Medium High Contrast'
    except Exception as e:
        log("WARN could not set AgX look: %s" % e)
    vs.exposure = -0.5
    vs.gamma = 1.0
    sc.display_settings.display_device = 'sRGB'
    return setup_devices(sc, args.cpu)


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main():
    args = parse_args()
    global SEED
    SEED = args.seed
    global F_END
    if args.f_end:
        F_END = args.f_end
    random.seed(SEED)
    rng = random.Random(SEED)
    os.makedirs(args.out_dir, exist_ok=True)
    log("Blender %s | Python %s | %s %s" % (bpy.app.version_string, platform.python_version(),
                                            platform.system(), platform.machine()))
    log("CPU: %s (%s logical cores)" % (platform.processor(), os.cpu_count()))
    log("Output dir: %s | resolution %dx%d | samples %d | seed %d" %
        (args.out_dir, args.res[0], args.res[1], args.samples, SEED))

    sc = reset_scene()
    board = build_board()
    build_sim_floor()
    backdrop = build_backdrop()
    sim = build_tortilla_sim(rng)
    add_fold_animation(sim)
    cm = add_cloth(sim, args.set)

    dump, dump_frames = {}, ([f for f in (1, 12, 24, 36, 46, 68, 90, 114, 140, F_END) if f <= F_END] if args.dump else [])
    bake(sim, cm, dump_frames, dump)
    if args.dump:
        with open(args.dump, "w") as fh:
            json.dump({"frames": dump, "n": 2 * GRID_M + 1, "cx": CX, "cy": CY}, fh)
        log("Dumped cloth frames to %s" % args.dump)
    tort, cloth_surface = freeze_and_thicken(sim)

    # --- materials
    tort.data.materials.append(tortilla_material())
    board.data.materials.append(wood_material())
    backdrop.data.materials.append(backdrop_material())
    world_setup()

    # --- composition anchors derived from the baked result
    pts = [v.co for v in tort.data.vertices]
    xs, ys, zs = [p.x for p in pts], [p.y for p in pts], [p.z for p in pts]
    subject = ((min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2, 0.0)
    lead = [p for p in pts if p.y > max(ys) - 0.0025]           # the leading (camera-side) fold edge
    focus = (sum(p.x for p in lead) / len(lead), sum(p.y for p in lead) / len(lead),
             sum(p.z for p in lead) / len(lead))
    build_lights(subject)
    build_camera(subject, focus)
    hw = setup_render(args)
    log("Compute device: %s" % hw)

    blend_path = os.path.join(args.out_dir, "tortilla_scene.blend")
    bpy.ops.wm.save_as_mainfile(filepath=blend_path, compress=True)
    log("Saved .blend after bake: %s" % blend_path)
    if args.bake_only:
        log("--bake-only: skipping render. Total runtime %.1f s" % (time.time() - T0))
        return

    out_png = os.path.join(args.out_dir, "tortilla_render.png")
    sc.render.filepath = out_png
    log("Rendering %dx%d, %d samples (Cycles, %s)" % (args.res[0], args.res[1], args.samples, hw))
    t = time.time()
    try:
        bpy.ops.render.render(write_still=True)
    except Exception as e:
        if sc.cycles.device != 'GPU':
            raise
        log("GPU render failed (%s) - falling back to CPU" % e)
        sc.cycles.device = 'CPU'
        bpy.ops.render.render(write_still=True)
    log("Render finished in %.1f s -> %s" % (time.time() - t, out_png))
    if not (os.path.exists(out_png) and os.path.getsize(out_png) > 0):
        raise RuntimeError("Render output missing")
    log("Total runtime %.1f s" % (time.time() - T0))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        log("FAILED")
        sys.stdout.flush()
        sys.exit(1)
