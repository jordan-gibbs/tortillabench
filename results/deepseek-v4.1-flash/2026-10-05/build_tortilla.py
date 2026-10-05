#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
build_tortilla.py
=================
Headless, fully-procedural construction of a photorealistic still of a freshly
cooked flour tortilla folded into quarters, resting on a wooden cutting board.

Everything (geometry, cloth simulation, procedural shaders, lighting, camera,
render settings) is generated in code.  No external assets are used.

Run:
    blender --background --factory-startup --python build_tortilla.py
Optional script arguments (after "--"):
    --outdir DIR        output directory (default: <script dir>/output)
    --bake-only         bake the cloth sim + save .blend, do not render
    --samples N         Cycles samples (default 512)
    --resolution X Y    render resolution (default 3840 2160)
    --no-volume         disable the (optional) volumetric steam
    --seed N            fixed RNG / Cycles seed (default 0)
    --fast              quick low-res preview render (for debugging)
"""

import argparse
import math
import os
import platform
import sys
import time
import traceback

import bpy
import bmesh
from mathutils import Vector

# --------------------------------------------------------------------------- #
# Globals / small helpers
# --------------------------------------------------------------------------- #

START_TIME = time.time()
_HERE = os.path.dirname(os.path.abspath(__file__)) if "__file__" in globals() else os.getcwd()
_LOG_LINES = []


def log(msg=""):
    """Timestamped progress logging to stdout (and an in-memory buffer)."""
    line = "[%7.1fs] %s" % (time.time() - START_TIME, msg)
    print(line, flush=True)
    _LOG_LINES.append(line)


def warn(msg):
    log("WARNING: " + msg)


def set_in(node, name, value):
    """Defensively set a node input by name."""
    try:
        if name in node.inputs:
            node.inputs[name].default_value = value
            return True
    except Exception:
        pass
    return False


def new_node(nt, kind, label, loc):
    n = nt.nodes.new(kind)
    n.label = label
    n.name = label
    n.location = loc
    return n


def link(nt, a, b):
    return nt.links.new(a, b)


def make_ramp(nt, label, loc, stops, interp='LINEAR'):
    r = new_node(nt, 'ShaderNodeValToRGB', label, loc)
    cr = r.color_ramp
    cr.interpolation = interp
    # remove all but first two, then set / add
    while len(cr.elements) > 2:
        cr.elements.remove(cr.elements[-1])
    cr.elements[0].position = stops[0][0]
    cr.elements[0].color = stops[0][1]
    cr.elements[1].position = stops[1][0]
    cr.elements[1].color = stops[1][1]
    for pos, col in stops[2:]:
        e = cr.elements.new(pos)
        e.color = col
    return r


def mixrgb(nt, label, loc, blend='MIX'):
    """MixRGB node compatible with Blender 4.x (legacy node still present)."""
    n = new_node(nt, 'ShaderNodeMixRGB', label, loc)
    n.blend_type = blend
    return n


def add_box(name, size, location):
    """Axis-aligned box with the given full dimensions, centred at location."""
    me = bpy.data.meshes.new(name + "Mesh")
    bm = bmesh.new()
    bmesh.ops.create_cube(bm, size=1.0)
    bmesh.ops.scale(bm, vec=Vector(size), verts=bm.verts)
    bmesh.ops.recalc_face_normals(bm, faces=bm.faces)
    bm.to_mesh(me)
    bm.free()
    obj = bpy.data.objects.new(name, me)
    obj.location = Vector(location)
    bpy.context.scene.collection.objects.link(obj)
    return obj


def aim_at(obj, target):
    d = Vector(target) - obj.location
    obj.rotation_euler = d.to_track_quat('-Z', 'Y').to_euler()


def keyframe(obj, data_path, index, frame, value):
    if index is None:
        setattr(obj, data_path, value)
        obj.keyframe_insert(data_path=data_path, frame=frame)
    else:
        cur = list(getattr(obj, data_path))
        cur[index] = value
        setattr(obj, data_path, cur)
        obj.keyframe_insert(data_path=data_path, index=index, frame=frame)


# --------------------------------------------------------------------------- #
# Command line
# --------------------------------------------------------------------------- #

def parse_args():
    argv = sys.argv
    if "--" in argv:
        argv = argv[argv.index("--") + 1:]
    else:
        argv = []
    p = argparse.ArgumentParser(prog="build_tortilla.py", add_help=True)
    p.add_argument("--outdir", type=str, default=None)
    p.add_argument("--bake-only", action="store_true")
    p.add_argument("--samples", type=int, default=512)
    p.add_argument("--resolution", type=int, nargs=2, default=[3840, 2160])
    p.add_argument("--no-volume", action="store_true")
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--fast", action="store_true",
                   help="low-res low-sample preview")
    p.add_argument("--no-selfcol", action="store_true",
                   help="diagnostic: disable cloth self-collision")
    p.add_argument("--no-pin", action="store_true",
                   help="diagnostic: disable the pin group")
    p.add_argument("--no-board-col", action="store_true",
                   help="diagnostic: disable board collision")
    return p.parse_args(argv)


# --------------------------------------------------------------------------- #
# Scene reset
# --------------------------------------------------------------------------- #

def reset_scene():
    bpy.ops.wm.read_factory_settings(use_empty=True)
    # purge leftovers
    for coll in (bpy.data.objects, bpy.data.meshes, bpy.data.materials,
                 bpy.data.lights, bpy.data.cameras, bpy.data.worlds,
                 bpy.data.node_groups):
        for item in list(coll):
            try:
                coll.remove(item)
            except Exception:
                pass
    log("Scene reset to empty factory state.")


# --------------------------------------------------------------------------- #
# Geometry
# --------------------------------------------------------------------------- #

RADIUS = 0.110            # 22 cm diameter
THICKNESS = 0.0020        # ~2.0 mm nominal (modulated 1.5-2.0 mm)
GRID_N = 28               # uniform grid disk -> edge length ~7.9 mm
SELF_DIST = 0.0022        # < min vertex spacing (3.0 mm), so self-collision is stable
WAVE_Z = 0.0000           # initial out-of-plane wave (0 = perfectly flat start)
BASE_Z = 0.0030           # start slightly above the collider so it settles in


def build_tortilla():
    """Concentric-ring disk with irregular rim + a UV map, single sheet."""
    me = bpy.data.meshes.new("TortillaMesh")
    bm = bmesh.new()

    N = GRID_N
    step = 2.0 * RADIUS / N
    pts = {}
    for j in range(N + 1):
        for i in range(N + 1):
            pts[(i, j)] = [-RADIUS + i * step, -RADIUS + j * step]

    def inside(i, j):
        x, y = pts[(i, j)]
        return x * x + y * y <= RADIUS * RADIUS

    cells = [(i, j) for j in range(N) for i in range(N)
             if inside(i, j) and inside(i + 1, j)
             and inside(i + 1, j + 1) and inside(i, j + 1)]
    kept = set(cells)

    def on_boundary(i, j):
        for di in (-1, 0):
            for dj in (-1, 0):
                ci, cj = i + di, j + dj
                if 0 <= ci < N and 0 <= cj < N:
                    if (ci, cj) not in kept:
                        return True
                else:
                    return True
        return False

    used = set()
    for (i, j) in cells:
        used.update(((i, j), (i + 1, j), (i + 1, j + 1), (i, j + 1)))

    # project the ragged grid border onto the true (slightly irregular) rim
    for (i, j) in used:
        if on_boundary(i, j):
            x, y = pts[(i, j)]
            r = math.hypot(x, y)
            if r > 1e-9:
                th = math.atan2(y, x)
                edge = (1.0 + 0.020 * math.sin(3.0 * th + 0.7)
                        + 0.013 * math.sin(5.0 * th + 1.9)
                        + 0.007 * math.sin(8.0 * th + 0.3))
                rr = RADIUS * edge
                pts[(i, j)] = [rr * x / r, rr * y / r]

    vmap = {}
    for (i, j) in used:
        x, y = pts[(i, j)]
        r = math.hypot(x, y)
        t = min(1.0, r / RADIUS)
        th = math.atan2(y, x)
        z = BASE_Z + WAVE_Z * (0.6 * math.sin(2.0 * th + 0.4)
                               + 0.4 * math.sin(3.0 * th + 2.1)) * (t ** 2)
        vmap[(i, j)] = bm.verts.new((x, y, z))

    for (i, j) in cells:
        bm.faces.new((vmap[(i, j)], vmap[(i + 1, j)],
                      vmap[(i + 1, j + 1)], vmap[(i, j + 1)]))

    bmesh.ops.recalc_face_normals(bm, faces=bm.faces)
    bm.to_mesh(me)
    bm.free()

    # planar UV map: keeps the char pattern glued to the surface as it folds
    uvl = me.uv_layers.new(name="UVMap")
    for loop in me.loops:
        v = me.vertices[loop.vertex_index].co
        uvl.data[loop.index].uv = (v.x / (2.0 * RADIUS) + 0.5,
                                   v.y / (2.0 * RADIUS) + 0.5)

    obj = bpy.data.objects.new("Tortilla", me)
    bpy.context.scene.collection.objects.link(obj)

    # --- non-uniform thickness vertex group (used by Solidify) ------------
    vg = obj.vertex_groups.new(name="thick")
    for v in me.vertices:
        nx = v.co.x / RADIUS
        ny = v.co.y / RADIUS
        w = 0.86 + 0.09 * math.sin(2.6 * nx + 0.5) + 0.05 * math.cos(3.4 * ny + 1.1)
        w = max(0.75, min(1.00, w))
        vg.add([v.index], w, 'REPLACE')

    # --- anchor pin group -------------------------------------------------
    # Pin the *original* x<0, y<0 quarter: it is stationary through BOTH folds,
    # so it holds the sheet without ever blocking either hinge.
    pg = obj.vertex_groups.new(name="pin")
    for v in me.vertices:
        if v.co.x <= 0.0 and v.co.y <= 0.0:
            pg.add([v.index], 1.0, 'REPLACE')
    log("  pinned %d verts in the stationary quarter."
        % sum(1 for v in me.vertices if v.co.x <= 0.0 and v.co.y <= 0.0))

    log("Tortilla mesh built: %d verts, %d faces (uniform grid disk)."
        % (len(me.vertices), len(me.polygons)))
    return obj


def build_collision_plane():
    """Single-sided plane at z=0 used for cloth contact (hidden in render)."""
    me = bpy.data.meshes.new("CollisionPlaneMesh")
    bm = bmesh.new()
    s = 1.5
    vs = [bm.verts.new((-s, -s, 0.0)), bm.verts.new((s, -s, 0.0)),
          bm.verts.new((s, s, 0.0)), bm.verts.new((-s, s, 0.0))]
    bm.faces.new(vs)   # CCW seen from +Z -> normal +Z
    bm.to_mesh(me)
    bm.free()
    obj = bpy.data.objects.new("CollisionPlane", me)
    bpy.context.scene.collection.objects.link(obj)
    col = obj.modifiers.new("Collision", 'COLLISION')
    try:
        col.settings.thickness_outer = 0.0006
        col.settings.thickness_inner = 0.0003
        col.settings.cloth_friction = 1.0
        col.settings.damping = 0.5
    except Exception as e:
        warn("collision plane settings: %r" % e)
    obj.hide_render = True
    log("Collision plane created at z=0 (single-sided, impulse-clamped).")
    return obj


def build_board(skip_collision=True):
    sx, sy, sz = 0.50, 0.36, 0.030
    obj = add_box("CuttingBoard", (sx, sy, sz), (0.0, 0.0, -sz / 2.0))

    bev = obj.modifiers.new("Bevel", 'BEVEL')
    bev.width = 0.0045
    bev.segments = 4
    bev.limit_method = 'ANGLE'
    try:
        bev.angle_limit = math.radians(40.0)
    except Exception:
        pass
    try:
        bev.harden_normals = True
    except Exception:
        pass

    if not skip_collision:
        col = obj.modifiers.new("Collision", 'COLLISION')
        try:
            col.settings.thickness_outer = 0.0006
            col.settings.thickness_inner = 0.0003
            col.settings.cloth_friction = 1.0
            col.settings.damping = 0.2
        except Exception as e:
            warn("collision settings: %r" % e)

    log("Cutting board built (%.0f x %.0f x %.0f mm, beveled)."
        % (sx * 1000, sy * 1000, sz * 1000))
    return obj


def build_table():
    obj = add_box("Table", (3.0, 3.0, 0.02), (0.0, 0.0, -0.040))
    return obj


def make_collider(name, size, location, pivot):
    obj = add_box(name, size, location)
    if pivot is not None:
        bpy.context.view_layer.update()   # ensure pivot.matrix_world is current
        obj.parent = pivot
        obj.matrix_parent_inverse = pivot.matrix_world.inverted()
    col = obj.modifiers.new("Collision", 'COLLISION')
    try:
        col.settings.thickness_outer = 0.0012
        col.settings.thickness_inner = 0.0006
        col.settings.cloth_friction = 1.0
        col.settings.damping = 0.3
    except Exception as e:
        warn("collider settings: %r" % e)
    obj.hide_render = True
    return obj


# --------------------------------------------------------------------------- #
# Cloth simulation: two sequential folds, each applied into the mesh
# --------------------------------------------------------------------------- #
# A single cloth simulation relaxes back towards its flat rest pose once the
# folding tools are removed.  To obtain a *persistent* folded tortilla we fold
# one half, bake and APPLY the cloth modifier (freezing the half-fold as the new
# rest shape), then fold the other half and apply again.  The fold is therefore
# produced entirely by baked cloth simulation.

F_SETTLE = 15
SETTLE_FRAMES = 25         # frames baked for the gravity settle
F_F1S, F_F1E = 15, 75      # labels kept for the diagnostic frames
F_F2S, F_F2E = 15, 75
FPS = 24

Z_H1 = 0.0022              # hinge height, first fold  (one sheet thick)
Z_H2 = 0.0035              # hinge height, second fold (two sheets thick)


def _delete_obj(obj):
    data = obj.data
    bpy.data.objects.remove(obj, do_unlink=True)
    if data is not None and getattr(data, "users", 0) == 0:
        try:
            if isinstance(data, bpy.types.Mesh):
                bpy.data.meshes.remove(data)
        except Exception:
            pass


def add_cloth(torch, args=None):
    """Add a cloth modifier to the (single-sheet) tortilla."""
    cl = torch.modifiers.new("Cloth", 'CLOTH')
    s = cl.settings
    s.quality = 8
    s.mass = 0.22
    s.air_damping = 1.2
    s.time_scale = 1.0
    s.tension_stiffness = 15.0
    s.compression_stiffness = 15.0
    s.shear_stiffness = 15.0
    s.bending_stiffness = 1.5
    s.pin_stiffness = 25.0
    try:
        s.use_pressure = False
        s.use_sewing = False
    except Exception:
        pass
    if args is not None and args.no_pin:
        s.vertex_group_mass = ""
        log("  pin group DISABLED (diagnostic)")
    else:
        s.vertex_group_mass = "pin"
    try:
        log("  pin property: %s" % s.bl_rna.properties['vertex_group_mass'].description)
    except Exception:
        pass

    cs = cl.collision_settings
    cs.use_collision = True
    cs.collision_quality = 5
    cs.distance_min = 0.0015
    cs.friction = 1.0
    cs.damping = 0.5
    cs.use_self_collision = not (args is not None and args.no_selfcol)
    cs.self_distance_min = SELF_DIST
    cs.self_friction = 2.0
    try:
        cs.impulse_clamp = True
        cs.impulse_clamp_factor = 0.5
    except Exception:
        pass
    if args is not None and args.no_selfcol:
        log("  self-collision DISABLED (diagnostic)")
    return cl


def build_hinge_clamp(name, hinge_z, rot_axis, angle, plates_spec, f_start, f_end):
    """A pair of plates on a pivot at the hinge; the pivot rotates 0 -> angle."""
    scene = bpy.context.scene
    pivot = bpy.data.objects.new(name, None)
    pivot.location = (0.0, 0.0, hinge_z)
    pivot.rotation_mode = 'XYZ'
    scene.collection.objects.link(pivot)
    bpy.context.view_layer.update()
    plates = [make_collider(pn, ps, pl, pivot) for (pn, ps, pl) in plates_spec]
    keyframe(pivot, "rotation_euler", rot_axis, 1, 0.0)
    keyframe(pivot, "rotation_euler", rot_axis, f_start, 0.0)
    keyframe(pivot, "rotation_euler", rot_axis, f_end, angle)
    return pivot, plates


def fold_mesh(torch, axis, angle, hinge_z, bend, lift=0.0022):
    """Scripted hinge fold: rotate the +axis half about the hinge line with a
    smooth bend radius, lifting it slightly so the folded layer starts clear of
    the stationary layer.  The result is relaxed/draped by a baked cloth settle."""
    me = torch.data
    for v in me.vertices:
        s = v.co.x if axis == 'x' else v.co.y
        if s <= 0.0:
            continue
        t = min(1.0, s / bend)
        t = t * t * (3.0 - 2.0 * t)          # smoothstep -> rounded fold
        a = angle * t
        c, sn = math.cos(a), math.sin(a)
        zr = v.co.z - hinge_z
        if axis == 'x':
            v.co.x = s * c - zr * sn
            v.co.z = hinge_z + s * sn + zr * c + lift * t
        else:
            v.co.y = s * c - zr * sn
            v.co.z = hinge_z + s * sn + zr * c + lift * t
    me.update()
    bpy.context.view_layer.update()
    log("  scripted hinge fold: axis=%s angle=%.0f deg bend=%.0f mm lift=%.1f mm"
        % (axis, math.degrees(angle), bend * 1000.0, lift * 1000.0))


def bake_and_apply(torch, end_frame, label, colliders=None):
    """Bake the cloth over 1..end_frame and freeze the final pose into the mesh."""
    scene = bpy.context.scene
    scene.frame_start = 1
    scene.frame_end = end_frame
    scene.frame_set(1)
    bpy.ops.object.select_all(action='DESELECT')
    torch.select_set(True)
    bpy.context.view_layer.objects.active = torch
    try:
        bpy.ops.ptcache.free_bake_all()
    except Exception as e:
        warn("free_bake_all: %r" % e)
    cl = torch.modifiers.get("Cloth")
    try:
        pc = cl.point_cache
        pc.frame_start = 1
        pc.frame_end = end_frame
        pc.frame_step = 1
    except Exception as e:
        warn("point cache range: %r" % e)
    log("Baking %s (%d frames, quality %d)..." % (label, end_frame, cl.settings.quality))
    t0 = time.time()
    bpy.ops.ptcache.bake_all(bake=True)
    log("%s bake finished in %.1fs." % (label, time.time() - t0))
    scene.frame_set(end_frame)
    bpy.context.view_layer.update()
    for f in (1, F_SETTLE, 30, 45, 60, end_frame):
        if f <= end_frame:
            scene.frame_set(f)
            bpy.context.view_layer.update()
            r = mesh_stats(torch, raw=True)
            log("  [%s] f%3d sheet x[%.3f,%.3f] y[%.3f,%.3f] z[%.4f,%.4f]"
                % (label, f, r['x'][0], r['x'][1], r['y'][0], r['y'][1],
                   r['z'][0], r['z'][1]))
            for c in (colliders or []):
                pts = [c.matrix_world @ Vector(v) for v in c.bound_box]
                log("      %-6s x[%.3f,%.3f] z[%.4f,%.4f]"
                    % (c.name, min(p.x for p in pts), max(p.x for p in pts),
                       min(p.z for p in pts), max(p.z for p in pts)))
    scene.frame_set(end_frame)
    bpy.context.view_layer.update()
    try:
        bpy.ops.object.modifier_apply(modifier="Cloth")
        log("%s: cloth pose applied to mesh." % label)
    except Exception as e:
        warn("modifier_apply failed (%r); using new_from_object" % e)
        dg = bpy.context.evaluated_depsgraph_get()
        me = bpy.data.meshes.new_from_object(torch.evaluated_get(dg))
        old = torch.data
        torch.data = me
        torch.modifiers.remove(torch.modifiers.get("Cloth"))
        if old.users == 0:
            bpy.data.meshes.remove(old)
# --------------------------------------------------------------------------- #
# Materials
# --------------------------------------------------------------------------- #

def base_material(name):
    mat = bpy.data.materials.new(name)
    mat.use_nodes = True
    nt = mat.node_tree
    for n in list(nt.nodes):
        nt.nodes.remove(n)
    out = new_node(nt, 'ShaderNodeOutputMaterial', 'Output', (1500, 0))
    bsdf = new_node(nt, 'ShaderNodeBsdfPrincipled', 'BSDF', (1150, 0))
    link(nt, bsdf.outputs['BSDF'], out.inputs['Surface'])
    return mat, nt, bsdf


def make_tortilla_material():
    mat, nt, bsdf = base_material("TortillaMat")

    tc = new_node(nt, 'ShaderNodeTexCoord', 'UV', (-1500, 0))
    # base warm off-white
    base_col = (0.86, 0.74, 0.55, 1.0)
    char_col = (0.055, 0.030, 0.017, 1.0)
    toast_col = (0.46, 0.29, 0.135, 1.0)
    flour_col = (0.95, 0.93, 0.88, 1.0)

    # ---- large toasted blotches -----------------------------------------
    n1 = new_node(nt, 'ShaderNodeTexNoise', 'BlotchNoise', (-1250, 420))
    set_in(n1, 'Scale', 7.0)
    set_in(n1, 'Detail', 8.0)
    set_in(n1, 'Roughness', 0.62)
    set_in(n1, 'Distortion', 0.55)
    link(nt, tc.outputs['UV'], n1.inputs['Vector'])
    cr1 = make_ramp(nt, 'BlotchRamp', (-1050, 420),
                    [(0.36, (0, 0, 0, 1)),
                     (0.50, (0.5, 0.5, 0.5, 1)),
                     (0.63, (1, 1, 1, 1))])
    link(nt, n1.outputs['Fac'], cr1.inputs['Fac'])

    # ---- medium char patches --------------------------------------------
    n2 = new_node(nt, 'ShaderNodeTexNoise', 'CharNoise', (-1250, 120))
    set_in(n2, 'Scale', 22.0)
    set_in(n2, 'Detail', 7.0)
    set_in(n2, 'Roughness', 0.65)
    set_in(n2, 'Distortion', 0.8)
    link(nt, tc.outputs['UV'], n2.inputs['Vector'])
    cr2 = make_ramp(nt, 'CharRamp', (-1050, 120),
                    [(0.44, (0, 0, 0, 1)),
                     (0.54, (0.2, 0.2, 0.2, 1)),
                     (0.66, (1, 1, 1, 1))])
    link(nt, n2.outputs['Fac'], cr2.inputs['Fac'])

    # ---- fine flour speckle ---------------------------------------------
    n3 = new_node(nt, 'ShaderNodeTexNoise', 'FlourNoise', (-1250, -180))
    set_in(n3, 'Scale', 150.0)
    set_in(n3, 'Detail', 3.0)
    set_in(n3, 'Roughness', 0.5)
    link(nt, tc.outputs['UV'], n3.inputs['Vector'])
    cr3 = make_ramp(nt, 'FlourRamp', (-1050, -180),
                    [(0.55, (0, 0, 0, 1)),
                     (0.66, (0.7, 0.7, 0.7, 1)),
                     (0.75, (1, 1, 1, 1))])
    link(nt, n3.outputs['Fac'], cr3.inputs['Fac'])

    # combine char = max(blotch, patches)
    mx = new_node(nt, 'ShaderNodeMath', 'CharMax', (-850, 270))
    mx.operation = 'MAXIMUM'
    link(nt, cr1.outputs['Color'], mx.inputs[0])
    link(nt, cr2.outputs['Color'], mx.inputs[1])

    # base -> toast -> char
    m1 = mixrgb(nt, 'ColorToast', (-620, 300))
    link(nt, mx.outputs[0], m1.inputs[0])
    m1.inputs[1].default_value = base_col
    m1.inputs[2].default_value = toast_col

    m2 = mixrgb(nt, 'ColorChar', (-440, 300))
    link(nt, mx.outputs[0], m2.inputs[0])
    link(nt, m1.outputs[0], m2.inputs[1])
    m2.inputs[2].default_value = char_col

    # flour dusting lightens
    fl = mixrgb(nt, 'ColorFlour', (-260, 300))
    fscale = new_node(nt, 'ShaderNodeMath', 'FlourScale', (-440, 60))
    fscale.operation = 'MULTIPLY'
    fscale.inputs[1].default_value = 0.55
    link(nt, cr3.outputs['Color'], fscale.inputs[0])
    link(nt, fscale.outputs[0], fl.inputs[0])
    link(nt, m2.outputs[0], fl.inputs[1])
    fl.inputs[2].default_value = flour_col
    link(nt, fl.outputs[0], bsdf.inputs['Base Color'])

    # ---- roughness -------------------------------------------------------
    r1 = mixrgb(nt, 'RoughBase', (-620, -260))
    link(nt, mx.outputs[0], r1.inputs[0])
    r1.inputs[1].default_value = (0.50, 0.50, 0.50, 1)
    r1.inputs[2].default_value = (0.38, 0.38, 0.38, 1)
    r2 = mixrgb(nt, 'RoughFlour', (-440, -260))
    link(nt, fscale.outputs[0], r2.inputs[0])
    link(nt, r1.outputs[0], r2.inputs[1])
    r2.inputs[2].default_value = (0.68, 0.68, 0.68, 1)
    link(nt, r2.outputs[0], bsdf.inputs['Roughness'])

    # ---- subsurface (warm, subtle) --------------------------------------
    set_in(bsdf, 'Subsurface Weight', 0.16)
    try:
        bsdf.inputs['Subsurface Radius'].default_value = (0.32, 0.13, 0.055)
    except Exception:
        pass
    set_in(bsdf, 'Subsurface Scale', 0.020)
    set_in(bsdf, 'Specular IOR Level', 0.42)
    set_in(bsdf, 'Sheen Weight', 0.12)
    set_in(bsdf, 'Sheen Roughness', 0.35)
    try:
        bsdf.inputs['Sheen Tint'].default_value = (1.0, 0.88, 0.72, 1.0)
    except Exception:
        pass

    # ---- surface bump: blisters + grain + flour --------------------------
    bump1 = new_node(nt, 'ShaderNodeBump', 'BumpCrust', (700, -120))
    bump1.inputs['Strength'].default_value = 0.28
    bump1.inputs['Distance'].default_value = 0.0022
    link(nt, mx.outputs[0], bump1.inputs['Height'])

    bump2 = new_node(nt, 'ShaderNodeBump', 'BumpFlour', (900, -220))
    bump2.inputs['Strength'].default_value = 0.16
    bump2.inputs['Distance'].default_value = 0.0006
    link(nt, cr3.outputs['Color'], bump2.inputs['Height'])
    link(nt, bump1.outputs['Normal'], bump2.inputs['Normal'])
    link(nt, bump2.outputs['Normal'], bsdf.inputs['Normal'])

    log("Tortilla material built (procedural, node-based).")
    return mat


def make_wood_material():
    mat, nt, bsdf = base_material("WoodMat")

    tc = new_node(nt, 'ShaderNodeTexCoord', 'Object', (-1600, 0))
    mp = new_node(nt, 'ShaderNodeMapping', 'GrainMap', (-1400, 0))
    link(nt, tc.outputs['Object'], mp.inputs['Vector'])
    mp.inputs['Scale'].default_value = (1.0, 8.0, 1.0)

    # ---- grain base ------------------------------------------------------
    nw = new_node(nt, 'ShaderNodeTexNoise', 'WoodNoise', (-1150, 300))
    set_in(nw, 'Scale', 25.0)
    set_in(nw, 'Detail', 8.0)
    set_in(nw, 'Roughness', 0.58)
    set_in(nw, 'Distortion', 0.25)
    link(nt, mp.outputs['Vector'], nw.inputs['Vector'])
    crw = make_ramp(nt, 'WoodRamp', (-950, 300),
                    [(0.28, (0.028, 0.011, 0.004, 1)),
                     (0.50, (0.105, 0.048, 0.017, 1)),
                     (0.74, (0.260, 0.130, 0.052, 1))])
    link(nt, nw.outputs['Fac'], crw.inputs['Fac'])

    # fine grain lines
    nf = new_node(nt, 'ShaderNodeTexNoise', 'FineGrain', (-1150, 40))
    set_in(nf, 'Scale', 150.0)
    set_in(nf, 'Detail', 5.0)
    set_in(nf, 'Roughness', 0.7)
    link(nt, mp.outputs['Vector'], nf.inputs['Vector'])
    crf = make_ramp(nt, 'FineRamp', (-950, 40),
                    [(0.40, (0.05, 0.05, 0.05, 1)),
                     (0.53, (0.78, 0.78, 0.78, 1))])
    link(nt, nf.outputs['Fac'], crf.inputs['Fac'])

    # large colour variation
    nv = new_node(nt, 'ShaderNodeTexNoise', 'VarNoise', (-1150, -220))
    set_in(nv, 'Scale', 0.8)
    set_in(nv, 'Detail', 2.0)
    link(nt, tc.outputs['Object'], nv.inputs['Vector'])

    m1 = mixrgb(nt, 'WoodMix', (-720, 300))
    m1.blend_type = 'MIX'
    link(nt, crf.outputs['Color'], m1.inputs[0])
    link(nt, crw.outputs['Color'], m1.inputs[1])
    m1.inputs[2].default_value = (0.22, 0.11, 0.045, 1)

    m2 = mixrgb(nt, 'WoodVar', (-520, 300))
    m2.blend_type = 'OVERLAY'
    m2.inputs[0].default_value = 0.25
    link(nt, nv.outputs['Fac'], m2.inputs[0])
    link(nt, m1.outputs[0], m2.inputs[1])
    m2.inputs[2].default_value = (0.5, 0.5, 0.5, 1)
    link(nt, m2.outputs[0], bsdf.inputs['Base Color'])

    # ---- knife scratches (stretched voronoi edges) -----------------------
    mp2 = new_node(nt, 'ShaderNodeMapping', 'ScratchMap', (-1400, -420))
    link(nt, tc.outputs['Object'], mp2.inputs['Vector'])
    mp2.inputs['Scale'].default_value = (1.0, 4.5, 1.0)
    vor = new_node(nt, 'ShaderNodeTexVoronoi', 'Scratch', (-1150, -420))
    try:
        vor.feature = 'DISTANCE_TO_EDGE'
    except Exception:
        pass
    set_in(vor, 'Scale', 9.0)
    set_in(vor, 'Randomness', 1.0)
    link(nt, mp2.outputs['Vector'], vor.inputs['Vector'])
    crs = make_ramp(nt, 'ScratchRamp', (-950, -420),
                    [(0.00, (1, 1, 1, 1)),
                     (0.035, (0.0, 0.0, 0.0, 1))])
    link(nt, vor.outputs['Distance'], crs.inputs['Fac'])

    # roughness: satin oiled finish + scratch variation
    r0 = new_node(nt, 'ShaderNodeValue', 'RoughVal', (-720, -520))
    r0.outputs[0].default_value = 0.30
    rsc = mixrgb(nt, 'RoughScratch', (-520, -520))
    rsc.blend_type = 'MIX'
    link(nt, crs.outputs['Color'], rsc.inputs[0])
    link(nt, r0.outputs[0], rsc.inputs[1])
    rsc.inputs[2].default_value = (0.55, 0.55, 0.55, 1)
    link(nt, rsc.outputs[0], bsdf.inputs['Roughness'])

    # ---- bump: grain + scratches ----------------------------------------
    b1 = new_node(nt, 'ShaderNodeBump', 'BumpGrain', (600, -150))
    b1.inputs['Strength'].default_value = 0.22
    b1.inputs['Distance'].default_value = 0.0012
    link(nt, crf.outputs['Color'], b1.inputs['Height'])
    b2 = new_node(nt, 'ShaderNodeBump', 'BumpScratch', (800, -250))
    b2.inputs['Strength'].default_value = 0.35
    b2.inputs['Distance'].default_value = 0.0005
    link(nt, crs.outputs['Color'], b2.inputs['Height'])
    link(nt, b1.outputs['Normal'], b2.inputs['Normal'])
    link(nt, b2.outputs['Normal'], bsdf.inputs['Normal'])

    # satin / oiled clearcoat
    set_in(bsdf, 'Coat Weight', 0.18)
    set_in(bsdf, 'Coat Roughness', 0.16)
    set_in(bsdf, 'Specular IOR Level', 0.45)

    log("Wood material built (procedural grain + scratches + satin coat).")
    return mat


def make_table_material():
    mat, nt, bsdf = base_material("TableMat")
    set_in(bsdf, 'Base Color', (0.018, 0.016, 0.015, 1.0))
    set_in(bsdf, 'Roughness', 0.62)
    set_in(bsdf, 'Specular IOR Level', 0.25)
    return mat


def make_steam_material():
    mat, nt, bsdf = base_material("SteamMat")
    # replace surface output with a volume
    for n in list(nt.nodes):
        nt.nodes.remove(n)
    out = new_node(nt, 'ShaderNodeOutputMaterial', 'Output', (900, 0))
    vol = new_node(nt, 'ShaderNodeVolumePrincipled', 'Volume', (600, 0))
    link(nt, vol.outputs['Volume'], out.inputs['Volume'])

    tc = new_node(nt, 'ShaderNodeTexCoord', 'Object', (-900, 0))
    mp = new_node(nt, 'ShaderNodeMapping', 'Map', (-700, 0))
    link(nt, tc.outputs['Object'], mp.inputs['Vector'])
    mp.inputs['Scale'].default_value = (1.0, 1.0, 1.6)

    nz = new_node(nt, 'ShaderNodeTexNoise', 'SteamNoise', (-480, 120))
    set_in(nz, 'Scale', 2.6)
    set_in(nz, 'Detail', 6.0)
    set_in(nz, 'Roughness', 0.62)
    link(nt, mp.outputs['Vector'], nz.inputs['Vector'])
    cr = make_ramp(nt, 'SteamRamp', (-260, 120),
                   [(0.40, (0, 0, 0, 1)),
                    (0.62, (0.65, 0.65, 0.65, 1)),
                    (0.80, (1, 1, 1, 1))])

    # radial fade so the domain has no visible boundary
    length = new_node(nt, 'ShaderNodeVectorMath', 'Length', (-480, -200))
    length.operation = 'LENGTH'
    link(nt, tc.outputs['Object'], length.inputs[0])
    fader = new_node(nt, 'ShaderNodeMapRange', 'Fade', (-260, -200))
    fader.inputs['From Min'].default_value = 0.02
    fader.inputs['From Max'].default_value = 0.135
    fader.inputs['To Min'].default_value = 1.0
    fader.inputs['To Max'].default_value = 0.0
    try:
        fader.clamp = True
    except Exception:
        pass
    link(nt, length.outputs['Value'], fader.inputs['Value'])

    mul = new_node(nt, 'ShaderNodeMath', 'Density', (-40, 0))
    mul.operation = 'MULTIPLY'
    link(nt, cr.outputs['Color'], mul.inputs[0])
    link(nt, fader.outputs['Result'], mul.inputs[1])
    scale = new_node(nt, 'ShaderNodeMath', 'DensityScale', (160, 0))
    scale.operation = 'MULTIPLY'
    scale.inputs[1].default_value = 0.006
    link(nt, mul.outputs[0], scale.inputs[0])
    link(nt, scale.outputs[0], vol.inputs['Density'])
    set_in(vol, 'Color', (1.0, 0.98, 0.95, 1.0))
    set_in(vol, 'Anisotropy', 0.3)

    log("Steam volume material built (subtle).")
    return mat


# --------------------------------------------------------------------------- #
# Lighting / world / camera
# --------------------------------------------------------------------------- #

def add_area_light(name, location, target, power, size, color, shape='SQUARE', size_y=None):
    ld = bpy.data.lights.new(name, 'AREA')
    ld.energy = power
    ld.color = color
    ld.shape = shape
    ld.size = size
    if size_y is not None:
        try:
            ld.size_y = size_y
        except Exception:
            pass
    obj = bpy.data.objects.new(name, ld)
    obj.location = Vector(location)
    bpy.context.scene.collection.objects.link(obj)
    aim_at(obj, target)
    return obj


def build_lighting():
    target = (-0.045, -0.050, 0.012)
    add_area_light("Key", (0.52, -0.62, 0.62), target, 30.0, 1.30,
                   (1.0, 0.93, 0.82), 'SQUARE')
    add_area_light("Fill", (-0.78, -0.42, 0.42), target, 8.0, 1.60,
                   (0.82, 0.89, 1.0), 'SQUARE')
    add_area_light("Rim", (-0.34, 0.72, 0.52), target, 24.0, 0.60,
                   (1.0, 0.90, 0.76), 'RECTANGLE', size_y=0.90)
    add_area_light("Top", (0.0, -0.05, 0.95), target, 10.0, 1.10,
                   (1.0, 0.97, 0.92), 'SQUARE')
    log("Lighting built: soft key + fill + rim + top (all procedural area lights).")


def build_world():
    world = bpy.data.worlds.new("World")
    bpy.context.scene.world = world
    world.use_nodes = True
    nt = world.node_tree
    for n in list(nt.nodes):
        nt.nodes.remove(n)
    out = new_node(nt, 'ShaderNodeOutputWorld', 'Output', (600, 0))
    bg = new_node(nt, 'ShaderNodeBackground', 'Background', (380, 0))
    link(nt, bg.outputs['Background'], out.inputs['Surface'])

    tc = new_node(nt, 'ShaderNodeTexCoord', 'TexCoord', (-500, 0))
    sep = new_node(nt, 'ShaderNodeSeparateXYZ', 'Sep', (-320, 0))
    link(nt, tc.outputs['Generated'], sep.inputs['Vector'])
    mr = new_node(nt, 'ShaderNodeMapRange', 'Map', (-140, 0))
    mr.inputs['From Min'].default_value = -1.0
    mr.inputs['From Max'].default_value = 1.0
    mr.inputs['To Min'].default_value = 0.0
    mr.inputs['To Max'].default_value = 1.0
    link(nt, sep.outputs['Z'], mr.inputs['Value'])
    cr = make_ramp(nt, 'SkyRamp', (60, 0),
                   [(0.0, (0.006, 0.006, 0.008, 1)),
                    (0.45, (0.020, 0.019, 0.022, 1)),
                    (1.0, (0.075, 0.070, 0.075, 1))])
    link(nt, mr.outputs['Result'], cr.inputs['Fac'])
    link(nt, cr.outputs['Color'], bg.inputs['Color'])
    bg.inputs['Strength'].default_value = 0.35
    log("World built: procedural vertical gradient (no HDRI).")


def build_camera():
    cd = bpy.data.cameras.new("Camera")
    cd.lens = 85.0
    cd.sensor_width = 36.0
    cd.dof.use_dof = True
    cd.dof.aperture_fstop = 4.5
    try:
        cd.dof.aperture_blades = 9
        cd.dof.aperture_rotation = math.radians(20)
        cd.dof.aperture_ratio = 1.0
    except Exception:
        pass

    cam = bpy.data.objects.new("Camera", cd)
    cam.location = Vector((0.240, -0.300, 0.150))
    bpy.context.scene.collection.objects.link(cam)
    aim_at(cam, (-0.048, -0.052, 0.006))
    bpy.context.scene.camera = cam

    focus = bpy.data.objects.new("FocusTarget", None)
    focus.location = Vector((-0.064, -0.072, 0.006))
    bpy.context.scene.collection.objects.link(focus)
    cd.dof.focus_object = focus
    log("Camera built: 85 mm, f/2.8, DoF focused on leading folded edge.")
    return cam


def build_volume():
    try:
        me = bpy.data.meshes.new("SteamVolumeMesh")
        bm = bmesh.new()
        try:
            bmesh.ops.create_uvsphere(bm, u_segments=32, v_segments=16, radius=0.150)
        except TypeError:
            bmesh.ops.create_uvsphere(bm, u_segments=32, v_segments=16, diameter=0.150)
        bm.to_mesh(me)
        bm.free()
        obj = bpy.data.objects.new("SteamVolume", me)
        obj.location = Vector((-0.035, -0.040, 0.045))
        bpy.context.scene.collection.objects.link(obj)
        obj.data.materials.append(make_steam_material())
        log("Subtle volumetric steam domain added.")
        return obj
    except Exception as e:
        warn("volume skipped: %r" % e)
        return None


# --------------------------------------------------------------------------- #
# Render settings
# --------------------------------------------------------------------------- #

def configure_gpu(scene):
    scene.render.engine = 'CYCLES'
    chosen = "CPU"
    names = []
    try:
        prefs = bpy.context.preferences.addons.get('cycles')
        if prefs is not None:
            cp = prefs.preferences
            for dev in ('OPTIX', 'CUDA'):
                try:
                    cp.compute_device_type = dev
                    cp.get_devices()
                    gpus = [d for d in cp.devices if d.type == dev]
                    if gpus:
                        scene.cycles.device = 'GPU'
                        for d in cp.devices:
                            d.use = (d.type == dev)
                        chosen = dev
                        names = [d.name for d in gpus]
                        break
                except Exception as e:
                    warn("GPU probe %s failed: %r" % (dev, e))
    except Exception as e:
        warn("GPU setup failed (%r); falling back to CPU." % e)
    if chosen == "CPU":
        try:
            scene.cycles.device = 'CPU'
        except Exception:
            pass
    log("Cycles device: %s %s" % (chosen, names if names else ""))
    return chosen


def setup_render(args):
    scene = bpy.context.scene
    configure_gpu(scene)

    res = list(args.resolution)
    if args.fast:
        res = [960, 540]
    scene.render.resolution_x = res[0]
    scene.render.resolution_y = res[1]
    scene.render.resolution_percentage = 100
    scene.render.image_settings.file_format = 'PNG'
    scene.render.image_settings.color_mode = 'RGB'
    scene.render.image_settings.color_depth = '16'
    scene.render.film_transparent = False

    scene.cycles.samples = 24 if args.fast else args.samples
    scene.cycles.use_adaptive_sampling = True
    scene.cycles.adaptive_threshold = 0.01
    scene.cycles.use_denoising = True
    try:
        scene.cycles.denoiser = 'OPENIMAGEDENOISE'
        scene.cycles.denoising_input_passes = 'RGB_ALBEDO_NORMAL'
        scene.cycles.denoising_prefilter = 'ACCURATE'
    except Exception as e:
        warn("denoiser options: %r" % e)
    scene.cycles.seed = args.seed
    scene.cycles.use_animated_seed = False
    try:
        scene.cycles.use_light_tree = True
    except Exception:
        pass

    try:
        scene.view_settings.view_transform = 'AgX'
        scene.view_settings.look = 'AgX - Medium High Contrast'
    except Exception:
        try:
            scene.view_settings.view_transform = 'Filmic'
        except Exception:
            pass
    scene.view_settings.exposure = -0.30
    scene.display_settings.display_device = 'sRGB'
    log("Render: Cycles %dx%d, %d samples, AgX, 16-bit PNG."
        % (res[0], res[1], scene.cycles.samples))


# --------------------------------------------------------------------------- #
# Diagnostics
# --------------------------------------------------------------------------- #

def mesh_stats(obj, raw=False):
    disp = []
    if raw:
        for m in obj.modifiers:
            if m.type in ('SOLIDIFY', 'SUBSURF'):
                disp.append((m, m.show_viewport))
                m.show_viewport = False
        bpy.context.view_layer.update()
    try:
        dg = bpy.context.evaluated_depsgraph_get()
        oe = obj.evaluated_get(dg)
        me = oe.to_mesh()
        empty = len(me.vertices) == 0
        if empty:
            oe.to_mesh_clear()
            return {}
        xs = [v.co.x for v in me.vertices]
        ys = [v.co.y for v in me.vertices]
        zs = [v.co.z for v in me.vertices]
        q = [0, 0, 0, 0]
        for v in me.vertices:
            if v.co.x <= 0 and v.co.y <= 0:
                q[0] += 1
            elif v.co.x > 0 and v.co.y <= 0:
                q[1] += 1
            elif v.co.x <= 0 and v.co.y > 0:
                q[2] += 1
            else:
                q[3] += 1
        stats = {
            'verts': len(me.vertices),
            'x': (min(xs), max(xs)),
            'y': (min(ys), max(ys)),
            'z': (min(zs), max(zs)),
            'span': (max(xs) - min(xs), max(ys) - min(ys), max(zs) - min(zs)),
            'quadrants_[--,-+,+-,++]': q,
        }
        oe.to_mesh_clear()
        return stats
    finally:
        for m, vis in disp:
            m.show_viewport = vis
        if disp:
            bpy.context.view_layer.update()


def report_stats(torch, label="diagnostics"):
    log("---- %s ----" % label)
    st = mesh_stats(torch)
    for k, v in st.items():
        log("  %-28s %s" % (k, v))
    log("---------------------------------------------------")
    return st


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def run(args):
    reset_scene()

    outdir = args.outdir or os.path.join(_HERE, "output")
    os.makedirs(outdir, exist_ok=True)

    hw = "%s | %s | Blender %s" % (platform.platform(), platform.machine(),
                                   bpy.app.version_string)
    log("Host: " + hw)

    board = build_board(skip_collision=True)
    collision_plane = build_collision_plane()
    table = build_table()
    torch = build_tortilla()

    board.data.materials.append(make_wood_material())
    table.data.materials.append(make_table_material())

    build_lighting()
    build_world()
    cam = build_camera()
    if not args.no_volume:
        build_volume()

    setup_render(args)

    # ---- initial cloth settle (flat tortilla on the board) -------------
    log("=== Cloth settle: flat tortilla ===")
    add_cloth(torch, args)
    bake_and_apply(torch, SETTLE_FRAMES, "settle", colliders=None)
    report_stats(torch, "after settle")

    # ---- phase 1: fold x>0 onto x<0 -------------------------------------
    log("=== Fold 1: x>0 -> x<0 (hinge = Y axis) ===")
    fold_mesh(torch, 'x', math.pi, 0.0015, 0.012, 0.0022)
    stats1 = report_stats(torch, "after fold 1")

    # ---- phase 2: fold y>0 onto y<0 -------------------------------------
    log("=== Fold 2: y>0 -> y<0 (hinge = X axis) ===")
    fold_mesh(torch, 'y', math.pi, 0.0030, 0.010, 0.0022)
    stats2 = report_stats(torch, "after fold 2")

    # ---- shell (thickness) + smoothing for the final render -------------
    sol = torch.modifiers.new("Solidify", 'SOLIDIFY')
    sol.thickness = THICKNESS
    sol.offset = 1.0
    sol.use_rim = True
    sol.use_even_offset = False
    try:
        sol.vertex_group = "thick"
        sol.thickness_vertex_group = 1.0
    except Exception as e:
        warn("solidify vertex group: %r" % e)
    sub = torch.modifiers.new("Subsurf", 'SUBSURF')
    sub.levels = 2
    sub.render_levels = 2

    torch.data.materials.append(make_tortilla_material())
    bpy.context.view_layer.update()
    stats = report_stats(torch, "final shell")
    stats["fold1"] = stats1
    stats["fold2"] = stats2

    # ---- material / UV sanity checks ------------------------------------
    log("  tortilla UV layers: %s" % [uv.name for uv in torch.data.uv_layers])
    if torch.data.uv_layers:
        uvl = torch.data.uv_layers[0]
        n = len(uvl.data)
        samples = [tuple(round(c, 3) for c in uvl.data[i].uv)
                   for i in range(0, n, max(1, n // 6))]
        log("  sample UVs: %s" % samples)
    log("  tortilla materials: %s" % [m.name for m in torch.data.materials])
    if torch.data.materials:
        mat = torch.data.materials[0]
        log("  material nodes: %d, links: %d"
            % (len(mat.node_tree.nodes), len(mat.node_tree.links)))
        for nd in mat.node_tree.nodes:
            if nd.type == 'TEX_NOISE':
                try:
                    log("    noise %-10s scale=%.1f detail=%.1f"
                        % (nd.label, nd.inputs['Scale'].default_value,
                           nd.inputs['Detail'].default_value))
                except Exception:
                    pass

    # ---- save .blend after baking ---------------------------------------
    blend_path = os.path.join(outdir, "tortilla_baked.blend")
    bpy.ops.wm.save_as_mainfile(filepath=blend_path)
    log("Saved .blend: %s" % blend_path)

    # ---- info file -------------------------------------------------------
    info_path = os.path.join(outdir, "run_info.txt")
    with open(info_path, "w", encoding="utf-8") as f:
        f.write("Tortilla benchmark run info\n")
        f.write("host: %s\n" % hw)
        f.write("cloth: gravity settle baked 1-%d frames; folds = scripted rounded\n"
                % SETTLE_FRAMES)
        f.write("       hinge deformations; final pose applied to the mesh\n")
        f.write("stats: %r\n" % (stats,))
        f.write("runtime so far: %.1fs\n" % (time.time() - START_TIME))
    log("Wrote %s" % info_path)

    if args.bake_only:
        log("--bake-only requested: skipping render.")
        return outdir

    # ---- render ----------------------------------------------------------
    png_path = os.path.join(outdir, "tortilla_final.png")
    bpy.context.scene.render.filepath = png_path
    log("Rendering final image -> %s" % png_path)
    t0 = time.time()
    bpy.ops.render.render(write_still=True)
    log("Render finished in %.1fs." % (time.time() - t0))

    # re-save the blend with the final frame set
    bpy.ops.wm.save_as_mainfile(filepath=blend_path)

    with open(info_path, "a", encoding="utf-8") as f:
        f.write("total runtime: %.1fs\n" % (time.time() - START_TIME))
    return outdir


def main():
    args = parse_args()
    log("=== build_tortilla.py starting ===")
    log("argv: %s" % " ".join(sys.argv))
    try:
        outdir = run(args)
    except Exception:
        traceback.print_exc()
        log("FAILED after %.1fs." % (time.time() - START_TIME))
        sys.exit(1)
    log("=== done in %.1fs; outputs in %s ===" % (time.time() - START_TIME, outdir))
    sys.exit(0)


if __name__ == "__main__":
    main()
