
# SPDX-License-Identifier: GPL-3.0-or-later
# Interstate '76 GEO Importer (Batch)

bl_info = {
    "name": "Interstate '76 .GEO Importer (Batch)",
    "author": "chasseyblue.com",
    "version": (0, 2, 0),
    "blender": (2, 93, 0),
    "location": "File > Import > Interstate '76 GEO (batch) (.geo)",
    "description": "Imports classic I'76 .geo files (one or many) with UVs and material slots.",
    "category": "Import-Export",
}

import bpy, bmesh, struct, math, os
from bpy.types import Operator, PropertyGroup
from bpy.props import (
    StringProperty, FloatProperty, EnumProperty, BoolProperty,
    CollectionProperty
)
from bpy_extras.io_utils import ImportHelper

# ---- Binary helpers ----
def rU32(f): return struct.unpack("<I", f.read(4))[0]
def rF32(f): return struct.unpack("<f", f.read(4))[0]
def rCSTR(f, n): return f.read(n).split(b"\x00", 1)[0].decode("ascii", "ignore")
def read_vec3(f): return (rF32(f), rF32(f), rF32(f))
def read_vec2(f): return (rF32(f), rF32(f))

def parse_geo_classic(path):
    with open(path, "rb") as f:
        size = os.path.getsize(path)
        if size < 64:
            raise ValueError("File too small to be a GEO")
        magic = rCSTR(f, 4)          # often GEO\0, not enforced
        _u1 = rU32(f)                # unknown
        name = rCSTR(f, 16) or os.path.basename(path)

        vct = rU32(f)
        fct = rU32(f)
        _u2 = rU32(f)

        if not (1 <= vct <= 2_000_000 and 1 <= fct <= 2_000_000):
            raise ValueError(f"Unreasonable counts v={vct} f={fct}")

        verts = [read_vec3(f) for _ in range(vct)]
        norms = [read_vec3(f) for _ in range(vct)]  # stored but unused

        faces = []  # list of { tex: str, refs: [(vi, ni, (u,v)), ...] }
        for _ in range(fct):
            face_id = rU32(f)
            nv = rU32(f)
            # observed face header block
            f.read(3)                 # color? RGB
            f.read(16)                # plane (4 floats)?
            f.read(4)                 # unknown u32
            f.read(3)                 # flags?
            tex = rCSTR(f, 13) or "i76_default"
            f.read(4)                 # unknown u32
            f.read(4)                 # unknown u32

            refs = []
            for __ in range(nv):
                vi = rU32(f)
                ni = rU32(f)
                u, v = read_vec2(f)
                refs.append((vi, ni, (u, v)))
            faces.append({"tex": tex, "refs": refs})

        return {"name": name, "verts": verts, "faces": faces}

def build_mesh(geo, scale=1.0, mat_cache=None):
    if mat_cache is None:
        mat_cache = {}

    bm = bmesh.new()
    verts = [bm.verts.new((x*scale, y*scale, z*scale)) for (x,y,z) in geo["verts"]]
    bm.verts.ensure_lookup_table()

    me = bpy.data.meshes.new(geo["name"] + "_Mesh")
    ob = bpy.data.objects.new(geo["name"], me)

    # Materials by texture name (shared across batch via cache)
    for face in geo["faces"]:
        t = face["tex"]
        if t not in mat_cache:
            m = bpy.data.materials.new(t)
            m.use_nodes = True
            mat_cache[t] = m
        if mat_cache[t].name not in [m.name for m in ob.data.materials]:
            ob.data.materials.append(mat_cache[t])

    # UV layer
    uv_layer = bm.loops.layers.uv.new("UVMap")

    # Triangulate fan per face
    mat_index_of = {m.name: i for i, m in enumerate(ob.data.materials)}
    for face in geo["faces"]:
        refs = face["refs"]
        if len(refs) < 3:
            continue
        mid = mat_index_of.get(face["tex"], 0)
        a = refs[0]
        for i in range(2, len(refs)):
            b = refs[i-1]
            c = refs[i]
            try:
                f = bm.faces.new((verts[a[0]], verts[b[0]], verts[c[0]]))
            except ValueError:
                continue
            f.material_index = mid
            f.smooth = True
            for loop, ref in zip(f.loops, (a, b, c)):
                loop[uv_layer].uv = ref[2]

    bm.normal_update()
    bm.to_mesh(me)
    bm.free()
    return ob

class I76_OT_ImportGEOClassicBatch(Operator, ImportHelper):
    bl_idname = "i76.import_geo_classic_batch"
    bl_label  = "Interstate '76 GEO (classic, batch) (.geo)"
    bl_options = {'REGISTER', 'UNDO'}
    filename_ext = ".geo"
    filter_glob: StringProperty(default="*.geo", options={'HIDDEN'})

    # Multi-file selection
    files: CollectionProperty(name="File Path", type=PropertyGroup)
    directory: StringProperty(subtype='DIR_PATH')

    # Options
    scale: FloatProperty(name="Scale", default=1.0, min=0.0001, soft_max=1000.0)
    axis_up: EnumProperty(
        name="Up Axis",
        items=(('Z', "Z Up (Blender)", ""), ('Y', "Y Up", "")),
        default='Z'
    )
    batch_collection: BoolProperty(
        name="Put all in one Collection",
        default=True
    )
    collection_name: StringProperty(
        name="Collection Name",
        default="I76_GEO_Batch"
    )
    parent_empty: BoolProperty(
        name="Create Parent Empty",
        default=True
    )
    parent_name: StringProperty(
        name="Parent Name",
        default="I76_GEO_Parts"
    )

    def execute(self, context):
        # Resolve selection
        filepaths = []
        if self.files:
            for f in self.files:
                filepaths.append(os.path.join(self.directory, f.name))
        else:
            filepaths.append(self.filepath)

        # Prepare targets
        col = None
        if self.batch_collection:
            col = bpy.data.collections.new(self.collection_name)
            context.scene.collection.children.link(col)

        parent = None
        if self.parent_empty:
            parent = bpy.data.objects.new(self.parent_name, None)
            parent.empty_display_type = 'PLAIN_AXES'
            if col:
                col.objects.link(parent)
            else:
                context.scene.collection.objects.link(parent)

        # Shared materials across batch
        mat_cache = {}

        imported = 0
        errors = []
        for path in filepaths:
            try:
                geo = parse_geo_classic(path)
                ob = build_mesh(geo, scale=self.scale, mat_cache=mat_cache)

                if self.axis_up == 'Y':
                    ob.rotation_euler[0] = math.radians(-90.0)

                # Link and parent
                if col:
                    col.objects.link(ob)
                else:
                    context.scene.collection.objects.link(ob)

                if parent:
                    ob.parent = parent

                imported += 1
            except Exception as e:
                errors.append((path, str(e)))

        # Selection feedback
        if imported:
            if parent:
                parent.select_set(True)
                context.view_layer.objects.active = parent

        msg = f"Imported {imported} GEO file(s)"
        if errors:
            msg += f"; {len(errors)} failed - check Console"
            for p, err in errors:
                print(f"[I76 GEO] ERROR {p}: {err}")
        self.report({'INFO'}, msg)
        return {'FINISHED'}

def menu_func(self, context):
    self.layout.operator(I76_OT_ImportGEOClassicBatch.bl_idname, text="Interstate '76 GEO (batch) (.geo)")

classes = (I76_OT_ImportGEOClassicBatch,)

def register():
    for c in classes:
        bpy.utils.register_class(c)
    bpy.types.TOPBAR_MT_file_import.append(menu_func)

def unregister():
    bpy.types.TOPBAR_MT_file_import.remove(menu_func)
    for c in reversed(classes):
        bpy.utils.unregister_class(c)

if __name__ == "__main__":
    register()
