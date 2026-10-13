"""
Builds the custom cosmetics for How to Fish in Blender and exports them for the mod.

Run headless:
  blender -b --python cosmetics.py -- <refs dir> <palette png> <output dir> [only_name]

Coordinates match the game's outfit meshes as exported by the Outfit Dumper: Z up, the face looks
toward -Y, metres, top of the head at z~1.37, X mirrored like the reference OBJs (the mod flips it
back). Every face carries a colour key:
  c1 / c2 / c3   follow the player's three colours for that slot (hat or accessory)
  skin           follows the player's skin colour
  #rrggbb        a fixed colour (snapped to the nearest cell of the game's palette)
"""
import bpy, bmesh, json, math, os, sys
from mathutils import Vector, Matrix

argv = sys.argv[sys.argv.index("--") + 1:]
REFS, PALETTE_PNG, OUT = argv[0], argv[1], argv[2]
ONLY = argv[3] if len(argv) > 3 else None
os.makedirs(OUT, exist_ok=True)

# Value the game's CharacterShader reads from uv2.x to pick a colour source
SLOT_VALUE = {"skin": 0.2, "c1": 0.4, "c2": 0.6, "c3": 0.8, "fixed": 0.5}

# Head measurements taken from the rest-pose head (rest_head.obj). The eyes and brow stick out to
# y=-0.12 up to z~1.315, so anything worn on the head has to clear that at the front.
HEAD_CY = -0.027     # front/back centre of the skull
HEAD_TOP = 1.366
EYE_X, EYE_Z, EYE_Y = 0.045, 1.283, -0.121     # EYE_Y = front surface of the eye
MOUTH = Vector((0.0, -0.112, 1.215))

# Every hat gets this after it is built: a little bigger, raised, and tilted so the front sits higher
# than the back (like the game's own hats), keeping the brim above the eyes.
HAT_FIT = {"scale": 1.04, "raise": 0.012, "tilt": math.radians(10)}

# ------------------------------------------------------------------ palette

def load_palette():
    global PAL_W, PAL_H, PIX
    img = bpy.data.images.load(PALETTE_PNG)
    PAL_W, PAL_H = img.size
    PIX = list(img.pixels)

def cell_uv(x, y):
    """Palette cell (x, y counted from the top-left, like the PNG) -> uv0."""
    return ((x + 0.5) / PAL_W, 1 - (y + 0.5) / PAL_H)

def cell_rgb(x, y):
    i = ((PAL_H - 1 - y) * PAL_W + x) * 4
    return PIX[i:i + 4]

def nearest_cell(hexcol):
    t = [int(hexcol[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    # palette pixels are linear in Blender; compare in sRGB-ish space
    def srgb(c): return c ** (1 / 2.2)
    best, bd = None, 9
    for y in range(PAL_H):
        for x in range(PAL_W):
            r, g, b, _ = cell_rgb(x, y)
            d = sum((srgb(a) - bb) ** 2 for a, bb in zip((r, g, b), t))
            if d < bd: best, bd = (x, y), d
    return best

# ------------------------------------------------------------------ mesh building

PARTS = [None, "torso", "arm", "leg", "shoe", "skirt", "robe"]

class Builder:
    """Collects faces tagged with colour keys. `preview` maps c1/c2/c3 to hex colours for renders
    (and the default uv0); in game the player's chosen colours replace them."""

    def __init__(self, name, kind, display, preview):
        self.name, self.kind, self.display = name, kind, display
        self.preview = {"skin": "#bc9971", **preview}
        self.bm = bmesh.new()
        self.mats = []
        self.part = None        # outfits: which body part new vertices belong to (decides bone weights)
        # stored on the mesh itself: BMVert Python wrappers aren't stable dictionary keys
        self.part_layer = self.bm.verts.layers.int.new("part")

    def mat(self, key):
        if key not in self.mats: self.mats.append(key)
        return self.mats.index(key)

    def v(self, p):
        v = self.bm.verts.new(Vector(p))
        v[self.part_layer] = PARTS.index(self.part) if self.part else 0
        return v

    def face(self, verts, key):
        f = self.bm.faces.new(verts)
        f.material_index = self.mat(key)
        return f

    def add_bm(self, other, key, matrix=Matrix()):
        m = self.mat(key)
        remap = {ov: self.v(matrix @ ov.co) for ov in other.verts}
        for of in other.faces:
            self.bm.faces.new([remap[v] for v in of.verts]).material_index = m
        other.free()

    def finish(self, fit=True):
        if fit and self.kind == "hat":
            f = HAT_FIT
            pivot = Vector((0, HEAD_CY, 1.30))
            rot = Matrix.Rotation(-f["tilt"], 4, 'X')      # negative X rotation lifts the front (-Y)
            for v in self.bm.verts:
                p = v.co - pivot
                p = Vector((p.x * f["scale"], p.y * f["scale"], p.z))
                v.co = pivot + (rot @ p) + Vector((0, 0, f["raise"]))
        bmesh.ops.remove_doubles(self.bm, verts=self.bm.verts, dist=1e-6)
        bmesh.ops.recalc_face_normals(self.bm, faces=self.bm.faces)
        if self.kind == "outfit":
            dl = self.bm.verts.layers.deform.verify()
            for v in self.bm.verts:
                for bone, w in outfit_weights(v.co, PARTS[v[self.part_layer]] or "torso"):
                    v[dl][bone] = w
        me = bpy.data.meshes.new(self.name)
        self.bm.to_mesh(me); self.bm.free()
        ob = bpy.data.objects.new(self.name, me)
        bpy.context.collection.objects.link(ob)
        if self.kind == "outfit":
            for name in BONE_NAMES: ob.vertex_groups.new(name=name)   # group index == game bone index
        for key in self.mats:
            mat = bpy.data.materials.new(f"{self.name}:{key}")
            hexcol = key if key.startswith("#") else self.preview[key]
            mat.diffuse_color = cell_rgb(*nearest_cell(hexcol))
            mat["key"] = key
            mat["hex"] = hexcol
            me.materials.append(mat)
        ob["kind"], ob["display_name"] = self.kind, self.display
        return ob

# ---- primitives (all return a bmesh to merge with add_bm)

def p_ico(r, sub=1, scale=(1, 1, 1)):
    bm = bmesh.new(); bmesh.ops.create_icosphere(bm, subdivisions=sub, radius=r)
    bmesh.ops.scale(bm, vec=Vector(scale), verts=bm.verts); return bm

def p_cyl(r1, r2, depth, seg=10):
    bm = bmesh.new(); bmesh.ops.create_cone(bm, cap_ends=True, segments=seg, radius1=r1, radius2=r2, depth=depth); return bm

def p_box(sx, sy, sz):
    bm = bmesh.new(); bmesh.ops.create_cube(bm, size=1.0)
    bmesh.ops.scale(bm, vec=Vector((sx, sy, sz)), verts=bm.verts); return bm

def place(pos, rot=(0, 0, 0)):
    return Matrix.Translation(Vector(pos)) @ Matrix.Rotation(rot[2], 4, 'Z') @ Matrix.Rotation(rot[1], 4, 'Y') @ Matrix.Rotation(rot[0], 4, 'X')

# ---- shapes built directly into a Builder

def ring(b, cx, cy, z, rx, ry, n, start=0.0):
    return [b.v((cx + rx * math.sin(start + 2 * math.pi * i / n),
                 cy - ry * math.cos(start + 2 * math.pi * i / n), z)) for i in range(n)]

def lathe(b, profile, key, n=24, cx=0.0, cy=HEAD_CY, sx=1.0, sy=1.04, closed=False, keyfn=None, tilt=0.0):
    """Revolve a (radius, z) profile around the head. Radius 0 makes a pole. keyfn(seg, i) can colour
    individual panels. tilt rotates the whole thing around X (positive LOWERS the front)."""
    rings = []
    for r, z in profile:
        if r <= 1e-6:
            rings.append([b.v((cx, cy, z))])
        else:
            rings.append(ring(b, cx, cy, z, r * sx, r * sy, n))
    pairs = list(zip(rings, rings[1:])) + ([(rings[-1], rings[0])] if closed else [])
    for s, (a, c) in enumerate(pairs):
        if len(a) == 1 and len(c) == 1: continue
        for i in range(n):
            j = (i + 1) % n
            k = keyfn(s, i) if keyfn else key
            if len(a) == 1: b.face([a[0], c[i], c[j]], k)
            elif len(c) == 1: b.face([a[i], a[j], c[0]], k)
            else: b.face([a[i], a[j], c[j], c[i]], k)
    if tilt:
        rot = Matrix.Translation((0, cy, 1.3)) @ Matrix.Rotation(tilt, 4, 'X') @ Matrix.Translation((0, -cy, -1.3))
        for rg in rings:
            for v in rg: v.co = rot @ v.co
    return rings

def tube(b, points, radii, key, n=8, cap=True):
    """Sweep a circle along a polyline (horns, stems, tails)."""
    pts = [Vector(p) for p in points]
    rings = []
    for i, p in enumerate(pts):
        d = (pts[min(i + 1, len(pts) - 1)] - pts[max(i - 1, 0)]).normalized()
        side = d.cross(Vector((0, 0, 1)))
        if side.length < 1e-4: side = d.cross(Vector((1, 0, 0)))
        side.normalize(); up = side.cross(d)
        r = radii[i]
        if r <= 1e-6:
            rings.append([b.v(p)]); continue
        rings.append([b.v(p + (side * math.cos(2 * math.pi * k / n) + up * math.sin(2 * math.pi * k / n)) * r) for k in range(n)])
    for a, c in zip(rings, rings[1:]):
        for k in range(n):
            j = (k + 1) % n
            if len(a) == 1: b.face([a[0], c[k], c[j]], key)
            elif len(c) == 1: b.face([a[k], a[j], c[0]], key)
            else: b.face([a[k], a[j], c[j], c[k]], key)
    if cap:
        for rg in (rings[0], rings[-1]):
            if len(rg) > 2: b.face(rg if rg is rings[-1] else rg[::-1], key)

def dome_profile(r0, z0, top, t=0.004, steps=7):
    """Snug shell over the skull: outer surface up to the crown, inner surface back down."""
    outer = [(r0 * math.sqrt(max(0, 1 - ((z - z0) / (top - z0)) ** 2)), z)
             for z in [z0 + (top - z0) * (1 - math.cos(math.pi / 2 * s / steps)) for s in range(steps + 1)]]
    inner = [(max(0, r - t), z - t) for r, z in reversed(outer)]
    return outer + inner

def brim(b, r_in, r_out, z_in, z_out, key, t=0.006, n=28, sy=1.04, cy=HEAD_CY):
    lathe(b, [(r_in, z_in), (r_out, z_out), (r_out, z_out - t), (r_in, z_in - t)], key, n=n, sy=sy, cy=cy, closed=True)

# ------------------------------------------------------------------ hats

def crown():
    b = Builder("Crown", "hat", "Crown", {"c1": "#ffcd00", "c2": "#ff0000", "c3": "#006dff"})
    n = 16; rx, ry, t = 0.093, 0.098, 0.007; z0, z1, ztip = 1.300, 1.342, 1.405; cy = HEAD_CY
    lathe(b, [(rx, z0), (rx, z1), (rx - t, z1), (rx - t, z0)], "c1", n=n, sx=1, sy=ry / rx, closed=True)
    for k in range(8):
        a0, a1 = 2 * math.pi * 2 * k / n, 2 * math.pi * (2 * k + 2) / n; am = (a0 + a1) / 2
        at = lambda a, off, z: b.v(((rx - off) * math.sin(a), cy - (ry - off) * math.cos(a), z))
        o0, o1, i0, i1, tip = at(a0, 0, z1), at(a1, 0, z1), at(a0, t, z1), at(a1, t, z1), at(am, t / 2, ztip)
        for tri in ([o0, o1, tip], [i1, i0, tip], [o1, i1, tip], [i0, o0, tip]): b.face(tri, "c1")
        b.add_bm(p_ico(0.0075), "c1", place(((rx - t / 2) * math.sin(am), cy - (ry - t / 2) * math.cos(am), ztip + 0.004)))
    for k, (a, r) in enumerate([(0, .013), (math.pi / 2, .009), (math.pi, .009), (-math.pi / 2, .009),
                                (math.pi / 4, .007), (-math.pi / 4, .007), (3 * math.pi / 4, .007), (-3 * math.pi / 4, .007)]):
        b.add_bm(p_ico(r, scale=(1, .55, 1)), "c2" if k < 4 else "c3",
                 place((rx * math.sin(a), cy - ry * math.cos(a), (z0 + z1) / 2), (0, 0, -a)))
    return b.finish()

def captains_cap():
    b = Builder("CaptainsCap", "hat", "Captain's Cap", {"c1": "#ffffff", "c2": "#121a3a", "c3": "#ffcd00"})
    # crown: straight band that flares out to a wide flat top
    lathe(b, [(0.097, 1.290), (0.099, 1.335), (0.112, 1.372), (0.108, 1.384), (0, 1.388),
              (0, 1.380), (0.104, 1.376), (0.092, 1.334), (0.091, 1.290)], "c1", closed=True, keyfn=lambda s, i: "c1")
    lathe(b, [(0.099, 1.288), (0.100, 1.318), (0.0955, 1.318), (0.0955, 1.288)], "c2", closed=True)   # dark band
    # visor: a curved, slightly drooping half-disc on the front
    n = 12; rows = []
    for row, (r, z) in enumerate([(0.098, 1.292), (0.150, 1.270)]):
        rows.append([b.v((r * math.sin(a), HEAD_CY - r * 1.04 * math.cos(a), z))
                     for a in [math.radians(-70 + 140 * i / n) for i in range(n + 1)]])
    lower = [[b.v(v.co + Vector((0, 0, -0.005))) for v in rw] for rw in rows]
    for i in range(n):
        b.face([rows[0][i], rows[0][i + 1], rows[1][i + 1], rows[1][i]], "c2")
        b.face([lower[0][i + 1], lower[0][i], lower[1][i], lower[1][i + 1]], "c2")
        b.face([rows[1][i], rows[1][i + 1], lower[1][i + 1], lower[1][i]], "c2")
    for k in (0, n):
        b.face([rows[0][k], rows[1][k], lower[1][k], lower[0][k]][::(1 if k else -1)], "c2")
    # gold cord above the visor and a badge
    tube(b, [(0.101 * math.sin(a), HEAD_CY - 0.105 * math.cos(a), 1.322) for a in [math.radians(x) for x in range(-60, 61, 12)]],
         [0.0035] * 11, "c3", n=6)
    b.add_bm(p_cyl(0.016, 0.016, 0.006, seg=8), "c3", place((0, HEAD_CY - 0.103, 1.35), (math.pi / 2, 0, 0)))
    b.add_bm(p_ico(0.008, scale=(1, .5, 1)), "c2", place((0, HEAD_CY - 0.107, 1.35)))
    return b.finish()

def pirate_bandana():
    b = Builder("PirateBandana", "hat", "Pirate Bandana", {"c1": "#d01818", "c2": "#ffffff", "c3": "#d01818"})
    lathe(b, dome_profile(0.101, 1.288, 1.392, t=0.004), "c1", n=24, closed=True)
    # white polka dots on the dome
    import random; rnd = random.Random(4)
    for k in range(22):
        a = rnd.uniform(0, 2 * math.pi); h = rnd.uniform(0.05, 0.85)
        z = 1.288 + (1.392 - 1.288) * h
        r = 0.101 * math.sqrt(1 - h * h) + 0.001
        p = Vector((r * math.sin(a), HEAD_CY - r * 1.04 * math.cos(a), z))
        nrm = (p - Vector((0, HEAD_CY, 1.25))).normalized()
        dot = p_cyl(0.008, 0.008, 0.002, seg=8)
        b.add_bm(dot, "c2", Matrix.Translation(p) @ nrm.to_track_quat('Z', 'Y').to_matrix().to_4x4())
    # knot and two tails at the back
    back = Vector((0, HEAD_CY + 0.104, 1.285))
    b.add_bm(p_ico(0.017, scale=(1.3, 0.8, 1)), "c3", Matrix.Translation(back))
    for side in (-1, 1):
        tube(b, [back + Vector((side * .01, .004, 0)), back + Vector((side * .03, .02, -.04)), back + Vector((side * .04, .03, -.09))],
             [0.011, 0.012, 0.004], "c3", n=6)
    return b.finish()

def fish_hat():
    b = Builder("FishHat", "hat", "Fish Hat", {"c1": "#3b9be0", "c2": "#ff7400", "c3": "#f2f2f2"})
    # body: an elongated sphere lying front-to-back on the head, belly (c3) underneath
    body = p_ico(1.0, sub=2)
    for v in body.verts:
        x, y, z = v.co
        taper = 1 - 0.45 * max(0, y) ** 1.5          # thinner toward the tail (+Y)
        v.co = Vector((x * 0.042 * taper, y * 0.12, z * 0.05 * taper))
    belly_faces = []
    m = place((0, HEAD_CY, 1.395), (math.radians(-8), 0, 0))
    bk = b.mat("c1"); bb = b.mat("c3")
    remap = {ov: b.bm.verts.new(m @ ov.co) for ov in body.verts}
    for of in body.faces:
        f = b.bm.faces.new([remap[v] for v in of.verts])
        f.material_index = bb if of.calc_center_median().z < -0.012 else bk
    body.free()
    # tail fin
    t0 = m @ Vector((0, 0.115, 0));
    tri = [b.v(t0), b.v(t0 + Vector((0.0, 0.06, 0.045))), b.v(t0 + Vector((0.0, 0.06, -0.035)))]
    tri2 = [b.v(v.co + Vector((0.004, 0, 0))) for v in tri]
    for t in (tri, tri2[::-1]): b.face(t, "c2")
    for i in range(3): b.face([tri[i], tri[(i + 1) % 3], tri2[(i + 1) % 3], tri2[i]], "c2")
    # dorsal fin and two side fins
    d = [m @ Vector(p) for p in [(0, -0.03, 0.048), (0, 0.05, 0.05), (0, 0.035, 0.085)]]
    d2 = [p + Vector((0.004, 0, 0)) for p in d]
    dv, dv2 = [b.v(p) for p in d], [b.v(p) for p in d2]
    b.face(dv, "c2"); b.face(dv2[::-1], "c2")
    for i in range(3): b.face([dv[i], dv[(i + 1) % 3], dv2[(i + 1) % 3], dv2[i]], "c2")
    for side in (-1, 1):
        b.add_bm(p_ico(0.022, scale=(0.15, 1, 0.5)), "c2", m @ place((side * 0.04, -0.03, -0.02), (0, side * 0.6, side * 0.4)))
    # eyes and a gaping mouth
    for side in (-1, 1):
        b.add_bm(p_ico(0.012), "#ffffff", m @ place((side * 0.03, -0.085, 0.012)))
        b.add_bm(p_ico(0.006), "#000000", m @ place((side * 0.039, -0.09, 0.013)))
    b.add_bm(p_ico(0.014, scale=(1, 0.5, 0.8)), "#3a1010", m @ place((0, -0.118, -0.008)))
    return b.finish()

def angler_bucket_hat():
    b = Builder("AnglerBucketHat", "hat", "Angler's Bucket Hat", {"c1": "#7a8a4a", "c2": "#5a6a34", "c3": "#3a2a1a"})
    lathe(b, [(0.099, 1.292), (0.094, 1.375), (0.06, 1.392), (0, 1.396),
              (0, 1.388), (0.058, 1.384), (0.089, 1.370), (0.093, 1.292)], "c1", closed=True)
    brim(b, 0.096, 0.150, 1.296, 1.262, "c2", t=0.006)
    lathe(b, [(0.0995, 1.296), (0.098, 1.318), (0.094, 1.318), (0.094, 1.296)], "c3", closed=True)   # band
    # tackle hooked on the band: a red/white bobber and a little fish lure
    a = math.radians(55); p = Vector((0.104 * math.sin(a), HEAD_CY - 0.108 * math.cos(a), 1.307))
    b.add_bm(p_ico(0.012, scale=(1, 1, 1)), "#ff2020", place(p + Vector((0.004, -0.003, 0.004))))
    b.add_bm(p_cyl(0.0122, 0.0122, 0.009, seg=10), "#ffffff", place(p + Vector((0.004, -0.003, -0.001))))
    b.add_bm(p_cyl(0.002, 0.002, 0.012, seg=6), "#ffffff", place(p + Vector((0.004, -0.003, 0.019))))
    a = math.radians(-40); q = Vector((0.104 * math.sin(a), HEAD_CY - 0.108 * math.cos(a), 1.308))
    b.add_bm(p_ico(0.011, scale=(0.5, 1.5, 0.7)), "#ffcd00", place(q, (0, 0, a)))
    tube(b, [q + Vector((0, 0.012, -0.004)), q + Vector((0, 0.02, -0.014)), q + Vector((0, 0.012, -0.02))], [0.0015] * 3, "#cccccc", n=4)
    return b.finish()

def top_hat():
    b = Builder("TopHat", "hat", "Top Hat", {"c1": "#1a1a1a", "c2": "#a01010", "c3": "#1a1a1a"})
    lathe(b, [(0.092, 1.296), (0.094, 1.47), (0.099, 1.53), (0, 1.53), (0, 1.522), (0.093, 1.522), (0.088, 1.47), (0.088, 1.296)],
          "c1", closed=True)
    lathe(b, [(0.140, 1.302), (0.143, 1.312), (0.135, 1.306), (0.096, 1.300), (0.096, 1.294), (0.135, 1.296)], "c1", closed=True, sy=1.0)
    lathe(b, [(0.0945, 1.300), (0.0955, 1.338), (0.091, 1.338), (0.091, 1.300)], "c2", closed=True)
    return b.finish()

def viking_helmet():
    b = Builder("VikingHelmet", "hat", "Viking Helmet", {"c1": "#9aa0a8", "c2": "#6b4a2a", "c3": "#efe6cc"})
    lathe(b, dome_profile(0.106, 1.290, 1.405, t=0.005), "c1", n=24, closed=True)
    lathe(b, [(0.109, 1.286), (0.109, 1.308), (0.101, 1.308), (0.101, 1.286)], "c2", closed=True)   # rim
    for k in range(10):   # rivets
        a = 2 * math.pi * k / 10
        b.add_bm(p_ico(0.004), "c1", place((0.110 * math.sin(a), HEAD_CY - 0.114 * math.cos(a), 1.297)))
    # nose guard
    b.add_bm(p_box(0.016, 0.006, 0.04), "c2", place((0, HEAD_CY - 0.115, 1.28)))
    # curved horns sweeping out and up
    for side in (-1, 1):
        pts, rad = [], []
        for i in range(9):
            t = i / 8
            pts.append((side * (0.085 + 0.07 * t), HEAD_CY - 0.01 - 0.02 * t, 1.33 + 0.02 * t + 0.09 * t * t))
            rad.append(0.022 * (1 - t) + 0.001)
        tube(b, pts, rad, "c3", n=8)
    return b.finish()

def propeller_beanie():
    b = Builder("PropellerBeanie", "hat", "Propeller Beanie", {"c1": "#ff2020", "c2": "#ffcd00", "c3": "#006dff"})
    lathe(b, dome_profile(0.100, 1.290, 1.396, t=0.004), "c1", n=24, closed=True,
          keyfn=lambda s, i: ("c1", "c2", "c3")[(i // 4) % 3] if s < 7 else "c1")
    lathe(b, [(0.102, 1.286), (0.102, 1.302), (0.096, 1.302), (0.096, 1.286)], "c1", closed=True)   # rolled edge
    b.add_bm(p_cyl(0.004, 0.004, 0.03, seg=6), "#cccccc", place((0, HEAD_CY, 1.405)))
    b.add_bm(p_ico(0.008), "c2", place((0, HEAD_CY, 1.42)))
    for side in (-1, 1):   # two blades, slightly pitched
        b.add_bm(p_box(0.09, 0.022, 0.003), "c3", place((side * 0.05, HEAD_CY, 1.418), (side * 0.25, 0, 0.3)))
    return b.finish()

def wizard_hat():
    b = Builder("WizardHat", "hat", "Wizard Hat", {"c1": "#3a2a9a", "c2": "#ffcd00", "c3": "#ffcd00"})
    brim(b, 0.096, 0.155, 1.300, 1.290, "c1", t=0.006)
    # cone bent backward toward the tip
    n, rings = 16, []
    for i in range(10):
        t = i / 9
        z = 1.296 + 0.30 * t
        y = HEAD_CY + 0.09 * t ** 2.2
        r = 0.096 * (1 - t) ** 1.1 + (0.0 if i == 9 else 0.002)
        rings.append([b.v((r * math.sin(2 * math.pi * k / n), y - r * 1.04 * math.cos(2 * math.pi * k / n), z)) for k in range(n)]
                     if i < 9 else [b.v((0, y, z))])
    for a, c in zip(rings, rings[1:]):
        for k in range(n):
            j = (k + 1) % n
            if len(c) == 1: b.face([a[k], a[j], c[0]], "c1")
            else: b.face([a[k], a[j], c[j], c[k]], "c1")
    b.face(rings[0][::-1], "c1")
    lathe(b, [(0.0975, 1.300), (0.093, 1.33), (0.089, 1.33), (0.0935, 1.300)], "c2", n=16, closed=True)   # band
    for (a, zz, s) in [(0.4, 1.40, 0.012), (-1.2, 1.45, 0.009), (2.6, 1.37, 0.010), (-2.4, 1.50, 0.008)]:   # stars
        t = (zz - 1.296) / 0.30; r = 0.096 * (1 - t) ** 1.1
        b.add_bm(p_ico(s, scale=(1, 0.3, 1)), "c3",
                 place((r * math.sin(a), HEAD_CY + 0.09 * t ** 2.2 - r * 1.04 * math.cos(a), zz), (0, 0, -a)))
    return b.finish()

# ------------------------------------------------------------------ accessories (face, also on the head bone)

def eyepatch():
    b = Builder("Eyepatch", "accessory", "Eyepatch", {"c1": "#151515", "c2": "#151515", "c3": "#151515"})
    patch = p_ico(0.027, sub=2, scale=(1, 0.30, 0.85))
    b.add_bm(patch, "c1", place((EYE_X, EYE_Y - 0.008, EYE_Z)))
    # strap: runs from the patch diagonally up over the forehead and round the back, hugging the head.
    # A circle tilted about the front-back axis: low on the patch side, high across the other brow.
    z0, slope = 1.312, 0.62
    pts = []
    for k in range(41):
        a = 2 * math.pi * k / 40
        x, y = 0.1 * math.sin(a), HEAD_CY - 0.1 * math.cos(a)
        pts.append(on_head(Vector((x, y, z0 - x * slope + 0.03 * math.cos(a) ** 2 * (math.cos(a) > 0))), 0.0035))
    tube(b, pts, [0.0028] * len(pts), "c2", n=4, cap=False)
    return b.finish()

def pipe():
    b = Builder("Pipe", "accessory", "Pipe", {"c1": "#6b3e1e", "c2": "#2a1a10", "c3": "#ff7a1a"})
    start = MOUTH + Vector((0.012, 0.004, 0.0))
    bend = start + Vector((0.008, -0.04, -0.016))
    bowl = bend + Vector((0.004, -0.02, -0.008))
    tube(b, [start, bend, bowl], [0.0035, 0.004, 0.0045], "c2", n=6)
    b.add_bm(p_cyl(0.011, 0.013, 0.03, seg=10), "c1", place(bowl + Vector((0, 0, 0.012))))
    b.add_bm(p_cyl(0.009, 0.009, 0.002, seg=10), "c3", place(bowl + Vector((0, 0, 0.027))))   # glowing tobacco
    return b.finish()

def monocle():
    b = Builder("Monocle", "accessory", "Monocle", {"c1": "#ffcd00", "c2": "#cfeaff", "c3": "#ffcd00"})
    c = Vector((-EYE_X, EYE_Y - 0.010, EYE_Z))
    pts = [c + Vector((0.020 * math.cos(2 * math.pi * k / 16), 0, 0.020 * math.sin(2 * math.pi * k / 16))) for k in range(17)]
    tube(b, pts, [0.0025] * 17, "c1", n=6, cap=False)
    b.add_bm(p_cyl(0.019, 0.019, 0.0015, seg=16), "c2", place(c, (math.pi / 2, 0, 0)))
    chain = [c + Vector((-0.02, 0, -0.005)), c + Vector((-0.035, 0.005, -0.04)), c + Vector((-0.045, 0.02, -0.09))]
    tube(b, chain, [0.0012] * 3, "c3", n=4)
    return b.finish()

NOSE = Vector((0.0, -0.133, 1.248))

def on_head(p, offset):
    """Project a point onto the real head surface (along the ray from the middle of the head) plus a gap,
    like a shrinkwrap. HEAD_TREE is built once the head reference is loaded."""
    c = Vector((0, HEAD_CY, 1.25))
    d = (Vector(p) - c).normalized()
    # cast inward from outside so the first hit is the outer skin, not eye sockets or the mouth
    hit = HEAD_TREE.ray_cast(c + d * 0.5, -d, 0.5)[0]
    return hit + d * offset if hit is not None else Vector(p)

def oval(c, rx, rz, n=16, y_bulge=0.0):
    """Points around an oval in the XZ plane (facing -Y), optionally curving back at the sides."""
    return [c + Vector((rx * math.cos(2 * math.pi * k / n), y_bulge * math.cos(2 * math.pi * k / n) ** 2,
                        rz * math.sin(2 * math.pi * k / n))) for k in range(n + 1)]

def aviators():
    b = Builder("Aviators", "accessory", "Aviator Sunglasses", {"c1": "#ffcd00", "c2": "#1a1a2a", "c3": "#ffcd00"})
    for side in (-1, 1):
        c = Vector((side * EYE_X, EYE_Y - 0.012, EYE_Z))
        lens = p_ico(0.026, sub=2, scale=(0.95, 0.12, 0.8))
        for v in lens.verts:                       # teardrop: wider at the bottom outside corner
            if v.co.z < 0: v.co.x += side * 0.004 * (-v.co.z / 0.02)
        b.add_bm(lens, "c2", place(c))
        tube(b, oval(c, 0.026, 0.021), [0.0018] * 17, "c1", n=5, cap=False)
        tube(b, [c + Vector((side * 0.026, 0.0, 0.006)), Vector((side * 0.094, -0.08, EYE_Z + 0.006)), Vector((side * 0.095, 0.0, EYE_Z - 0.005))],
             [0.0016] * 3, "c1", n=4)               # arms back to the ears
    tube(b, [Vector((-0.02, EYE_Y - 0.012, EYE_Z + 0.012)), Vector((0, EYE_Y - 0.014, EYE_Z + 0.014)), Vector((0.02, EYE_Y - 0.012, EYE_Z + 0.012))],
         [0.0017] * 3, "c3", n=4)                   # bridge
    return b.finish()

def snorkel_mask():
    b = Builder("SnorkelMask", "accessory", "Snorkel Mask", {"c1": "#ff7400", "c2": "#bfe9ff", "c3": "#ffcd00"})
    c = Vector((0, -0.142, 1.276))
    b.add_bm(p_ico(1.0, sub=2, scale=(0.074, 0.012, 0.042)), "c2", place(c))            # glass
    tube(b, oval(c, 0.076, 0.044, n=20, y_bulge=0.03), [0.007] * 21, "c1", n=6, cap=False)   # rubber frame
    # strap: from one side of the frame round the back of the head to the other, hugging the head
    pts = [on_head(Vector((0.1 * math.sin(a), HEAD_CY - 0.1 * math.cos(a), 1.288)), 0.004)
           for a in [math.radians(58 + 244 * k / 30) for k in range(31)]]
    pts = [c + Vector((0.074, 0.03, 0.0))] + pts + [c + Vector((-0.074, 0.03, 0.0))]
    tube(b, pts, [0.005] * len(pts), "c1", n=4, cap=True)
    s = [Vector(p) for p in [(0.05, -0.122, 1.212), (0.088, -0.085, 1.235), (0.1, -0.05, 1.30), (0.1, -0.04, 1.43)]]
    tube(b, s, [0.007, 0.007, 0.007, 0.008], "c3", n=8)                                    # snorkel
    b.add_bm(p_box(0.03, 0.014, 0.012), "c3", place((0.03, -0.122, 1.212)))               # mouthpiece
    return b.finish()

def clown_nose():
    b = Builder("ClownNose", "accessory", "Clown Nose", {"c1": "#ff1010", "c2": "#ff1010", "c3": "#ff1010"})
    b.add_bm(p_ico(0.021, sub=2), "c1", place(NOSE + Vector((0, -0.008, 0.0))))
    return b.finish()

def headphones():
    b = Builder("Headphones", "accessory", "Headphones", {"c1": "#2a2a2a", "c2": "#ff2060", "c3": "#2a2a2a"})
    arc = []
    for k in range(13):
        a = math.pi * k / 12                        # from one ear over the top to the other
        arc.append((0.104 * math.cos(a), HEAD_CY + 0.005, 1.235 + 0.165 * math.sin(a)))
    tube(b, arc, [0.007] * 13, "c1", n=6)
    for side in (-1, 1):
        b.add_bm(p_cyl(0.030, 0.030, 0.026, seg=12), "c2", place((side * 0.102, HEAD_CY + 0.005, 1.235), (0, math.pi / 2, 0)))
        b.add_bm(p_cyl(0.026, 0.026, 0.012, seg=12), "c3", place((side * 0.088, HEAD_CY + 0.005, 1.235), (0, math.pi / 2, 0)))
    return b.finish()

def fish_in_mouth():
    b = Builder("FishInMouth", "accessory", "Fish In Mouth", {"c1": "#9fb8c8", "c2": "#5f7f94", "c3": "#e8eef2"})
    body = p_ico(1.0, sub=2)
    for v in body.verts:
        x, y, z = v.co
        v.co = Vector((x * 0.055, y * 0.012 * (1 - 0.5 * max(0, x)), z * 0.02 * (1 - 0.5 * max(0, x))))
    c = MOUTH + Vector((0.008, -0.012, -0.002))
    b.add_bm(body, "c1", place(c, (0, 0.12, 0)))
    tail = c + Vector((0.055, 0, 0.006))
    t = [b.v(tail), b.v(tail + Vector((0.03, 0, 0.022))), b.v(tail + Vector((0.03, 0, -0.016)))]
    t2 = [b.v(v.co + Vector((0, 0.004, 0))) for v in t]
    b.face(t, "c2"); b.face(t2[::-1], "c2")
    for i in range(3): b.face([t[i], t[(i + 1) % 3], t2[(i + 1) % 3], t2[i]], "c2")
    b.add_bm(p_ico(0.005), "#000000", place(c + Vector((-0.04, -0.01, 0.006))))
    return b.finish()

def head_bandage():
    b = Builder("HeadBandage", "accessory", "Head Bandage", {"c1": "#f4f0e6", "c2": "#c01818", "c3": "#f4f0e6"})
    lathe(b, [(0.0935, 1.300), (0.0925, 1.320), (0.0885, 1.320), (0.0895, 1.300)], "c1", n=24, closed=True, sy=1.0, tilt=-0.16)
    a = math.radians(-35)
    b.add_bm(p_ico(0.011, scale=(1, 0.3, 0.8)), "c2", place((0.094 * math.sin(a), HEAD_CY - 0.094 * math.cos(a), 1.322), (0, 0, -a)))
    for side in (-1, 1):   # little knot ends at the back
        tube(b, [(0, HEAD_CY + 0.093, 1.296), (side * 0.012, HEAD_CY + 0.106, 1.279), (side * 0.02, HEAD_CY + 0.111, 1.259)],
             [0.006, 0.006, 0.002], "c3", n=5)
    return b.finish()

# ------------------------------------------------------------------ outfits (whole body, skinned to the skeleton)
#
# The game's outfit IS the character's body: torso, sleeves, legs and shoes (head and hands are separate).
# The shape is measured from a game outfit in its rest (T) pose by casting rays out from each limb's
# centre line, so ours matches the character's proportions; only the measurements are used.
# Skeleton in rest pose (Blender coords, so the game's "_L" side is +X here):
BONE_NAMES = ["Body", "Head", "Eye_L", "Eye_R", "MouthUpper", "MouthLower",
              "ArmUpper_L", "ArmLower_L", "Hand_L", "ArmUpper_R", "ArmLower_R", "Hand_R",
              "LegUpper_L", "LegLower_L", "Foot_L", "LegUpper_R", "LegLower_R", "Foot_R"]
SIDE_BONES = {1: dict(au=6, al=7, hand=8, lu=12, ll=13, foot=14),     # +X (game's left)
              -1: dict(au=9, al=10, hand=11, lu=15, ll=16, foot=17)}
ARM_Z, SHOULDER_X, ELBOW_X, WRIST_X = 1.0, 0.10, 0.353, 0.58
HIP_Z, LEG_X, KNEE_Z = 0.49, 0.06, 0.26
TORSO_CY = -0.012

def _blend(a, b, t, t0, w):
    """Weights moving from bone a (t < t0) to bone b (t > t0) across t0 +/- w."""
    f = min(1.0, max(0.0, (t - (t0 - w)) / (2 * w)))
    return [(bone, wt) for bone, wt in ((a, 1 - f), (b, f)) if wt > 1e-3]

def outfit_weights(co, part):
    x, y, z = co
    sb = SIDE_BONES[1 if x >= 0 else -1]
    if part == "arm":
        s = abs(x)
        return _blend(0, sb["au"], s, 0.145, 0.03) if s < 0.25 else _blend(sb["au"], sb["al"], s, ELBOW_X, 0.035)
    if part == "leg":
        if z > 0.36: return _blend(sb["lu"], 0, z, 0.45, 0.035)
        if z > 0.14: return _blend(sb["ll"], sb["lu"], z, KNEE_Z, 0.035)
        return _blend(sb["foot"], sb["ll"], z, 0.10, 0.02)
    if part == "shoe":
        return [(sb["foot"], 1.0)]
    if part == "robe":
        # long split skirts: each half follows its own thigh more and more toward the hem
        w = min(0.8, max(0.0, (0.56 - z) / 0.3)) * min(1.0, abs(x) / 0.04)
        return [(0, 1 - w), (sb["lu"], w)] if w > 1e-3 else [(0, 1.0)]
    if part == "skirt":
        # coat tails follow the hips mostly, with a share of the thigh so legs don't burst through
        w = 0.4 * min(1.0, abs(x) / 0.05) * min(1.0, max(0.0, (0.62 - z) / 0.3))
        return [(0, 1 - w), (sb["lu"], w)] if w > 1e-3 else [(0, 1.0)]
    return [(0, 1.0)]

def ray_out(origin, d, rmin, rmax):
    """Distance from origin to the reference body's surface along d (first hit past rmin)."""
    o = Vector(origin); d = Vector(d).normalized()
    hit = BODY_TREE.ray_cast(o + d * rmin, d, rmax - rmin)[0]
    return (hit - o).length if hit is not None else rmax

def smooth_grid(R, passes=1):
    for _ in range(passes):
        n, m = len(R), len(R[0])
        R = [[0.25 * R[i][(j - 1) % m] + 0.5 * R[i][j] + 0.25 * R[i][(j + 1) % m] for j in range(m)] for i in range(n)]
        R = [[(0.25 * R[max(i - 1, 0)][j] + 0.5 * R[i][j] + 0.25 * R[min(i + 1, n - 1)][j]) for j in range(m)] for i in range(n)]
    return R

N_TORSO, N_LIMB = 16, 10

def torso_frame(z, a, r):
    return Vector((r * math.sin(a), TORSO_CY - r * math.cos(a), z))

def arm_frame(side, s, a, r):            # a = 0 faces front (-Y), a = 90deg faces up
    return Vector((side * s, -r * math.cos(a), ARM_Z + r * math.sin(a)))

def leg_frame(side, z, a, r):
    return Vector((side * LEG_X + r * math.sin(a), -r * math.cos(a), z))

def measure_torso(zs):
    return smooth_grid([[ray_out((0, TORSO_CY, z), (math.sin(a), -math.cos(a), 0), 0.02, 0.135)
                         for a in [2 * math.pi * j / N_TORSO for j in range(N_TORSO)]] for z in zs])

def measure_arm(ss):
    return smooth_grid([[ray_out((s, 0, ARM_Z), (0, -math.cos(a), math.sin(a)), 0.005, 0.07)
                         for a in [2 * math.pi * j / N_LIMB for j in range(N_LIMB)]] for s in ss])

def measure_leg(zs):
    return smooth_grid([[ray_out((LEG_X, 0, z), (math.sin(a), -math.cos(a), 0), 0.005, 0.075)
                         for a in [2 * math.pi * j / N_LIMB for j in range(N_LIMB)]] for z in zs])

def sweep(b, frame, params, R, keyfn, extra=lambda t, a: 0.0, cap_start=True, cap_end=True):
    """Rings along a limb. frame(t, a, r) -> point; R[i][j] measured radius; keyfn(t, a) -> colour key."""
    m = len(R[0])
    angles = [2 * math.pi * j / m for j in range(m)]
    rings = [[b.v(frame(t, a, R[i][j] + extra(t, a))) for j, a in enumerate(angles)] for i, t in enumerate(params)]
    for i in range(len(rings) - 1):
        for j in range(m):
            k = (j + 1) % m
            tm, am = (params[i] + params[i + 1]) / 2, (angles[j] + angles[j] + 2 * math.pi / m) / 2
            b.face([rings[i][j], rings[i][k], rings[i + 1][k], rings[i + 1][j]], keyfn(tm, am))
    for idx, flip in ((0, True), (-1, False)):
        if (idx == 0 and cap_start) or (idx == -1 and cap_end):
            c = sum((v.co for v in rings[idx]), Vector()) / m
            cv = b.v(c)
            for j in range(m):
                tri = [rings[idx][j], rings[idx][(j + 1) % m], cv]
                b.face(tri[::-1] if flip else tri, keyfn(params[idx], angles[j]))
    return rings

def shoe(b, side, key, size=1.0, height=1.0):
    b.part = "shoe"
    pts, rx, rz = [], [], []
    for i in range(7):
        t = i / 6
        pts.append(Vector((side * LEG_X, 0.05 - 0.235 * t, 0.045 - 0.012 * t)))
        rx.append((0.05 - 0.008 * t) * size); rz.append((0.047 - 0.015 * t * t) * height)
    rings = []
    for p, w, h in zip(pts, rx, rz):
        ring_ = []
        for j in range(10):
            a = 2 * math.pi * j / 10
            zz = max(p.z + h * math.sin(a), 0.0)             # flat sole on the ground
            ring_.append(b.v((p.x + w * math.cos(a), p.y, zz)))
        rings.append(ring_)
    for i in range(len(rings) - 1):
        for j in range(10):
            k = (j + 1) % 10
            b.face([rings[i][j], rings[i][k], rings[i + 1][k], rings[i + 1][j]], key)
    for rg, flip in ((rings[0], False), (rings[-1], True)):
        b.face(rg[::-1] if flip else rg, key)

def body_shell(b, torso_key, arm_key, leg_key, off=0.006, arm_extra=lambda t, a: 0.0, leg_extra=lambda t, a: 0.0,
               torso_extra=lambda t, a: 0.0, sleeve_end=0.55):
    """Torso + both sleeves + both trouser legs, built around the measured body with `off` of slack."""
    zs = [0.42 + (1.10 - 0.42) * i / 17 for i in range(18)]
    RT = [[r + off for r in row] for row in measure_torso(zs)]
    b.part = "torso"
    sweep(b, lambda z, a, r: torso_frame(z, a, r), zs, RT, torso_key, extra=torso_extra)
    ss = [0.12 + (sleeve_end - 0.12) * i / 10 for i in range(11)]
    RA = [[r + off for r in row] for row in measure_arm(ss)]
    for side in (1, -1):
        b.part = "arm"
        sweep(b, lambda s, a, r, sd=side: arm_frame(sd, s, a, r), ss, RA,
              lambda s, a, sd=side: arm_key(s, a, sd), extra=arm_extra)
    lz = [0.46 - (0.46 - 0.085) * i / 10 for i in range(11)]
    RL = [[r + off for r in row] for row in measure_leg(lz)]
    for side in (1, -1):
        b.part = "leg"
        # mirror the angle for the right leg so inner/outer sides match
        sweep(b, lambda z, a, r, sd=side: leg_frame(sd, z, sd * a if sd > 0 else -a, r), lz, RL,
              lambda z, a, sd=side: leg_key(z, a, sd), extra=leg_extra)
    b.part = None
    return RT, zs

def torso_point(RT, zs, z, a, out=0.0):
    """Point on the (already offset) torso surface at height z, angle a."""
    i = min(range(len(zs)), key=lambda k: abs(zs[k] - z))
    j = int(round(a / (2 * math.pi) * N_TORSO)) % N_TORSO
    return torso_frame(z, a, RT[i][j] + out)

def pirate_coat():
    b = Builder("PirateCoat", "outfit", "Pirate Coat", {"c1": "#7a1010", "c2": "#ffcd00", "c3": "#f2ece0"})
    front = lambda a: min(abs(a), abs(2 * math.pi - a))     # angular distance from the front
    def torso_key(z, a):
        if 0.595 < z < 0.65: return "#2a1a10"               # leather belt
        if z > 0.80 and front(a) < math.radians(24): return "c3"   # shirt showing at the open front
        return "c1"
    def arm_key(s, a, side):
        return "c2" if s > 0.47 else "c1"                  # big gold cuffs
    def leg_key(z, a, side):
        return "#141414" if z < 0.21 else "#3a2a22"        # tall boots over dark trousers
    RT, zs = body_shell(b, torso_key, arm_key, leg_key, off=0.008,
                        arm_extra=lambda s, a: 0.012 if s > 0.47 else 0.0,
                        leg_extra=lambda z, a: 0.01 if z < 0.21 else 0.0)
    # coat tails: a flared skirt from the waist to the knees, open at the front
    b.part = "skirt"
    n = 16; js = [j for j in range(n + 1)]
    a0, a1 = math.radians(38), math.radians(322)
    rows = []
    for zi, (z, flare) in enumerate([(0.62, 0.006), (0.48, 0.03), (0.34, 0.055), (0.24, 0.07)]):
        row = []
        for j in js:
            a = a0 + (a1 - a0) * j / n
            row.append(b.v(torso_point(RT, zs, max(z, 0.42), a, flare) + Vector((0, 0, z - max(z, 0.42)))))
        rows.append(row)
    inner = [[b.v(v.co + (Vector((v.co.x, v.co.y - TORSO_CY, 0)).normalized() * -0.005)) for v in row] for row in rows]
    for i in range(len(rows) - 1):
        key = "c2" if i == len(rows) - 2 else "c1"
        for j in range(n):
            b.face([rows[i][j], rows[i][j + 1], rows[i + 1][j + 1], rows[i + 1][j]], "c1" if i < len(rows) - 2 else "c1")
            b.face([inner[i][j + 1], inner[i][j], inner[i + 1][j], inner[i + 1][j + 1]], "c1")
    for j in range(n):   # gold hem along the bottom edge
        b.face([rows[-1][j], rows[-1][j + 1], inner[-1][j + 1], inner[-1][j]], "c2")
    for i in range(len(rows) - 1):
        for j in (0, n):
            q = [rows[i][j], rows[i + 1][j], inner[i + 1][j], inner[i][j]]
            b.face(q if j else q[::-1], "c1")
    b.part = "torso"
    # gold buttons down both front edges, a collar, and a belt buckle
    for z in [0.70, 0.76, 0.82, 0.88, 0.94]:
        for a in (math.radians(27), math.radians(333)):
            b.add_bm(p_ico(0.008), "c2", Matrix.Translation(torso_point(RT, zs, z, a, 0.003)))
    b.add_bm(p_box(0.04, 0.012, 0.035), "c2", Matrix.Translation(torso_point(RT, zs, 0.62, 0.0, 0.004)))
    lathe(b, [(0.075, 1.06), (0.095, 1.115), (0.088, 1.115), (0.068, 1.06)], "c1", n=16, cy=TORSO_CY, sy=0.95, closed=True)
    for side in (1, -1):
        shoe(b, side, "#141414", size=1.08)
    return b.finish()

def captains_uniform():
    b = Builder("CaptainsUniform", "outfit", "Captain's Uniform", {"c1": "#f4f4f4", "c2": "#ffcd00", "c3": "#121a3a"})
    def torso_key(z, a):
        return "c3" if z < 0.50 else "c1"                  # jacket ends at the hips, trousers below
    def arm_key(s, a, side):
        return "c2" if (0.475 < s < 0.49 or 0.505 < s < 0.52) else "c1"   # two gold stripes at the cuff
    def leg_key(z, a, side):
        outer = abs(math.sin(a)) > 0.92                     # gold stripe down the outside seam
        return "c2" if outer and z > 0.12 else "c3"
    RT, zs = body_shell(b, torso_key, arm_key, leg_key, off=0.007)
    b.part = "torso"
    # double row of gold buttons
    for z in [0.62, 0.70, 0.78, 0.86, 0.94]:
        for a in (math.radians(16), math.radians(344)):
            b.add_bm(p_ico(0.0075), "c2", Matrix.Translation(torso_point(RT, zs, z, a, 0.003)))
    # high collar with a gold edge
    lathe(b, [(0.072, 1.07), (0.078, 1.12), (0.072, 1.12), (0.066, 1.07)], "c1", n=16, cy=TORSO_CY, sy=0.95, closed=True)
    lathe(b, [(0.0785, 1.112), (0.0795, 1.124), (0.072, 1.124), (0.0715, 1.112)], "c2", n=16, cy=TORSO_CY, sy=0.95, closed=True)
    # epaulettes with fringe on each shoulder
    for side in (1, -1):
        b.part = "arm"
        b.add_bm(p_box(0.07, 0.07, 0.012), "c2", Matrix.Translation((side * 0.135, 0, ARM_Z + 0.052)))
        for k in range(5):
            b.add_bm(p_cyl(0.003, 0.003, 0.025, seg=5), "c2",
                     Matrix.Translation((side * 0.172, -0.03 + 0.015 * k, ARM_Z + 0.035)))
        b.part = None
        shoe(b, side, "#141414", size=1.04)
    return b.finish()

# ---- shared pieces for the next outfits

def skirt(b, RT, zs, rows, key_fn, segments, part="robe", thick=0.005):
    """A skirt/coat-tail shell hanging from the torso. rows = [(z, flare)...] top to bottom;
    segments = [(a_start, a_end)...] in radians (gaps between them are open slits);
    key_fn(row, a, hem) -> colour key."""
    b.part = part
    for a0, a1 in segments:
        n = max(2, int(round((a1 - a0) / (2 * math.pi) * 22)))
        rows_o, rows_i = [], []
        for z, flare in rows:
            ro, ri = [], []
            for j in range(n + 1):
                a = a0 + (a1 - a0) * j / n
                p = torso_point(RT, zs, max(z, 0.42), a, flare) + Vector((0, 0, z - max(z, 0.42)))
                out = Vector((p.x, p.y - TORSO_CY, 0)).normalized()
                ro.append(b.v(p)); ri.append(b.v(p - out * thick))
            rows_o.append(ro); rows_i.append(ri)
        last = len(rows) - 2
        for i in range(len(rows) - 1):
            for j in range(n):
                am = a0 + (a1 - a0) * (j + 0.5) / n
                k = key_fn(i, am, i == last)
                b.face([rows_o[i][j], rows_o[i][j + 1], rows_o[i + 1][j + 1], rows_o[i + 1][j]], k)
                b.face([rows_i[i][j + 1], rows_i[i][j], rows_i[i + 1][j], rows_i[i + 1][j + 1]], k)
        for j in range(n):
            am = a0 + (a1 - a0) * (j + 0.5) / n
            b.face([rows_o[-1][j], rows_o[-1][j + 1], rows_i[-1][j + 1], rows_i[-1][j]], key_fn(last, am, True))
        for i in range(len(rows) - 1):
            for j in (0, n):
                q = [rows_o[i][j], rows_o[i + 1][j], rows_i[i + 1][j], rows_i[i][j]]
                b.face(q if j else q[::-1], key_fn(i, a0 if j == 0 else a1, i == last))
    b.part = None

def front_dist(a):
    return min(abs(a), abs(2 * math.pi - a))

def surface_strap(b, RT, zs, pts_za, r, key, part="torso"):
    """A strap lying on the torso, given as (z, angle) points."""
    b.part = part
    tube(b, [torso_point(RT, zs, z, a % (2 * math.pi), 0.003) for z, a in pts_za], [r] * len(pts_za), key, n=4)
    b.part = None

SPLIT = [(0.03, math.pi - 0.03), (math.pi + 0.03, 2 * math.pi - 0.03)]   # slit front and back

def wizard_robe():
    b = Builder("WizardRobe", "outfit", "Wizard Robe", {"c1": "#3a2a9a", "c2": "#ffcd00", "c3": "#ffcd00"})
    def torso_key(z, a):
        if 0.60 < z < 0.635: return "c2"                                   # rope belt
        if front_dist(a) < math.radians(9) and z < 0.95: return "c2"      # trim down the front
        return "c1"
    RT, zs = body_shell(b, torso_key, lambda s, a, sd: "c2" if s > 0.50 else "c1", lambda z, a, sd: "#2a2030",
                        off=0.009, arm_extra=lambda s, a: 0.03 * max(0.0, (s - 0.30) / 0.25))   # bell sleeves
    skirt(b, RT, zs, [(0.62, 0.008), (0.45, 0.025), (0.28, 0.045), (0.12, 0.06), (0.03, 0.07)],
          lambda i, a, hem: "c2" if hem else "c1", SPLIT)
    b.part = "torso"
    for z, a, r in [(0.92, 0.6, 0.012), (0.80, -0.9, 0.010), (0.72, 2.4, 0.011), (0.98, -2.2, 0.009), (0.85, 3.0, 0.010)]:
        b.add_bm(p_ico(r, scale=(1, 0.35, 1)), "c3", Matrix.Translation(torso_point(RT, zs, z, a % (2 * math.pi), 0.002)) @ Matrix.Rotation(-a, 4, 'Z'))
    lathe(b, [(0.08, 1.05), (0.098, 1.11), (0.09, 1.11), (0.072, 1.05)], "c1", n=16, cy=TORSO_CY, sy=0.95, closed=True)
    for side in (1, -1):
        shoe(b, side, "#2a2030", size=1.0, height=0.9)
    return b.finish()

def tuxedo():
    b = Builder("Tuxedo", "outfit", "Tuxedo", {"c1": "#151515", "c2": "#f4f4f4", "c3": "#b01010"})
    def torso_key(z, a):
        fd = front_dist(a)
        if 0.56 < z < 0.63 and fd < math.radians(70): return "c3"         # cummerbund
        if z > 0.63 and fd < math.radians(10 + (z - 0.63) * 60): return "c2"   # shirt front widening into the V
        return "c1"
    RT, zs = body_shell(b, torso_key, lambda s, a, sd: "c2" if s > 0.535 else "c1", lambda z, a, sd: "c1", off=0.007)
    # tails at the back
    skirt(b, RT, zs, [(0.58, 0.006), (0.45, 0.016), (0.33, 0.024)], lambda i, a, hem: "c1",
          [(math.radians(125), math.radians(178)), (math.radians(182), math.radians(235))], part="skirt")
    b.part = "torso"
    for z in (0.70, 0.78, 0.86):                                            # shirt studs
        b.add_bm(p_ico(0.005), "#151515", Matrix.Translation(torso_point(RT, zs, z, 0.0, 0.002)))
    knot = torso_point(RT, zs, 1.06, 0.0, 0.012)
    b.add_bm(p_ico(0.009), "c3", Matrix.Translation(knot))
    for side in (1, -1):                                                    # bow tie wings
        b.add_bm(p_cyl(0.004, 0.018, 0.035, seg=6), "c3", Matrix.Translation(knot + Vector((side * 0.02, 0.002, 0))) @ Matrix.Rotation(side * math.pi / 2, 4, 'Y'))
    lathe(b, [(0.074, 1.06), (0.08, 1.115), (0.073, 1.115), (0.067, 1.06)], "c2", n=16, cy=TORSO_CY, sy=0.95, closed=True)   # collar
    for side in (1, -1):
        shoe(b, side, "#0a0a0a", size=1.02, height=0.85)
    return b.finish()

def angler_waders():
    b = Builder("AnglerWaders", "outfit", "Angler's Waders", {"c1": "#4f5f34", "c2": "#2a2a2a", "c3": "#c03030"})
    def torso_key(z, a):
        return "c1" if z < 0.86 else "c3"                                   # waders up to the chest, shirt above
    def arm_key(s, a, side):
        if s > 0.40: return "skin"                                          # rolled-up sleeves
        return "c3"
    RT, zs = body_shell(b, torso_key, arm_key, lambda z, a, sd: "c1", off=0.009,
                        arm_extra=lambda s, a: 0.008 if 0.36 < s < 0.40 else 0.0,
                        leg_extra=lambda z, a: 0.006, torso_extra=lambda z, a: 0.004 if z < 0.86 else 0.0)
    for side in (1, -1):                                                    # suspenders over the shoulders
        surface_strap(b, RT, zs, [(0.84, side * 0.45), (0.95, side * 0.42), (1.06, side * 0.6), (1.10, side * 1.5),
                                  (1.06, side * 2.6), (0.95, side * 2.75), (0.84, side * 2.75)], 0.006, "c2")
    b.part = "torso"
    b.add_bm(p_box(0.07, 0.014, 0.06), "c1", Matrix.Translation(torso_point(RT, zs, 0.79, 0.0, 0.008)))   # chest pocket
    b.add_bm(p_ico(0.008, scale=(0.6, 0.6, 1.4)), "#ffcd00", Matrix.Translation(torso_point(RT, zs, 0.83, 0.25, 0.02)))  # lure
    for side in (1, -1):
        shoe(b, side, "c1", size=1.1, height=1.05)
        b.part = "shoe"
        b.add_bm(p_box(0.1, 0.24, 0.012), "#1a1a1a", Matrix.Translation((side * LEG_X, -0.065, 0.006)))   # sole
    return b.finish()

def hawaiian_shirt():
    b = Builder("HawaiianShirt", "outfit", "Hawaiian Shirt", {"c1": "#1aa3c8", "c2": "#ff5fa0", "c3": "#2ea84a"})
    def pattern(t, a):
        h = int(abs(math.sin(t * 91.7 + a * 37.3)) * 1000) % 9
        return "c2" if h == 0 else ("c3" if h in (1, 2) else "c1")
    def torso_key(z, a):
        if z < 0.55: return "#c8b48a"                                       # shorts
        if z > 0.98 and front_dist(a) < math.radians(28): return "skin"   # open collar
        return pattern(z, a)
    def arm_key(s, a, side):
        return pattern(s, a) if s < 0.29 else "skin"                       # short sleeves
    def leg_key(z, a, side):
        return "#c8b48a" if z > 0.29 else "skin"                           # shorts to the knee, bare legs
    RT, zs = body_shell(b, torso_key, arm_key, leg_key, off=0.008,
                        arm_extra=lambda s, a: 0.006 if s < 0.29 else -0.004,
                        leg_extra=lambda z, a: 0.006 if z > 0.29 else -0.004)
    for side in (1, -1):                                                    # flip-flops: skin feet on thin soles
        shoe(b, side, "skin", size=0.9, height=0.7)
        b.part = "shoe"
        b.add_bm(p_box(0.1, 0.25, 0.01), "#f2c200", Matrix.Translation((side * LEG_X, -0.065, 0.005)))
        b.add_bm(p_box(0.012, 0.06, 0.008), "#f2c200", Matrix.Translation((side * LEG_X, -0.12, 0.035)) @ Matrix.Rotation(0.5, 4, 'X'))
    return b.finish()

def viking_furs():
    b = Builder("VikingFurs", "outfit", "Viking Furs", {"c1": "#7a3a1a", "c2": "#9a9286", "c3": "#4a2a14"})
    def torso_key(z, a):
        return "c3" if 0.60 < z < 0.65 else "c1"                            # leather belt
    def arm_key(s, a, side):
        return "c3" if s > 0.42 else "c1"                                   # leather bracers
    def leg_key(z, a, side):
        return "c2" if z < 0.22 else "#3a3a3a"                              # fur-wrapped boots, dark trousers
    RT, zs = body_shell(b, torso_key, arm_key, leg_key, off=0.008,
                        arm_extra=lambda s, a: 0.007 if s > 0.42 else 0.0,
                        leg_extra=lambda z, a: 0.014 if z < 0.22 else 0.0)
    skirt(b, RT, zs, [(0.62, 0.006), (0.48, 0.02), (0.36, 0.03)], lambda i, a, hem: "c1", SPLIT)
    b.part = "torso"
    b.add_bm(p_box(0.045, 0.012, 0.04), "#c0c4c8", Matrix.Translation(torso_point(RT, zs, 0.625, 0.0, 0.005)))   # buckle
    import random; rnd = random.Random(7)
    for k in range(26):                                                     # fur mantle round the neck
        a = 2 * math.pi * k / 26
        b.add_bm(p_ico(rnd.uniform(0.022, 0.03), sub=1), "c2",
                 Matrix.Translation(torso_point(RT, zs, 1.05 + rnd.uniform(-0.01, 0.02), a, 0.008)))
    for side in (1, -1):                                                    # fur on the shoulders
        b.part = "arm"
        for k in range(6):
            b.add_bm(p_ico(rnd.uniform(0.02, 0.026), sub=1), "c2",
                     Matrix.Translation((side * (0.12 + 0.02 * k), rnd.uniform(-0.02, 0.02), ARM_Z + 0.04 + rnd.uniform(-0.005, 0.01))))
        b.part = "leg"
        for k in range(8):                                                  # fur tufts on the boots
            a = 2 * math.pi * k / 8
            b.add_bm(p_ico(0.016, sub=1), "c2", Matrix.Translation((side * LEG_X + 0.055 * math.sin(a), -0.055 * math.cos(a), 0.2)))
        shoe(b, side, "c3", size=1.08)
    return b.finish()

def scuba_wetsuit():
    b = Builder("ScubaWetsuit", "outfit", "Scuba Wetsuit", {"c1": "#151515", "c2": "#1aa3c8", "c3": "#ffcd00"})
    def stripe(a):
        return abs(math.sin(a)) > 0.93                                      # down both sides
    RT, zs = body_shell(b, lambda z, a: "c2" if stripe(a) else "c1",
                        lambda s, a, sd: "c2" if abs(math.sin(a)) > 0.93 else "c1",
                        lambda z, a, sd: "c2" if abs(math.sin(a)) > 0.93 else "c1", off=0.004)
    b.part = "torso"
    for side in (1, -1):                                                    # twin air tanks
        c = torso_point(RT, zs, 0.84, math.pi, 0.04) + Vector((side * 0.036, 0, 0))
        b.add_bm(p_cyl(0.034, 0.034, 0.30, seg=12), "c3", Matrix.Translation(c))
        b.add_bm(p_ico(0.034, sub=1, scale=(1, 1, 0.6)), "c3", Matrix.Translation(c + Vector((0, 0, 0.15))))
    b.add_bm(p_cyl(0.008, 0.008, 0.05, seg=6), "#c0c4c8", Matrix.Translation(torso_point(RT, zs, 1.0, math.pi, 0.04)))
    for side in (1, -1):                                                    # harness straps
        surface_strap(b, RT, zs, [(0.70, side * 2.6), (0.85, side * 2.4), (1.0, side * 2.0), (1.08, side * 1.4),
                                  (1.0, side * 0.6), (0.85, side * 0.45), (0.70, side * 0.45)], 0.006, "c1")
    for side in (1, -1):                                                    # booties and flippers
        shoe(b, side, "c1", size=1.02, height=0.9)
        b.add_bm(p_box(0.11, 0.24, 0.01), "c2", Matrix.Translation((side * LEG_X, -0.25, 0.012)))
    return b.finish()

def fishermans_raincoat():
    b = Builder("FishermansRaincoat", "outfit", "Fisherman's Raincoat", {"c1": "#ffd21a", "c2": "#3a2a1a", "c3": "#1a3a6a"})
    RT, zs = body_shell(b, lambda z, a: "c1", lambda s, a, sd: "c1",
                        lambda z, a, sd: "c3" if z < 0.24 else "#2a3a5a", off=0.011,
                        arm_extra=lambda s, a: 0.006, leg_extra=lambda z, a: 0.012 if z < 0.24 else 0.0)
    skirt(b, RT, zs, [(0.60, 0.012), (0.45, 0.024), (0.32, 0.034)], lambda i, a, hem: "c1",
          [(0.08, 2 * math.pi - 0.08)], part="skirt")
    b.part = "torso"
    for z in (0.66, 0.76, 0.86, 0.96):                                      # toggles
        b.add_bm(p_box(0.03, 0.01, 0.009), "c2", Matrix.Translation(torso_point(RT, zs, z, 0.0, 0.006)))
    lathe(b, [(0.08, 1.04), (0.1, 1.13), (0.092, 1.13), (0.072, 1.04)], "c1", n=16, cy=TORSO_CY, sy=0.95, closed=True)   # tall collar
    for side in (1, -1):
        shoe(b, side, "c3", size=1.1, height=1.05)
    return b.finish()

def souwester():
    b = Builder("Souwester", "hat", "Sou'wester", {"c1": "#ffd21a", "c2": "#ffd21a", "c3": "#3a2a1a"})
    lathe(b, dome_profile(0.103, 1.295, 1.41, t=0.005), "c1", n=24, closed=True)
    # brim: short at the front, long and drooping at the back to keep rain off the neck
    n = 28; inner, outer, inner_b, outer_b = [], [], [], []
    for k in range(n):
        a = 2 * math.pi * k / n
        back = (1 - math.cos(a)) / 2                                        # 0 at the front, 1 at the back
        r_out = 0.125 + 0.05 * back
        z_out = 1.292 - 0.045 * back
        for lst, r, z in ((inner, 0.100, 1.296), (outer, r_out, z_out)):
            lst.append(b.v((r * math.sin(a), HEAD_CY - r * 1.04 * math.cos(a), z)))
    inner_b = [b.v(v.co - Vector((0, 0, 0.005))) for v in inner]
    outer_b = [b.v(v.co - Vector((0, 0, 0.005))) for v in outer]
    for k in range(n):
        j = (k + 1) % n
        b.face([inner[k], inner[j], outer[j], outer[k]], "c2")
        b.face([inner_b[j], inner_b[k], outer_b[k], outer_b[j]], "c2")
        b.face([outer[k], outer[j], outer_b[j], outer_b[k]], "c2")
        b.face([inner[j], inner[k], inner_b[k], inner_b[j]], "c2")
    return b.finish()

# ORDER MATTERS: a cosmetic's position in the game's list is what gets saved and synced, so only ever
# add new entries at the END of this list, never reorder or remove.
COSMETICS = {f.__name__: f for f in [crown, captains_cap, pirate_bandana, fish_hat, angler_bucket_hat, top_hat,
                                      viking_helmet, propeller_beanie, wizard_hat, eyepatch, pipe, monocle,
                                      aviators, snorkel_mask, clown_nose, headphones, fish_in_mouth, head_bandage,
                                      pirate_coat, captains_uniform,
                                      wizard_robe, tuxedo, angler_waders, hawaiian_shirt, viking_furs, scuba_wetsuit,
                                      fishermans_raincoat, souwester]}

# ------------------------------------------------------------------ export

def export(ob, order):
    dg = bpy.context.evaluated_depsgraph_get()
    me = ob.evaluated_get(dg).to_mesh()
    bm = bmesh.new(); bm.from_mesh(me)
    bmesh.ops.triangulate(bm, faces=bm.faces)
    pos, nrm, uv0, slot, uv3, tris = [], [], [], [], [], []
    dl = bm.verts.layers.deform.active
    bones, weights = [], []          # outfits only: two influences per vertex (game bone index, weight)
    for f in bm.faces:
        mat = ob.material_slots[f.material_index].material
        key = mat["key"]
        u = cell_uv(*nearest_cell(mat["hex"]))
        s = SLOT_VALUE["fixed"] if key.startswith("#") else SLOT_VALUE[key]
        n = f.normal
        for v in f.verts:              # flat shading: every triangle gets its own corners
            tris.append(len(pos) // 3)
            pos += [round(c, 5) for c in v.co]
            nrm += [round(c, 4) for c in n]
            uv0 += [round(u[0], 5), round(u[1], 5)]
            slot.append(s)
            ax = max(range(3), key=lambda i: abs(n[i]))   # box projection for the subtle normal-map texture
            a, c = [(1, 2), (0, 2), (0, 1)][ax]
            uv3 += [round(v.co[a] * 3 % 1, 4), round(v.co[c] * 3 % 1, 4)]
            if dl is not None:
                top = sorted(v[dl].items(), key=lambda kv: -kv[1])[:2] or [(0, 1.0)]
                total = sum(w for _, w in top)
                top += [(0, 0.0)] * (2 - len(top))
                for bi, w in top:
                    bones.append(bi); weights.append(round(w / total, 4))
    bm.free(); ob.evaluated_get(dg).to_mesh_clear()
    data = {"name": ob.name, "displayName": ob["display_name"], "kind": ob["kind"], "order": order,
            "positions": pos, "normals": nrm, "uv0": uv0, "slot": slot, "uv3": uv3, "triangles": tris}
    if dl is not None:
        data["bones"], data["weights"] = bones, weights
    with open(os.path.join(OUT, ob.name + ".json"), "w") as fh:
        json.dump(data, fh, separators=(",", ":"))
    print(f"exported {ob['kind']} {ob.name}: {len(tris) // 3} triangles")

# ------------------------------------------------------------------ scene and preview renders

bpy.ops.wm.read_factory_settings(use_empty=True)
load_palette()
bpy.ops.wm.obj_import(filepath=os.path.join(REFS, "rest_head.obj"), forward_axis='Y', up_axis='Z')
head = bpy.context.selected_objects[0]; head.name = "REF_Head"
from mathutils.bvhtree import BVHTree
HEAD_TREE = BVHTree.FromObject(head, bpy.context.evaluated_depsgraph_get())
skin = bpy.data.materials.new("REF_skin"); skin.diffuse_color = cell_rgb(*nearest_cell("#bc9971"))
head.data.materials.clear(); head.data.materials.append(skin)
# reference body (a game outfit in rest pose) - only used to measure proportions, never exported
bpy.ops.wm.obj_import(filepath=os.path.join(REFS, "rest_outfit_00_FishingClothes1.obj"), forward_axis='Y', up_axis='Z')
ref_body = bpy.context.selected_objects[0]; ref_body.name = "REF_Body"; ref_body.hide_render = True
BODY_TREE = BVHTree.FromObject(ref_body, bpy.context.evaluated_depsgraph_get())

made = []
for order, (name, fn) in enumerate(COSMETICS.items()):
    if ONLY and name != ONLY: continue
    ob = fn(); export(ob, order); made.append(ob)

# ---- clearance check: does any part of the head poke out through the cosmetic?
# Cast a ray from the middle of the head out through each head vertex; if it hits the cosmetic before
# reaching the vertex, that vertex is outside the cosmetic, i.e. visible through it.
from mathutils.bvhtree import BVHTree
dg = bpy.context.evaluated_depsgraph_get()
head_pts = [head.matrix_world @ v.co for v in head.data.vertices]
centre = Vector((0, HEAD_CY, 1.25))
for ob in made:
    if ob["kind"] == "outfit": continue
    tree = BVHTree.FromObject(ob, dg)
    pokes = []
    for p in head_pts:
        d = p - centre
        hit = tree.ray_cast(centre, d.normalized(), d.length)
        # ...and nothing of the cosmetic covers it further out (otherwise it's just tucked inside)
        if hit[0] is not None and tree.ray_cast(p + d.normalized() * 1e-4, d.normalized(), 1.0)[0] is None:
            pokes.append((d.length - (hit[0] - centre).length, p))
    if pokes:
        depth, where = max(pokes, key=lambda x: x[0])
        print(f"CLEARANCE {ob.name}: {len(pokes)} head verts poke through, worst {depth * 1000:.1f} mm at "
              f"({where.x:.3f}, {where.y:.3f}, {where.z:.3f})")
    else:
        print(f"CLEARANCE {ob.name}: ok")

scene = bpy.context.scene
scene.render.engine = 'BLENDER_WORKBENCH'
scene.display.shading.light = 'STUDIO'
scene.display.shading.color_type = 'MATERIAL'
scene.display.shading.show_cavity = True
scene.render.resolution_x = scene.render.resolution_y = 480
scene.world = bpy.data.worlds.new("w")
cam = bpy.data.objects.new("cam", bpy.data.cameras.new("cam")); cam.data.lens = 85
bpy.context.collection.objects.link(cam); scene.camera = cam
# ---- skeleton for posing outfits in the previews (rebuilt from the game's rest-pose bone data)
def build_armature():
    pos, parent = {}, {}
    for line in open(os.path.join(REFS, "rest_bones.txt")):
        if line.startswith("#"): continue
        left, mat = line.split("|")
        _, name, par = left.split()
        rows = [list(map(float, r.split(","))) for r in mat.split()]
        pos[name] = Vector((-rows[0][3], rows[1][3], rows[2][3]))      # mirror X like the OBJs
        parent[name] = par
    children = {n: [c for c in pos if parent[c] == n] for n in pos}
    arm_data = bpy.data.armatures.new("Skeleton")
    arm = bpy.data.objects.new("Skeleton", arm_data)
    bpy.context.collection.objects.link(arm)
    bpy.context.view_layer.objects.active = arm
    bpy.ops.object.mode_set(mode='EDIT')
    leaf_dir = {"Hand_L": (0.06, 0, 0), "Hand_R": (-0.06, 0, 0), "Foot_L": (0, -0.08, 0), "Foot_R": (0, -0.08, 0)}
    for n in BONE_NAMES:
        eb = arm_data.edit_bones.new(n)
        eb.head = pos[n]
        kids = [c for c in children[n] if c in ("Head", "ArmLower_L", "ArmLower_R", "Hand_L", "Hand_R", "LegLower_L", "LegLower_R", "Foot_L", "Foot_R")]
        eb.tail = pos[kids[0]] if kids else pos[n] + Vector(leaf_dir.get(n, (0, 0, 0.04)))
    for n in BONE_NAMES:
        if parent[n] in arm_data.edit_bones:
            arm_data.edit_bones[n].parent = arm_data.edit_bones[parent[n]]
    bpy.ops.object.mode_set(mode='OBJECT')
    return arm

def pose(arm, name, axis, degrees):
    """Rotate a bone about a world axis (in its rest frame)."""
    pb = arm.pose.bones[name]
    pb.rotation_mode = 'QUATERNION'
    rest = pb.bone.matrix_local.to_quaternion()
    world = Matrix.Rotation(math.radians(degrees), 4, axis).to_quaternion()
    pb.rotation_quaternion = rest.inverted() @ world @ rest

outfits = [ob for ob in made if ob["kind"] == "outfit"]
skeleton = build_armature() if outfits else None

for ob in made:
    for other in made: other.hide_render = other is not ob
    if ob["kind"] == "outfit":
        mod = ob.modifiers.new("Skeleton", 'ARMATURE'); mod.object = skeleton
        target = Vector((0, 0, 0.62)); d = 3.3
        for i, (az, el, posed) in enumerate([(-30, 8, False), (35, 8, True), (165, 12, True)]):
            for pb in skeleton.pose.bones: pb.rotation_mode = 'QUATERNION'; pb.rotation_quaternion = (1, 0, 0, 0)
            if posed:   # arms down and bent, one leg stepping forward: shows whether the skinning holds up
                pose(skeleton, "ArmUpper_L", 'Y', 55); pose(skeleton, "ArmLower_L", 'Z', 50)
                pose(skeleton, "ArmUpper_R", 'Y', -40); pose(skeleton, "ArmLower_R", 'Z', 45)
                pose(skeleton, "LegUpper_L", 'X', 35); pose(skeleton, "LegLower_L", 'X', -50)
                pose(skeleton, "LegUpper_R", 'X', -20)
            bpy.context.view_layer.update()
            a, e = math.radians(az), math.radians(el)
            cam.location = target + Vector((d * math.sin(a) * math.cos(e), -d * math.cos(a) * math.cos(e), d * math.sin(e)))
            cam.rotation_euler = (target - cam.location).to_track_quat('-Z', 'Y').to_euler()
            scene.render.filepath = os.path.join(OUT, "previews", f"{ob.name}_{i}.png")
            bpy.ops.render.render(write_still=True)
        for pb in skeleton.pose.bones: pb.rotation_quaternion = (1, 0, 0, 0)
        continue
    top = max((ob.matrix_world @ Vector(c)).z for c in ob.bound_box)
    target = Vector((0, HEAD_CY, (1.24 + top) / 2)); d = 0.55 + max(0, top - 1.42) * 1.6
    for i, (az, el) in enumerate([(-35, 12), (145, 22)]):
        a, e = math.radians(az), math.radians(el)
        cam.location = target + Vector((d * math.sin(a) * math.cos(e), -d * math.cos(a) * math.cos(e), d * math.sin(e)))
        cam.rotation_euler = (target - cam.location).to_track_quat('-Z', 'Y').to_euler()
        scene.render.filepath = os.path.join(OUT, "previews", f"{ob.name}_{i}.png")
        bpy.ops.render.render(write_still=True)
for ob in made: ob.hide_render = False
bpy.ops.wm.save_as_mainfile(filepath=os.path.join(OUT, "cosmetics_preview.blend"))
print("done")
