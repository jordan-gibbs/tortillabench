#!/usr/bin/env python3
"""Procedural flour tortilla, two guided cloth folds, Cycles food photograph.
Run: blender --background --factory-startup --python build_tortilla.py
All geometry, materials, animation and lighting are generated here.
"""
import argparse
import json
import math
import os
import platform
import random
import subprocess
import sys
import time
import traceback
from pathlib import Path
import bpy
from mathutils import Vector, noise

SEED = 731904
ROOT = Path(__file__).resolve().parent
START = time.perf_counter()
SCALE = 0.1  # simulate at 10x for robust millimetre-thick collision shells
RADIUS = 1.12
LAST_FRAME = 132


def log(message):
    print(f'[TORTILLA {time.perf_counter()-START:8.2f}s] {message}', flush=True)


def arguments():
    p = argparse.ArgumentParser()
    p.add_argument('--output', default='output/final')
    p.add_argument('--width', type=int, default=3840)
    p.add_argument('--samples', type=int, default=384)
    p.add_argument('--bake-only', action='store_true')
    return p.parse_args(sys.argv[sys.argv.index('--')+1:] if '--' in sys.argv else [])


def node(mat, kind, name, x=0, y=0):
    n = mat.node_tree.nodes.new(kind)
    n.name = name
    n.label = name
    n.location = (x, y)
    return n


def link(mat, a, out, b, inp):
    mat.node_tree.links.new(a.outputs[out], b.inputs[inp])


def material(name):
    m = bpy.data.materials.new(name)
    m.use_nodes = True
    m.node_tree.nodes.clear()
    out = node(m, 'ShaderNodeOutputMaterial', 'Surface output', 1100, 100)
    bs = node(m, 'ShaderNodeBsdfPrincipled', 'Physical surface', 850, 100)
    link(m, bs, 'BSDF', out, 'Surface')
    return m, bs


def ramp(m, name, stops, x=0, y=0):
    n = node(m, 'ShaderNodeValToRGB', name, x, y)
    cr = n.color_ramp
    cr.interpolation = 'EASE'
    for e in list(cr.elements)[2:]:
        cr.elements.remove(e)
    for i, (p, c) in enumerate(stops):
        e = cr.elements[i] if i < 2 else cr.elements.new(p)
        e.position = p
        e.color = (*c, 1) if len(c) == 3 else c
    return n


def mathnode(m, name, op, a=None, b=None):
    n = node(m, 'ShaderNodeMath', name)
    n.operation = op
    if a is not None: n.inputs[0].default_value = a
    if b is not None: n.inputs[1].default_value = b
    return n


def tortilla_material():
    m, bs = material('Tortilla | flour, toasted bubbles and warm crumb')
    bs.inputs['Roughness'].default_value = 0.56
    bs.inputs['IOR'].default_value = 1.43
    bs.inputs['Subsurface Weight'].default_value = 0.075
    bs.inputs['Subsurface Radius'].default_value = (1.0, 0.48, 0.22)
    bs.inputs['Subsurface Scale'].default_value = 0.0012
    bs.inputs['Specular IOR Level'].default_value = 0.27
    uv = node(m, 'ShaderNodeTexCoord', 'Original round dough coordinates', -1200, 100)
    vor = node(m, 'ShaderNodeTexVoronoi', 'Uneven griddle blister positions', -950, 320)
    vor.voronoi_dimensions = '2D'
    vor.inputs['Scale'].default_value = 13.5
    vor.inputs['Randomness'].default_value = 1.0
    link(m, uv, 'UV', vor, 'Vector')
    breakup = node(m, 'ShaderNodeTexNoise', 'Ragged blister borders', -950, 50)
    breakup.inputs['Scale'].default_value = 85
    breakup.inputs['Detail'].default_value = 3.2
    breakup.inputs['Roughness'].default_value = 0.72
    link(m, uv, 'UV', breakup, 'Vector')
    rag = mathnode(m, 'Edge noise amplitude', 'MULTIPLY', b=0.22)
    link(m, breakup, 'Fac', rag, 0)
    d = mathnode(m, 'Irregular radial distance', 'ADD')
    link(m, vor, 'Distance', d, 0)
    link(m, rag, 0, d, 1)
    bw = node(m, 'ShaderNodeRGBToBW', 'Different size for every browned blister')
    link(m, vor, 'Color', bw, 'Color')
    sz = mathnode(m, 'Blister size variation', 'MULTIPLY', b=0.30)
    link(m, bw, 0, sz, 0)
    vary = mathnode(m, 'Varied toasted islands', 'SUBTRACT')
    link(m, d, 0, vary, 0)
    link(m, sz, 0, vary, 1)
    toast = ramp(m, 'Char to toasted gold to pale dough', [
        (0.01, (0.035, 0.013, 0.004)),
        (0.095, (0.14, 0.052, 0.015)),
        (0.16, (0.29, 0.115, 0.032)),
        (0.22, (0.54, 0.285, 0.105)),
        (0.275, (0.77, 0.55, 0.295)),
        (0.345, (0.86, 0.735, 0.53)),
        (0.53, (0.91, 0.80, 0.62))], -380, 300)
    link(m, vary, 0, toast, 'Fac')
    broad = node(m, 'ShaderNodeTexNoise', 'Slow natural dough colour variations', -930, -200)
    broad.inputs['Scale'].default_value = 7
    broad.inputs['Detail'].default_value = 3
    link(m, uv, 'UV', broad, 'Vector')
    tint = ramp(m, 'Unbleached flour mottling', [(0.15, (0.60,0.44,0.25)),(0.80,(1.0,0.95,0.83))], -380, -100)
    link(m, broad, 'Fac', tint, 'Fac')
    mix = node(m, 'ShaderNodeMixRGB', 'Subtle uneven cooking', 130, 280)
    mix.blend_type = 'MULTIPLY'
    mix.inputs[0].default_value = 0.35
    link(m, toast, 'Color', mix, 1)
    link(m, tint, 'Color', mix, 2)
    flour = node(m, 'ShaderNodeTexNoise', 'Fine flour dust and pores', -650, -480)
    flour.inputs['Scale'].default_value = 750
    flour.inputs['Detail'].default_value = 2
    flour.inputs['Roughness'].default_value = 0.68
    link(m, uv, 'UV', flour, 'Vector')
    rough = ramp(m, 'Flour roughness variation', [(0.22,(0.44,)*3),(0.78,(0.68,)*3)], 160, -100)
    link(m, flour, 'Fac', rough, 'Fac')
    link(m, rough, 'Color', bs, 'Roughness')
    dust = ramp(m, 'Tiny flour remnants', [(0.61,(0,)*3),(0.78,(0.23,)*3)], 20, -280)
    link(m, flour, 'Fac', dust, 'Fac')
    fmix = node(m, 'ShaderNodeMixRGB', 'Flour on raised surface', 500, 300)
    link(m, dust, 'Color', fmix, 0)
    link(m, mix, 'Color', fmix, 1)
    fmix.inputs[2].default_value = (0.96,0.89,0.73,1)
    link(m, fmix, 'Color', bs, 'Base Color')
    pores = node(m, 'ShaderNodeBump', 'Minute flour surface relief', 480, -240)
    pores.inputs['Strength'].default_value = 0.20
    pores.inputs['Distance'].default_value = 0.00011
    link(m, flour, 'Fac', pores, 'Height')
    bubbles = node(m, 'ShaderNodeBump', 'Griddle blister skin', 680, -100)
    bubbles.inputs['Strength'].default_value = 0.22
    bubbles.inputs['Distance'].default_value = 0.00045
    bubbles.invert = True
    link(m, vary, 0, bubbles, 'Height')
    link(m, pores, 'Normal', bubbles, 'Normal')
    link(m, bubbles, 'Normal', bs, 'Normal')
    return m


def wood_material(name, tint=1.0):
    m, bs = material(name)
    bs.inputs['Roughness'].default_value = 0.37
    bs.inputs['Specular IOR Level'].default_value = 0.30
    bs.inputs['Coat Weight'].default_value = 0.11
    bs.inputs['Coat Roughness'].default_value = 0.43
    tc = node(m, 'ShaderNodeTexCoord', 'Wood block coordinates', -1100, 150)
    mapping = node(m, 'ShaderNodeVectorMath', 'Long fibres along the board', -900, 150)
    mapping.operation = 'MULTIPLY'
    mapping.inputs[1].default_value = (2.8, 110.0, 9.0)
    link(m, tc, 'Generated', mapping, 0)
    grain = node(m, 'ShaderNodeTexNoise', 'Wandering fine timber fibres', -700, 180)
    grain.inputs['Scale'].default_value = 1
    grain.inputs['Detail'].default_value = 3
    grain.inputs['Roughness'].default_value = 0.70
    grain.inputs['Distortion'].default_value = 1.3
    link(m, mapping, 'Vector', grain, 'Vector')
    colors = ramp(m, 'Honey and umber grain', [
        (0.15, tuple(tint*x for x in (0.055,0.022,0.009))),
        (0.36, tuple(tint*x for x in (0.13,0.060,0.023))),
        (0.55, tuple(tint*x for x in (0.27,0.145,0.064))),
        (0.78, tuple(tint*x for x in (0.38,0.23,0.115)))], -220, 260)
    link(m, grain, 'Fac', colors, 'Fac')
    macro = node(m, 'ShaderNodeTexNoise', 'Broad changes across the growth rings', -700, -120)
    macro.inputs['Scale'].default_value = 3.7
    macro.inputs['Detail'].default_value = 2
    link(m, tc, 'Generated', macro, 'Vector')
    macrocol = ramp(m, 'Oil absorbed unevenly', [(0.1,(0.38,0.27,0.17)),(0.85,(1.0,0.9,0.72))])
    link(m, macro, 'Fac', macrocol, 'Fac')
    mix = node(m, 'ShaderNodeMixRGB', 'Living timber colour', 330, 250)
    mix.blend_type = 'MULTIPLY'
    mix.inputs[0].default_value = 0.34
    link(m, colors, 'Color', mix, 1)
    link(m, macrocol, 'Color', mix, 2)
    link(m, mix, 'Color', bs, 'Base Color')
    bump = node(m, 'ShaderNodeBump', 'Pores in oiled wood', 430, -60)
    bump.inputs['Strength'].default_value = 0.19
    bump.inputs['Distance'].default_value = 0.00018
    link(m, grain, 'Fac', bump, 'Height')
    scar_map = node(m, 'ShaderNodeVectorMath', 'Fine crossing knife marks')
    scar_map.operation = 'MULTIPLY'
    scar_map.inputs[1].default_value = (85, 2.5, 3)
    link(m, tc, 'Generated', scar_map, 0)
    scars = node(m, 'ShaderNodeTexNoise', 'Short broken knife scratches')
    scars.inputs['Scale'].default_value = 4
    scars.inputs['Detail'].default_value = 2
    link(m, scar_map, 0, scars, 'Vector')
    scar = ramp(m, 'Sparse worn cuts', [(0.65,(0,)*3),(0.73,(1,)*3)])
    link(m, scars, 'Fac', scar, 'Fac')
    b2 = node(m, 'ShaderNodeBump', 'Shallow knife wear', 650, -80)
    b2.invert = True
    b2.inputs['Strength'].default_value = 0.15
    b2.inputs['Distance'].default_value = 0.00008
    link(m, scar, 'Color', b2, 'Height')
    link(m, bump, 'Normal', b2, 'Normal')
    link(m, b2, 'Normal', bs, 'Normal')
    return m


def cube(name, location, dimensions, mat, bevel=0):
    bpy.ops.mesh.primitive_cube_add(size=1, location=location)
    o = bpy.context.object
    o.name = name
    o.dimensions = dimensions
    bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)
    if mat: o.data.materials.append(mat)
    if bevel:
        b = o.modifiers.new('Hand softened board edges', 'BEVEL')
        b.width = bevel
        b.segments = 5
        for p in o.data.polygons: p.use_smooth = True
        n = o.modifiers.new('Weighted broad face normals', 'WEIGHTED_NORMAL')
        n.keep_sharp = True
    return o


def smoothstep(x):
    x = max(0, min(1, x))
    return x*x*(3-2*x)


def bend(t, z, angle, width):
    # Isometric cylindrical hinge with a finite, rounded bend radius.
    if angle < 1e-6 or t <= -width/2:
        return t, z
    s = t+width/2
    k = angle/width
    theta = angle*min(1, s/width)
    if s < width:
        q = -width/2 + math.sin(theta)/k
        h = (1-math.cos(theta))/k
    else:
        q = -width/2 + math.sin(angle)/k + (s-width)*math.cos(angle)
        h = (1-math.cos(angle))/k + (s-width)*math.sin(angle)
    return q-z*math.sin(theta), h+z*math.cos(theta)


def guided_position(x, y, frame):
    a = math.pi*smoothstep((frame-1)/48)
    b = math.pi*smoothstep((frame-55)/56)
    # Small dough irregularities travel with the sheet through both folds.
    n = noise.noise_vector(Vector((x*6+2.3,y*6-1.1,0.9))).z
    surface = 0.0025*n + 0.0015*math.sin(18*x+7*y)
    xx, z = bend(x, surface, a, 0.071)
    yy, z = bend(y, z, b, 0.205)
    progress = smoothstep((frame-55)/56)
    # Gradual small lift at the open arc, with asymmetric natural drape.
    rr = math.hypot(x,y)/RADIUS
    upper = smoothstep((z-0.065)/0.055)
    lift = progress*upper*(0.012+0.014*math.sin(7*xx+4*yy)**2)*smoothstep((rr-0.64)/0.36)
    return (xx, yy, 0.015+z+lift)


def make_tortilla(mat):
    n = 84
    verts, faces, original = [], [], []
    for j in range(n+1):
        v = 2*j/n-1
        for i in range(n+1):
            u = 2*i/n-1
            x = RADIUS*u*math.sqrt(1-v*v/2)
            y = RADIUS*v*math.sqrt(1-u*u/2)
            angle = math.atan2(y,x)
            radial = math.hypot(x,y)/RADIUS
            wobble = 1+radial**4*(0.009*math.sin(9*angle+0.4)+0.005*math.sin(17*angle-0.7)+0.003*math.sin(31*angle))
            x *= wobble; y *= wobble
            original.append((x,y))
            verts.append(guided_position(x,y,1))
    for j in range(n):
        for i in range(n):
            a = j*(n+1)+i
            faces.append((a,a+1,a+n+2,a+n+1))
    mesh = bpy.data.meshes.new('Single continuous circular tortilla | 7056 quads')
    mesh.from_pydata(verts, [], faces)
    mesh.update()
    o = bpy.data.objects.new('Tortilla simulation | two sequential folds', mesh)
    bpy.context.collection.objects.link(o)
    o.data.materials.append(mat)
    for p in mesh.polygons: p.use_smooth = True
    uv = mesh.uv_layers.new(name='Original dough disc')
    for p in mesh.polygons:
        for li in p.loop_indices:
            x,y = original[mesh.loops[li].vertex_index]
            uv.data[li].uv = (0.5+x/(2*RADIUS),0.5+y/(2*RADIUS))
    pins = o.vertex_groups.new(name='Soft folding grips | free edge drape')
    thick = o.vertex_groups.new(name='Uneven rolled dough thickness')
    for idx,(x,y) in enumerate(original):
        i,j = idx%(n+1),idx//(n+1)
        rr = math.hypot(x,y)/RADIUS
        hinge = min(abs(x)/0.065,abs(y)/0.13)
        weight = 0.84 if rr < 0.93 else 0.40
        if hinge < 1.1: weight = 0.96
        if i%5 == 0 and j%5 == 0 and rr < 0.98: weight = 1.0
        pins.add([idx], weight, 'REPLACE')
        thick.add([idx], 0.76+0.24*(0.5+0.5*math.sin(x*19+y*11)), 'REPLACE')
    o.shape_key_add(name='Basis | round unfolded 22.4 cm dough')
    frames = list(range(7,112,6))
    if frames[-1] != 111: frames.append(111)
    for k,frame in enumerate(frames):
        sk = o.shape_key_add(name=f'Fold guide frame {frame:03d}')
        for idx,(x,y) in enumerate(original):
            sk.data[idx].co = guided_position(x,y,frame)
        prev = frames[k-1] if k else 1
        dr = sk.driver_add('value').driver
        if k == len(frames)-1:
            dr.expression = f'max(0,min(1,(frame-{prev})/{frame-prev}))'
        else:
            nex = frames[k+1]
            dr.expression = f'max(0,min((frame-{prev})/{frame-prev},({nex}-frame)/{nex-frame}))'
    c = o.modifiers.new('Baked cloth | first fold then second fold', 'CLOTH')
    s = c.settings
    s.quality = 8
    s.mass = 0.025
    s.tension_stiffness = 35
    s.compression_stiffness = 35
    s.shear_stiffness = 25
    s.bending_stiffness = 0.65
    s.tension_damping = 7
    s.compression_damping = 7
    s.shear_damping = 7
    s.bending_damping = 0.8
    s.air_damping = 4
    s.vertex_group_mass = pins.name
    s.pin_stiffness = 50
    s.use_dynamic_mesh = False
    col = c.collision_settings
    col.use_collision = True
    col.distance_min = 0.013
    col.collision_quality = 6
    col.friction = 7
    col.use_self_collision = True
    col.self_distance_min = 0.010
    col.self_friction = 5
    c.point_cache.frame_start = 1
    c.point_cache.frame_end = LAST_FRAME
    o['method'] = 'Cloth solver, continuously deforming pin targets, two isometric rounded folds; free and soft-pinned boundary vertices'
    o['real_unfolded_diameter_m'] = 2*RADIUS*SCALE
    o['rendered_thickness_m'] = 0.0023
    return o,c


def baked_surface(source, cloth, outdir):
    scene = bpy.context.scene
    bpy.ops.object.select_all(action='DESELECT')
    source.select_set(True)
    bpy.context.view_layer.objects.active = source
    scene.frame_set(1)
    log(f'Baking cloth: {len(source.data.vertices)} vertices, frames 1–{LAST_FRAME}, two successive folds')
    bake_start = time.perf_counter()
    def progress(scene, depsgraph=None):
        if scene.frame_current%12 == 0:
            log(f'Cloth bake frame {scene.frame_current}/{LAST_FRAME}')
    bpy.app.handlers.frame_change_post.append(progress)
    try:
        with bpy.context.temp_override(scene=scene, object=source, active_object=source, point_cache=cloth.point_cache):
            bpy.ops.ptcache.bake(bake=True)
    finally:
        bpy.app.handlers.frame_change_post.remove(progress)
    if not cloth.point_cache.is_baked:
        raise RuntimeError('Cloth cache did not reach baked state')
    scene.frame_set(LAST_FRAME)
    bpy.context.view_layer.update()
    evaluated = source.evaluated_get(bpy.context.evaluated_depsgraph_get())
    mesh = bpy.data.meshes.new_from_object(evaluated, preserve_all_data_layers=True, depsgraph=bpy.context.evaluated_depsgraph_get())
    for v in mesh.vertices:
        if not all(math.isfinite(q) for q in v.co): raise RuntimeError('Non-finite cloth vertex')
    bounds = [[min(v.co[i] for v in mesh.vertices),max(v.co[i] for v in mesh.vertices)] for i in range(3)]
    log(f'Cloth baked in {time.perf_counter()-bake_start:.1f}s; final solver bounds {bounds}')
    if any(hi-lo > 3 for lo,hi in bounds): raise RuntimeError('Cloth simulation became unstable')
    o = bpy.data.objects.new('Fresh flour tortilla | baked final frame',mesh)
    bpy.context.collection.objects.link(o)
    for p in mesh.polygons: p.use_smooth = True
    # Keep the original animated solver and its baked cache for inspection.
    source.hide_render = True
    source.hide_set(True)
    source['cache_baked'] = bool(cloth.point_cache.is_baked)
    source['bake_seconds'] = time.perf_counter()-bake_start
    o['cloth_source'] = source.name
    o['applied_cloth_frame'] = LAST_FRAME
    # Restore thickness group on the evaluated snapshot.
    thickness = o.vertex_groups.new(name='Natural thickness variation')
    for v in mesh.vertices:
        x,y,z = v.co
        thickness.add([v.index],0.76+0.24*(0.5+0.5*math.sin(x*19+y*11)),'REPLACE')
    smooth = o.modifiers.new('Relax tiny solver creases','SMOOTH')
    smooth.factor = 0.4; smooth.iterations = 3
    sub = o.modifiers.new('Continuous soft cooked dough','SUBSURF')
    sub.levels = 2; sub.render_levels = 2
    tex = bpy.data.textures.new('Procedural dough micro blisters', type='CLOUDS')
    tex.noise_scale = 0.075
    tex.noise_depth = 2
    disp = o.modifiers.new('Small uneven puffed blisters','DISPLACE')
    disp.texture = tex
    disp.strength = 0.009
    disp.mid_level = 0.5
    disp.texture_coords = 'UV'
    disp.uv_layer = 'Original dough disc'
    solid = o.modifiers.new('Rolled dough | 1.8–2.3 mm thick','SOLIDIFY')
    solid.thickness = 0.023
    solid.offset = 0
    solid.vertex_group = thickness.name
    solid.thickness_vertex_group = 0.0
    solid.use_even_offset = False
    solid.use_quality_normals = True
    # Seat the physically baked surface after adding its real render thickness.
    bpy.context.view_layer.update()
    dg = bpy.context.evaluated_depsgraph_get()
    ev = o.evaluated_get(dg)
    em = ev.to_mesh()
    minz = min(v.co.z for v in em.vertices)
    ev.to_mesh_clear()
    o.location.z = 0.0003-minz
    log(f'Final thickness contact correction {o.location.z*SCALE*1000:.3f} mm; board clearance 0.030 mm')
    report = dict(vertices=len(mesh.vertices),quads=len(mesh.polygons),frame=LAST_FRAME,cache_baked=True,
                  solver_bounds=bounds,bake_seconds=time.perf_counter()-bake_start,
                  contact_correction_mm=o.location.z*SCALE*1000,diameter_m=2*RADIUS*SCALE,
                  thickness_m=[0.0018,0.0023])
    (outdir/'simulation.json').write_text(json.dumps(report,indent=2))
    return o


def point_at(obj, target):
    obj.rotation_euler = (Vector(target)-obj.location).to_track_quat('-Z','Y').to_euler()


def area(name, location, target, energy, size, color, size_y=None):
    data = bpy.data.lights.new(name,'AREA')
    data.energy = energy
    data.color = color
    if size_y:
        data.shape = 'RECTANGLE'; data.size = size; data.size_y = size_y
    else:
        data.shape = 'DISK'; data.size = size
    o = bpy.data.objects.new(name,data)
    bpy.context.collection.objects.link(o)
    o.location = location
    point_at(o,target)
    return o


def render_device(scene):
    prefs = bpy.context.preferences.addons['cycles'].preferences
    for backend in ('OPTIX','CUDA','HIP','METAL','ONEAPI'):
        try:
            prefs.compute_device_type = backend
            prefs.refresh_devices()
            gpus = [d for d in prefs.devices if d.type != 'CPU']
            if gpus:
                for d in prefs.devices: d.use = d.type == backend
                scene.cycles.device = 'GPU'
                names = [f'{d.name} ({d.type})' for d in gpus if d.use]
                log('Cycles GPU: '+', '.join(names))
                return dict(backend=backend,devices=names)
        except Exception as e:
            log(f'Backend {backend} unavailable: {type(e).__name__}')
    scene.cycles.device = 'CPU'
    log('Cycles CPU fallback: '+platform.processor())
    return dict(backend='CPU',devices=[platform.processor() or platform.machine()])


def main():
    args = arguments()
    random.seed(SEED)
    outdir = ROOT/args.output
    outdir.mkdir(parents=True,exist_ok=True)
    log('Starting self-contained tortilla construction')
    log(f'Blender {bpy.app.version_string}; {platform.platform()}; {os.cpu_count()} logical CPUs')
    try:
        result = subprocess.run(['nvidia-smi','--query-gpu=name,driver_version,memory.total','--format=csv'],capture_output=True,text=True,timeout=15)
        log('GPU hardware: '+result.stdout.strip())
    except Exception as e: log(f'GPU inventory unavailable: {type(e).__name__}')
    bpy.ops.object.select_all(action='SELECT')
    bpy.ops.object.delete(use_global=False)
    scene = bpy.context.scene
    scene.frame_start = 1; scene.frame_end = LAST_FRAME
    scene.render.fps = 30
    scene.gravity = (0,0,-9.81)
    scene.unit_settings.system = 'METRIC'
    scene.unit_settings.scale_length = 1.0
    scene.render.engine = 'CYCLES'
    hardware = render_device(scene)
    scene.cycles.samples = args.samples
    scene.cycles.seed = SEED
    scene.cycles.use_animated_seed = False
    scene.cycles.use_denoising = True
    scene.cycles.adaptive_threshold = 0.008
    scene.cycles.max_bounces = 10
    scene.cycles.diffuse_bounces = 4
    scene.cycles.glossy_bounces = 4
    scene.cycles.transmission_bounces = 6
    scene.cycles.transparent_max_bounces = 8
    scene.render.resolution_x = args.width
    scene.render.resolution_y = round(args.width*9/16)
    scene.render.resolution_percentage = 100
    scene.render.image_settings.file_format = 'PNG'
    scene.render.image_settings.color_mode = 'RGB'
    scene.render.image_settings.color_depth = '16'
    scene.render.film_transparent = False
    scene.view_settings.view_transform = 'AgX'
    try: scene.view_settings.look = 'AgX - Medium High Contrast'
    except Exception: pass
    scene.view_settings.exposure = -0.60
    scene.world.use_nodes = True
    scene.world.node_tree.nodes['Background'].inputs['Color'].default_value = (0.30,0.37,0.48,1)
    scene.world.node_tree.nodes['Background'].inputs['Strength'].default_value = 0.18
    dough = tortilla_material()
    wood = wood_material('Cutting board | oiled long-grain maple')
    board = cube('Solid maple cutting board | 34 x 26 x 2.1 cm',(-0.48,-0.40,-0.105),(3.4,2.6,0.21),wood,0.055)
    board.modifiers.new('Cloth contact with board','COLLISION')
    board.collision.thickness_outer = 0.002
    board.collision.thickness_inner = 0.001
    board.collision.cloth_friction = 9
    source,cloth = make_tortilla(dough)
    folded = baked_surface(source,cloth,outdir)
    # Retain the original collider at simulation scale for baked-cache inspection.
    sim_board = board
    sim_board.name = 'Simulation collider | original 10x board'
    board = sim_board.copy()
    board.data = sim_board.data.copy()
    board.name = 'Oiled maple cutting board | final metric render'
    bpy.context.collection.objects.link(board)
    for mod in list(board.modifiers):
        if mod.type == 'COLLISION': board.modifiers.remove(mod)
    sim_board.hide_render = True
    sim_board.hide_set(True)
    # Frozen cloth mesh is authoritative. Use metric scale for camera optics.
    for ob in (board,folded):
        ob.location *= SCALE
        ob.scale *= SCALE
    bpy.context.view_layer.update()
    dg = bpy.context.evaluated_depsgraph_get()
    ev = folded.evaluated_get(dg)
    em = ev.to_mesh()
    actual_min = min((ev.matrix_world @ v.co).z for v in em.vertices)
    ev.to_mesh_clear()
    adjustment = 0.00003-actual_min
    folded.location.z += adjustment
    audit = json.loads((outdir/'simulation.json').read_text())
    audit['metric_contact_adjustment_mm'] = adjustment*1000
    audit['final_board_clearance_mm'] = 0.030
    (outdir/'simulation.json').write_text(json.dumps(audit,indent=2))
    log(f'Metric contact audit: minimum clearance 0.030 mm, final adjustment {adjustment*1000:.5f} mm')
    # Hide the inspection source without changing its cache.
    source.hide_render = True
    tabletop,ts = material('Background | quiet charcoal plaster')
    ts.inputs['Base Color'].default_value = (0.043,0.052,0.049,1)
    ts.inputs['Roughness'].default_value = 0.72
    tc = node(tabletop,'ShaderNodeTexNoise','Subtle plaster texture')
    tc.inputs['Scale'].default_value = 180
    bu = node(tabletop,'ShaderNodeBump','Fine plaster grain')
    bu.inputs['Strength'].default_value = 0.13
    bu.inputs['Distance'].default_value = 0.00011
    link(tabletop,tc,'Fac',bu,'Height'); link(tabletop,bu,'Normal',ts,'Normal')
    cube('Quiet studio surface',(0,0,-0.024),(200,200,0.006),tabletop)
    focus = bpy.data.objects.new('Focus | leading layered tortilla edge',None)
    bpy.context.collection.objects.link(focus)
    focus.location = (-0.055,-0.100,0.010)
    camdata = bpy.data.cameras.new('82 mm macro food photograph')
    cam = bpy.data.objects.new('Camera',camdata)
    bpy.context.collection.objects.link(cam)
    cam.location = (-0.255,-0.446,0.160)
    point_at(cam,(-0.047,-0.047,0.009))
    camdata.lens = 82
    camdata.sensor_width = 36
    camdata.clip_start = 0.005
    camdata.clip_end = 200
    camdata.dof.use_dof = True
    camdata.dof.focus_object = focus
    camdata.dof.aperture_fstop = 5.6
    camdata.dof.aperture_blades = 9
    scene.camera = cam
    area('Key | broad warm window',(-0.28,-0.04,0.37),(-0.045,-0.045,0),7.5,0.26,(1.0,0.86,0.69),0.38)
    area('Fill | gentle cool bounce',(0.20,-0.28,0.17),(-0.045,-0.045,0.015),1.5,0.30,(0.78,0.86,1.0))
    area('Rim | soft strip behind folds',(0.04,0.21,0.27),(-0.045,-0.045,0.018),5.0,0.23,(1.0,0.91,0.78),0.11)
    # A restrained scattering of generated crumbs keeps the surface tactile.
    crumb_mat,cb = material('Tiny toasted dough crumbs')
    cb.inputs['Base Color'].default_value = (0.48,0.29,0.11,1)
    cb.inputs['Roughness'].default_value = 0.75
    rng = random.Random(SEED+11)
    for i in range(17):
        a = rng.uniform(math.pi*0.98,math.pi*1.61)
        rr = rng.uniform(0.116,0.139)
        x,y = rr*math.cos(a),rr*math.sin(a)
        sz = rng.uniform(0.00016,0.00046)
        bpy.ops.mesh.primitive_ico_sphere_add(subdivisions=1,radius=sz,location=(x,y,sz*0.5+0.00005))
        o=bpy.context.object; o.name=f'Dough crumb {i+1:02d}'
        o.scale=(1.4,0.75,0.55); o.rotation_euler=(rng.random(),rng.random(),rng.random()*6)
        o.data.materials.append(crumb_mat)
    scene.render.filepath = str(outdir/'tortilla.png')
    # The .blend contains an inspectable simulation source plus its final snapshot.
    scene['benchmark'] = 'PROCEDURAL-PHOTOREAL-01'
    scene['seed'] = SEED
    scene['simulation_scale'] = '10x, final snapshot and board converted to metres'
    scene['assets'] = 'All generated by build_tortilla.py; no image textures or external assets'
    scene['render_device'] = json.dumps(hardware)
    text = bpy.data.texts.new('build_tortilla.py')
    text.write(Path(__file__).read_text())
    bpy.context.view_layer.objects.active = folded
    bpy.ops.object.select_all(action='DESELECT')
    folded.select_set(True)
    blend = outdir/'tortilla.blend'
    bpy.ops.wm.save_as_mainfile(filepath=str(blend))
    log(f'Saved baked scene: {blend}')
    if args.bake_only:
        log('BAKE ONLY: no render executed and no rendered image produced')
    else:
        log(f'Rendering {args.width}x{scene.render.resolution_y}, {args.samples} samples, 16-bit PNG')
        bpy.ops.render.render(write_still=True)
        log(f'Image saved: {scene.render.filepath}')
    report = dict(blender=bpy.app.version_string,hardware=hardware,seed=SEED,
                  resolution=[args.width,scene.render.resolution_y],samples=args.samples,
                  bake_only=args.bake_only,total_seconds=time.perf_counter()-START,
                  image=None if args.bake_only else str(outdir/'tortilla.png'),blend=str(blend))
    (outdir/'run_summary.json').write_text(json.dumps(report,indent=2))
    log(f'COMPLETE | total runtime {time.perf_counter()-START:.2f}s | hardware {hardware}')


if __name__ == '__main__':
    try:
        main()
    except BaseException:
        traceback.print_exc()
        log('FAILED; exiting with status 1')
        sys.stdout.flush(); sys.stderr.flush()
        os._exit(1)
