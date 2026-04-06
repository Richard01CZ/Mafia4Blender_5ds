from datetime import datetime
import os
import struct
import bpy  # type: ignore
from mathutils import Quaternion, Vector, Matrix  # type: ignore
from bpy_extras.io_utils import ImportHelper, ExportHelper  # type: ignore
from bpy_extras import anim_utils  # type: ignore
from bpy.props import StringProperty, BoolProperty, EnumProperty, IntProperty  # type: ignore

bl_info = {
    "name": "LS3D 5DS Animation Importer/Exporter",
    "author": "Richard01_CZ",
    "version": (0, 1, 0),
    "blender": (5, 1, 0),
    "location": "File > Import/Export > 5DS Animation File",
    "description": "Import and export LS3D .5ds animation files (Mafia)",
    "category": "Import-Export",
}

# ── 5DS Format Constants ─────────────────────────────────────────────────────

VERSION_MAFIA = 20

KEY_POSITION = 0x02
KEY_ROTATION = 0x04
KEY_SCALE    = 0x08
KEY_NOTE     = 0x10  # Note/sound events (flag 16 per MaxScript; 5ds.bt template says 32 — template error)

HEADER_SIZE = 18  # 4 (fourcc) + 2 (version) + 8 (timestamp) + 4 (datasize)


# ── Flag Property Helpers (same pattern as 4ds addon) ────────────────────────

def get_flag_mask(self, prop_name, mask):
    """Returns True if mask is set (unsigned-safe)."""
    return (getattr(self, prop_name, 0) & mask) != 0

def set_flag_mask(self, value, prop_name, mask):
    """Sets/clears mask safely on signed 32-bit storage."""
    current_signed = getattr(self, prop_name, 0)
    current_unsigned = current_signed & 0xFFFFFFFF

    if value:
        new_unsigned = current_unsigned | mask
    else:
        new_unsigned = current_unsigned & ~mask

    if new_unsigned >= 0x80000000:
        new_signed = new_unsigned - 0x100000000
    else:
        new_signed = new_unsigned

    setattr(self, prop_name, int(new_signed))

def make_5ds_getter(mask):
    return lambda self: get_flag_mask(self, "ls3d_5ds_flags", mask)

def make_5ds_setter(mask):
    return lambda self, value: set_flag_mask(self, value, "ls3d_5ds_flags", mask)


# ── Coordinate Conversion ────────────────────────────────────────────────────
# LS3D engine: X=right, Y=up, Z=depth
# Blender:     X=right, Y=depth, Z=up
# Conversion:  swap Y and Z in both position/scale and quaternion components.

def ls3d_to_blender_pos(x, y, z):
    return Vector((x, z, y))

def blender_to_ls3d_pos(vec):
    return (vec.x, vec.z, vec.y)

def ls3d_to_blender_quat(w, x, y, z):
    return Quaternion((w, x, z, y))

def blender_to_ls3d_quat(q):
    return (q.w, q.x, q.z, q.y)

def ls3d_to_blender_scale(x, y, z):
    return Vector((x, z, y))

def blender_to_ls3d_scale(vec):
    return (vec.x, vec.z, vec.y)


# ── 5DS Parser ───────────────────────────────────────────────────────────────

class AnimNode:
    """Parsed animation data for a single node."""
    __slots__ = (
        'name', 'name_offset', 'data_offset',
        'flags',
        'rot_frames', 'rot_keys',
        'pos_frames', 'pos_keys',
        'scl_frames', 'scl_keys',
        'note_frames', 'note_keys',
    )

    def __init__(self):
        self.name = ""
        self.name_offset = 0
        self.data_offset = 0
        self.flags = 0
        self.rot_frames = []
        self.rot_keys = []
        self.pos_frames = []
        self.pos_keys = []
        self.scl_frames = []
        self.scl_keys = []
        self.note_frames = []
        self.note_keys = []


def parse_5ds(filepath):
    """Parse a 5DS file and return (num_frames, list[AnimNode])."""
    with open(filepath, "rb") as f:
        # ── Header ──
        fourcc = f.read(4)
        if fourcc != b'5DS\x00':
            raise ValueError(f"Invalid 5DS header: {fourcc}")

        version = struct.unpack("<H", f.read(2))[0]
        if version != VERSION_MAFIA:
            raise ValueError(f"Unsupported 5DS version: {version} (expected {VERSION_MAFIA})")

        _timestamp = f.read(8)
        _data_size = struct.unpack("<I", f.read(4))[0]

        # ── Data section ──
        num_nodes = struct.unpack("<H", f.read(2))[0]
        num_frames = struct.unpack("<H", f.read(2))[0]

        # Link offset table
        nodes = []
        for _ in range(num_nodes):
            node = AnimNode()
            node.name_offset, node.data_offset = struct.unpack("<II", f.read(8))
            nodes.append(node)

        # ── Read node names ──
        for node in nodes:
            f.seek(node.name_offset + HEADER_SIZE)
            chars = []
            while True:
                c = f.read(1)
                if c == b'\x00' or not c:
                    break
                chars.append(c)
            node.name = b''.join(chars).decode("windows-1250", errors="replace")

        # ── Read animation data ──
        for node in nodes:
            f.seek(node.data_offset + HEADER_SIZE)
            node.flags = struct.unpack("<I", f.read(4))[0]

            if node.flags & KEY_ROTATION:
                count = struct.unpack("<H", f.read(2))[0]
                node.rot_frames = list(struct.unpack(f"<{count}H", f.read(2 * count)))
                if count % 2 == 0:
                    f.read(2)  # alignment padding
                for _ in range(count):
                    w, x, y, z = struct.unpack("<4f", f.read(16))
                    node.rot_keys.append(ls3d_to_blender_quat(w, x, y, z))

            if node.flags & KEY_POSITION:
                count = struct.unpack("<H", f.read(2))[0]
                node.pos_frames = list(struct.unpack(f"<{count}H", f.read(2 * count)))
                if count % 2 == 0:
                    f.read(2)  # alignment padding
                for _ in range(count):
                    x, y, z = struct.unpack("<3f", f.read(12))
                    node.pos_keys.append(ls3d_to_blender_pos(x, y, z))

            if node.flags & KEY_SCALE:
                count = struct.unpack("<H", f.read(2))[0]
                node.scl_frames = list(struct.unpack(f"<{count}H", f.read(2 * count)))
                if count % 2 == 0:
                    f.read(2)  # alignment padding
                for _ in range(count):
                    x, y, z = struct.unpack("<3f", f.read(12))
                    node.scl_keys.append(ls3d_to_blender_scale(x, y, z))

            if node.flags & KEY_NOTE:
                count = struct.unpack("<H", f.read(2))[0]
                node.note_frames = list(struct.unpack(f"<{count}H", f.read(2 * count)))
                for _ in range(count):
                    val = struct.unpack("<H", f.read(2))[0]
                    node.note_keys.append(val)
                f.read(2)  # trailing padding

    return num_frames, nodes


# ── 5DS Writer ────────────────────────────────────────────────────────────────

def write_5ds(filepath, num_frames, nodes):
    """Write a 5DS file from a list of AnimNode objects (already in LS3D coords)."""
    with open(filepath, "wb") as f:
        # ── Header (placeholder dataSize, fill later) ──
        f.write(b'5DS\x00')
        f.write(struct.pack("<H", VERSION_MAFIA))
        # Timestamp: current time as Windows FILETIME
        epoch_diff = 116444736000000000  # diff between 1601 and 1970 in 100ns
        now_ft = int(datetime.now().timestamp() * 10000000) + epoch_diff
        f.write(struct.pack("<Q", now_ft))
        data_size_offset = f.tell()
        f.write(struct.pack("<I", 0))  # placeholder

        # ── Data section start ──
        num_nodes = len(nodes)
        f.write(struct.pack("<H", num_nodes))
        f.write(struct.pack("<H", num_frames))

        # Link offset table (placeholders)
        link_table_offset = f.tell()
        for _ in range(num_nodes):
            f.write(struct.pack("<II", 0, 0))

        # ── Animation data ──
        data_offsets = []
        for node in nodes:
            data_offsets.append(f.tell() - HEADER_SIZE)

            f.write(struct.pack("<I", node.flags))

            if node.flags & KEY_ROTATION:
                count = len(node.rot_frames)
                f.write(struct.pack("<H", count))
                f.write(struct.pack(f"<{count}H", *node.rot_frames))
                if count % 2 == 0:
                    f.write(struct.pack("<H", 0))
                for q in node.rot_keys:
                    f.write(struct.pack("<4f", *q))

            if node.flags & KEY_POSITION:
                count = len(node.pos_frames)
                f.write(struct.pack("<H", count))
                f.write(struct.pack(f"<{count}H", *node.pos_frames))
                if count % 2 == 0:
                    f.write(struct.pack("<H", 0))
                for p in node.pos_keys:
                    f.write(struct.pack("<3f", *p))

            if node.flags & KEY_SCALE:
                count = len(node.scl_frames)
                f.write(struct.pack("<H", count))
                f.write(struct.pack(f"<{count}H", *node.scl_frames))
                if count % 2 == 0:
                    f.write(struct.pack("<H", 0))
                for s in node.scl_keys:
                    f.write(struct.pack("<3f", *s))

            if node.flags & KEY_NOTE:
                count = len(node.note_frames)
                f.write(struct.pack("<H", count))
                f.write(struct.pack(f"<{count}H", *node.note_frames))
                for v in node.note_keys:
                    f.write(struct.pack("<H", v))
                f.write(struct.pack("<H", 0))  # trailing padding

        # ── Node names ──
        name_offsets = []
        for node in nodes:
            name_offsets.append(f.tell() - HEADER_SIZE)
            f.write(node.name.encode("windows-1250") + b'\x00')

        # ── Patch link offset table ──
        f.seek(link_table_offset)
        for i in range(num_nodes):
            f.write(struct.pack("<II", name_offsets[i], data_offsets[i]))

        # ── Patch data size ──
        f.seek(0, 2)
        total_data = f.tell() - HEADER_SIZE
        f.seek(data_size_offset)
        f.write(struct.pack("<I", total_data))


# ── Blender 5.x Helpers ──────────────────────────────────────────────────────

def is_skinned_mesh(obj):
    """Check if an object is a skinned mesh (parented to an armature with an Armature modifier)."""
    if obj.parent and obj.parent.type == 'ARMATURE':
        for mod in obj.modifiers:
            if mod.type == 'ARMATURE' and mod.object == obj.parent:
                return True
    return False


def find_target(name):
    """Find a Blender object or pose bone by name.
    Returns (target, is_pose_bone, skin_rest_matrix).
    For skinned meshes, returns the parent armature and the mesh's rest matrix
    so the animation can be applied as a delta (bones already include the rest offset).
    skin_rest_matrix is None for non-skinned targets."""
    obj = bpy.data.objects.get(name)
    if obj is not None:
        if is_skinned_mesh(obj):
            # Capture the mesh's rest transform before redirecting to armature
            rest_mat = Matrix.LocRotScale(
                obj.location.copy(),
                obj.rotation_quaternion.copy() if obj.rotation_mode == 'QUATERNION'
                    else obj.rotation_euler.to_quaternion(),
                obj.scale.copy(),
            )
            return obj.parent, False, rest_mat
        return obj, False, None

    for arm_obj in bpy.data.objects:
        if arm_obj.type == 'ARMATURE' and arm_obj.pose:
            pb = arm_obj.pose.bones.get(name)
            if pb is not None:
                return pb, True, None

    return None, False, None


def get_channelbag_for_object(obj):
    """Get the channelbag for an object's animation data (Blender 5.x slotted actions)."""
    anim_data = obj.animation_data
    if not anim_data or not anim_data.action or not anim_data.action_slot:
        return None
    return anim_utils.action_get_channelbag_for_slot(anim_data.action, anim_data.action_slot)


def get_fcurves_for_object(obj):
    """Get iterable fcurves for an object, handling the Blender 5.x channelbag API."""
    cb = get_channelbag_for_object(obj)
    if cb is not None:
        return cb.fcurves
    return None


def find_fcurve(fcurves, data_path, index=0):
    """Find an fcurve by data_path and index in a channelbag fcurves collection."""
    if fcurves is None:
        return None
    for fc in fcurves:
        if fc.data_path == data_path and fc.array_index == index:
            return fc
    return None


def ensure_channelbag_for_object(obj):
    """Ensure an object has animation_data, action, slot, and channelbag. Returns channelbag."""
    if not obj.animation_data:
        obj.animation_data_create()
    anim_data = obj.animation_data
    if not anim_data.action:
        anim_data.action = bpy.data.actions.new(name=obj.name + "_5ds_Action")
    action = anim_data.action
    if not anim_data.action_slot:
        slot = action.slots.new(id_type='OBJECT', name=obj.name)
        anim_data.action_slot = slot
    return anim_utils.action_ensure_channelbag_for_slot(action, anim_data.action_slot)


def get_base_matrix_for_armature(arm_obj):
    """Find the skinned mesh child ('base') of an armature and return its rest matrix.
    In LS3D, root bones are children of 'base' (the singlemesh), so their rest pose
    in Blender (which is in world space) needs to be made relative to base's transform."""
    for child in arm_obj.children:
        if is_skinned_mesh(child):
            return Matrix.LocRotScale(
                child.location.copy(),
                child.rotation_quaternion.copy() if child.rotation_mode == 'QUATERNION'
                    else child.rotation_euler.to_quaternion(),
                child.scale.copy(),
            )
    return None


def get_bone_rest_local(bone, base_matrix=None):
    """Get a bone's rest-pose local matrix relative to its Blender parent.
    For root bones (no parent), if base_matrix is provided, computes the rest
    pose relative to the base (singlemesh) transform instead of world space.
    Returns (rest_matrix_4x4, rest_rotation_quaternion, rest_translation_vector).

    NOTE: This must return the same rest transform that Blender's pose system
    uses internally (bone.parent.matrix_local^-1 @ bone.matrix_local), so that
    the delta rotations/positions we compute are correct when applied as
    pose_bone.rotation_quaternion / pose_bone.location.  For bones parented to
    the base bone, this means using the base bone as parent (NOT base_matrix)."""
    if bone.parent:
        rest_local = bone.parent.matrix_local.inverted() @ bone.matrix_local
    elif base_matrix is not None:
        rest_local = base_matrix.inverted() @ bone.matrix_local
    else:
        rest_local = bone.matrix_local
    return rest_local, rest_local.to_quaternion(), rest_local.to_translation()



def convert_pose_bone_rotation(rest_rot, anim_rot):
    """Convert an absolute local rotation (from 5DS) to a pose-bone-relative rotation.
    pose_bone.rotation_quaternion is relative to the bone's rest orientation."""
    return rest_rot.inverted() @ anim_rot


def convert_pose_bone_location(rest_rot, rest_pos, anim_pos):
    """Convert an absolute local position (from 5DS) to a pose-bone-relative location.
    pose_bone.location is a displacement from rest position, in the bone's rest frame."""
    delta = anim_pos - rest_pos
    return rest_rot.inverted() @ delta


def convert_pose_bone_scale(rest_local, anim_scl):
    """Convert an absolute local scale (from 5DS) to a pose-bone-relative scale.
    Divides animated scale by rest scale component-wise."""
    rest_scl = rest_local.to_scale()
    return Vector((
        anim_scl.x / rest_scl.x if rest_scl.x != 0 else anim_scl.x,
        anim_scl.y / rest_scl.y if rest_scl.y != 0 else anim_scl.y,
        anim_scl.z / rest_scl.z if rest_scl.z != 0 else anim_scl.z,
    ))


def insert_keyframes_channelbag(channelbag, data_path, num_components, frames, values, group_name=""):
    """Batch-insert keyframes using the Blender 5.x channelbag fcurve API."""
    for comp_idx in range(num_components):
        # Find or create fcurve
        fc = None
        for existing_fc in channelbag.fcurves:
            if existing_fc.data_path == data_path and existing_fc.array_index == comp_idx:
                fc = existing_fc
                break
        if fc is None:
            fc = channelbag.fcurves.new(data_path, index=comp_idx, group_name=group_name)

        # Batch insert keyframe data
        kf_data = []
        for i, frame in enumerate(frames):
            kf_data.append(float(frame))
            kf_data.append(values[i][comp_idx])

        fc.keyframe_points.add(count=len(frames))
        fc.keyframe_points.foreach_set("co", kf_data)

        for kp in fc.keyframe_points:
            kp.interpolation = 'LINEAR'

        fc.update()


# ── Import Operator ───────────────────────────────────────────────────────────

class Import5DS(bpy.types.Operator, ImportHelper):
    """Import LS3D 5DS animation file"""
    bl_idname = "import_scene.5ds"
    bl_label = "Import 5DS"
    bl_options = {"REGISTER", "UNDO"}
    filename_ext = ".5ds"
    filter_glob: StringProperty(default="*.5ds", options={"HIDDEN"})

    clear_existing: BoolProperty(
        name="Clear Existing Animation",
        description="Remove existing animation data on matched objects before importing",
        default=True,
    )

    def execute(self, context):
        try:
            num_frames, nodes = parse_5ds(self.filepath)
        except ValueError as e:
            self.report({'ERROR'}, str(e))
            return {'CANCELLED'}

        matched = 0
        skipped_names = []
        base_mat_cache = {}  # armature name → base matrix (or None)

        for node in nodes:
            target, is_pose_bone, skin_rest_matrix = find_target(node.name)

            if target is None:
                skipped_names.append(node.name)
                continue

            matched += 1

            # Store the 5DS flags on the target
            target.ls3d_5ds_flags = node.flags

            # Set rotation mode to quaternion
            target.rotation_mode = 'QUATERNION'

            # Determine the ID object (armature for pose bones, object itself otherwise)
            if is_pose_bone:
                id_obj = target.id_data
            else:
                id_obj = target

            # Clear existing animation if requested
            if self.clear_existing:
                fcurves = get_fcurves_for_object(id_obj)
                if fcurves is not None:
                    if is_pose_bone:
                        prefix = f'pose.bones["{node.name}"].'
                    elif skin_rest_matrix is not None:
                        # Skinned mesh redirected to armature — only clear object-level fcurves,
                        # do NOT wipe bone animation with animation_data_clear()
                        prefix = None  # marker: clear non-bone fcurves
                    else:
                        prefix = ""  # clear all

                    if prefix is None:
                        # Clear only object-level fcurves (no pose.bones prefix)
                        to_remove = [fc for fc in fcurves
                                     if not fc.data_path.startswith("pose.bones[")]
                        for fc in to_remove:
                            fcurves.remove(fc)
                    elif prefix:
                        to_remove = [fc for fc in fcurves if fc.data_path.startswith(prefix)]
                        for fc in to_remove:
                            fcurves.remove(fc)
                    else:
                        # Regular object: clear everything
                        if id_obj.animation_data:
                            id_obj.animation_data_clear()

            # Get or create channelbag for keyframe insertion
            channelbag = ensure_channelbag_for_object(id_obj)

            # Determine data_path prefix for pose bones
            dp_prefix = f'pose.bones["{node.name}"].' if is_pose_bone else ""

            # For pose bones, compute rest pose delta
            if is_pose_bone:
                data_bone = id_obj.data.bones[node.name]
                arm_name = id_obj.name
                if arm_name not in base_mat_cache:
                    base_mat_cache[arm_name] = get_base_matrix_for_armature(id_obj)
                rest_local, rest_rot, rest_pos = get_bone_rest_local(data_bone, base_mat_cache[arm_name])

            # For skinned mesh redirected to armature, precompute rest components
            # Armature rotation: delta_rot = anim_rot @ rest_rot^-1
            # Armature position: delta_pos = anim_pos - delta_rot @ rest_pos
            if skin_rest_matrix is not None:
                skin_rest_rot = skin_rest_matrix.to_quaternion()
                skin_rest_pos = skin_rest_matrix.to_translation()
                skin_rest_rot_inv = skin_rest_rot.inverted()
            else:
                skin_rest_rot = skin_rest_pos = skin_rest_rot_inv = None

            # ── Rotation keyframes ──
            if node.rot_keys:
                if is_pose_bone:
                    values = []
                    for q in node.rot_keys:
                        pr = convert_pose_bone_rotation(rest_rot, q)
                        values.append((pr.w, pr.x, pr.y, pr.z))
                elif skin_rest_rot_inv is not None:
                    # Skinned mesh → armature: delta_rot = anim_rot @ rest_rot^-1
                    values = []
                    for q in node.rot_keys:
                        dr = q @ skin_rest_rot_inv
                        values.append((dr.w, dr.x, dr.y, dr.z))
                else:
                    values = [(q.w, q.x, q.y, q.z) for q in node.rot_keys]
                insert_keyframes_channelbag(
                    channelbag, dp_prefix + "rotation_quaternion", 4,
                    node.rot_frames, values, group_name=node.name,
                )

            # ── Position keyframes ──
            if node.pos_keys:
                if is_pose_bone:
                    values = []
                    for v in node.pos_keys:
                        pl = convert_pose_bone_location(rest_rot, rest_pos, v)
                        values.append(tuple(pl))
                elif skin_rest_rot_inv is not None:
                    # Skinned mesh → armature: delta_pos = anim_pos - delta_rot @ rest_pos
                    # delta_rot at each frame depends on the rotation at that same time
                    values = []
                    for i, v in enumerate(node.pos_keys):
                        # Get the rotation at this position frame's time
                        if node.rot_keys and i < len(node.rot_keys):
                            anim_rot_at_frame = node.rot_keys[i]
                        else:
                            anim_rot_at_frame = skin_rest_rot  # identity delta
                        dr = anim_rot_at_frame @ skin_rest_rot_inv
                        dl = v - dr @ skin_rest_pos
                        values.append(tuple(dl))
                else:
                    values = [tuple(v) for v in node.pos_keys]
                insert_keyframes_channelbag(
                    channelbag, dp_prefix + "location", 3,
                    node.pos_frames, values, group_name=node.name,
                )

            # ── Scale keyframes ──
            if node.scl_keys:
                if is_pose_bone:
                    values = []
                    for v in node.scl_keys:
                        ps = convert_pose_bone_scale(rest_local, v)
                        values.append(tuple(ps))
                else:
                    values = [tuple(v) for v in node.scl_keys]
                insert_keyframes_channelbag(
                    channelbag, dp_prefix + "scale", 3,
                    node.scl_frames, values, group_name=node.name,
                )

            # ── Note events → custom property ──
            if node.note_keys:
                if "ls3d_note_id" not in id_obj:
                    id_obj["ls3d_note_id"] = 0

                note_cb = ensure_channelbag_for_object(id_obj)
                note_dp = '["ls3d_note_id"]'

                fc = None
                for existing_fc in note_cb.fcurves:
                    if existing_fc.data_path == note_dp and existing_fc.array_index == 0:
                        fc = existing_fc
                        break
                if fc is None:
                    fc = note_cb.fcurves.new(note_dp, index=0, group_name=node.name)

                kf_data = []
                for i, frame in enumerate(node.note_frames):
                    kf_data.append(float(frame))
                    kf_data.append(float(node.note_keys[i]))
                fc.keyframe_points.add(count=len(node.note_frames))
                fc.keyframe_points.foreach_set("co", kf_data)
                for kp in fc.keyframe_points:
                    kp.interpolation = 'CONSTANT'
                fc.update()

        # Set scene frame range
        context.scene.frame_start = 0
        context.scene.frame_end = num_frames

        if skipped_names:
            print(f"[5DS Import] Skipped nodes (no match): {skipped_names}")

        self.report(
            {'INFO'},
            f"5DS import: {matched}/{len(nodes)} nodes matched, "
            f"{num_frames + 1} frames. File: {os.path.basename(self.filepath)}"
        )
        return {'FINISHED'}


# ── Export Operator ───────────────────────────────────────────────────────────

class Export5DS(bpy.types.Operator, ExportHelper):
    """Export LS3D 5DS animation file"""
    bl_idname = "export_scene.5ds"
    bl_label = "Export 5DS"
    filename_ext = ".5ds"
    filter_glob: StringProperty(default="*.5ds", options={"HIDDEN"})

    export_scope: EnumProperty(
        name="Export Scope",
        items=[
            ('SCENE', "Entire Scene", "Export all animated objects in the scene"),
            ('SELECTED', "Selected Only", "Export only selected animated objects"),
        ],
        default='SCENE',
    )

    @staticmethod
    def apply_manual_flags(node, target):
        """When auto-flags is OFF, use the manual per-frame flags instead of
        auto-detected ones.  Clears channels whose flag bit is not set."""
        manual = target.ls3d_5ds_flags
        if not (manual & KEY_ROTATION):
            node.flags &= ~KEY_ROTATION
            node.rot_frames = []
            node.rot_keys = []
        if not (manual & KEY_POSITION):
            node.flags &= ~KEY_POSITION
            node.pos_frames = []
            node.pos_keys = []
        if not (manual & KEY_SCALE):
            node.flags &= ~KEY_SCALE
            node.scl_frames = []
            node.scl_keys = []
        if not (manual & KEY_NOTE):
            node.flags &= ~KEY_NOTE
            node.note_frames = []
            node.note_keys = []

    def get_keyframe_data(self, fcurves, data_path, num_components, frame_start, frame_end):
        """Extract keyframe frames and values from channelbag fcurves."""
        if fcurves is None:
            return [], []

        fcs = []
        for comp in range(num_components):
            fcs.append(find_fcurve(fcurves, data_path, comp))

        if all(fc is None for fc in fcs):
            return [], []

        # Collect all unique frame numbers across all components
        frame_set = set()
        for fc in fcs:
            if fc:
                for kp in fc.keyframe_points:
                    frame = int(round(kp.co[0]))
                    if frame_start <= frame <= frame_end:
                        frame_set.add(frame)

        if not frame_set:
            return [], []

        frames = sorted(frame_set)
        values = []
        for frame in frames:
            val = []
            for fc in fcs:
                if fc:
                    val.append(fc.evaluate(frame))
                else:
                    val.append(0.0)
            values.append(tuple(val))

        return frames, values

    def collect_object_anim(self, obj, frame_start, frame_end):
        """Collect animation data from a regular object. Returns AnimNode or None."""
        fcurves = get_fcurves_for_object(obj)
        if fcurves is None:
            return None

        node = AnimNode()
        node.name = obj.name
        node.flags = 0

        # Rotation (quaternion)
        rot_frames, rot_values = self.get_keyframe_data(
            fcurves, "rotation_quaternion", 4, frame_start, frame_end
        )
        if rot_frames:
            node.flags |= KEY_ROTATION
            node.rot_frames = rot_frames
            node.rot_keys = [blender_to_ls3d_quat(Quaternion(v)) for v in rot_values]

        # Position
        pos_frames, pos_values = self.get_keyframe_data(
            fcurves, "location", 3, frame_start, frame_end
        )
        if pos_frames:
            node.flags |= KEY_POSITION
            node.pos_frames = pos_frames
            node.pos_keys = [blender_to_ls3d_pos(Vector(v)) for v in pos_values]

        # Scale
        scl_frames, scl_values = self.get_keyframe_data(
            fcurves, "scale", 3, frame_start, frame_end
        )
        if scl_frames:
            node.flags |= KEY_SCALE
            node.scl_frames = scl_frames
            node.scl_keys = [blender_to_ls3d_scale(Vector(v)) for v in scl_values]

        # Note events (custom property)
        note_frames, note_values = self.get_keyframe_data(
            fcurves, '["ls3d_note_id"]', 1, frame_start, frame_end
        )
        if note_frames:
            node.flags |= KEY_NOTE
            node.note_frames = note_frames
            node.note_keys = [int(v[0]) for v in note_values]

        if not bpy.context.scene.ls3d_5ds_auto_flags:
            self.apply_manual_flags(node, obj)

        if node.flags == 0:
            return None

        return node

    def collect_bone_anim(self, arm_obj, bone_name, frame_start, frame_end, base_mat=None):
        """Collect animation data from a pose bone. Returns AnimNode or None.
        Converts pose-bone-relative transforms back to absolute local (5DS format)."""
        fcurves = get_fcurves_for_object(arm_obj)
        if fcurves is None:
            return None

        dp_prefix = f'pose.bones["{bone_name}"].'

        # Get bone rest pose for converting back to absolute local
        data_bone = arm_obj.data.bones[bone_name]
        rest_local, rest_rot, rest_pos = get_bone_rest_local(data_bone, base_mat)

        node = AnimNode()
        node.name = bone_name
        node.flags = 0

        # Rotation: convert pose-relative back to absolute local
        rot_frames, rot_values = self.get_keyframe_data(
            fcurves, dp_prefix + "rotation_quaternion", 4, frame_start, frame_end
        )
        if rot_frames:
            node.flags |= KEY_ROTATION
            node.rot_frames = rot_frames
            node.rot_keys = []
            for v in rot_values:
                pose_rot = Quaternion(v)
                abs_rot = rest_rot @ pose_rot  # undo the delta: absolute = rest @ pose
                node.rot_keys.append(blender_to_ls3d_quat(abs_rot))

        # Position: convert pose-relative back to absolute local
        pos_frames, pos_values = self.get_keyframe_data(
            fcurves, dp_prefix + "location", 3, frame_start, frame_end
        )
        if pos_frames:
            node.flags |= KEY_POSITION
            node.pos_frames = pos_frames
            node.pos_keys = []
            for v in pos_values:
                pose_loc = Vector(v)
                abs_pos = rest_pos + (rest_rot @ pose_loc)  # undo the delta
                node.pos_keys.append(blender_to_ls3d_pos(abs_pos))

        # Scale: convert pose-relative back to absolute local
        scl_frames, scl_values = self.get_keyframe_data(
            fcurves, dp_prefix + "scale", 3, frame_start, frame_end
        )
        if scl_frames:
            node.flags |= KEY_SCALE
            node.scl_frames = scl_frames
            rest_scl = rest_local.to_scale()
            node.scl_keys = []
            for v in scl_values:
                abs_scl = Vector((v[0] * rest_scl.x, v[1] * rest_scl.y, v[2] * rest_scl.z))
                node.scl_keys.append(blender_to_ls3d_scale(abs_scl))

        # When auto-flags is off, use manual per-bone flags
        if not bpy.context.scene.ls3d_5ds_auto_flags:
            pb = arm_obj.pose.bones.get(bone_name)
            if pb:
                self.apply_manual_flags(node, pb)

        if node.flags == 0:
            return None

        return node

    def collect_armature_as_base(self, arm_obj, skin_child, frame_start, frame_end):
        """Export an armature's object animation as the skinned mesh 'base' node.
        Reverses the import delta:
          Import: delta_rot = anim_rot @ rest_rot^-1
          Export: anim_rot = delta_rot @ rest_rot
          Import: delta_pos = anim_pos - delta_rot @ rest_pos
          Export: anim_pos = delta_pos + delta_rot @ rest_pos"""
        fcurves = get_fcurves_for_object(arm_obj)
        if fcurves is None:
            return None

        node = AnimNode()
        node.name = skin_child.name if skin_child else arm_obj.name
        node.flags = 0

        # Get the skin rest components for reversing the delta
        if skin_child:
            rest_rot = (skin_child.rotation_quaternion.copy()
                        if skin_child.rotation_mode == 'QUATERNION'
                        else skin_child.rotation_euler.to_quaternion())
            rest_pos = skin_child.location.copy()
        else:
            rest_rot = Quaternion()
            rest_pos = Vector((0, 0, 0))

        # Read rotation data
        rot_frames, rot_values = self.get_keyframe_data(
            fcurves, "rotation_quaternion", 4, frame_start, frame_end
        )
        if rot_frames:
            node.flags |= KEY_ROTATION
            node.rot_frames = rot_frames
            node.rot_keys = []
            for v in rot_values:
                delta_rot = Quaternion(v)
                abs_rot = delta_rot @ rest_rot  # reverse: anim = delta @ rest
                node.rot_keys.append(blender_to_ls3d_quat(abs_rot))

        # Read position data
        pos_frames, pos_values = self.get_keyframe_data(
            fcurves, "location", 3, frame_start, frame_end
        )
        if pos_frames:
            node.flags |= KEY_POSITION
            node.pos_frames = pos_frames
            # Build rotation lookup for position delta reversal
            rot_map = dict(zip(rot_frames, rot_values)) if rot_frames else {}
            node.pos_keys = []
            for i, v in enumerate(pos_values):
                delta_pos = Vector(v)
                # Get the delta_rot at this position's frame
                frame = pos_frames[i]
                if frame in rot_map:
                    dr = Quaternion(rot_map[frame])
                elif rot_frames:
                    # Evaluate from fcurve at this frame
                    dr_vals = []
                    for comp in range(4):
                        fc = find_fcurve(fcurves, "rotation_quaternion", comp)
                        dr_vals.append(fc.evaluate(frame) if fc else (1.0 if comp == 0 else 0.0))
                    dr = Quaternion(dr_vals)
                else:
                    dr = Quaternion()
                abs_pos = delta_pos + dr @ rest_pos  # reverse: anim = delta + delta_rot @ rest
                node.pos_keys.append(blender_to_ls3d_pos(abs_pos))

        # Scale
        scl_frames, scl_values = self.get_keyframe_data(
            fcurves, "scale", 3, frame_start, frame_end
        )
        if scl_frames:
            node.flags |= KEY_SCALE
            node.scl_frames = scl_frames
            node.scl_keys = [blender_to_ls3d_scale(Vector(v)) for v in scl_values]

        # Note events
        note_frames, note_values = self.get_keyframe_data(
            fcurves, '["ls3d_note_id"]', 1, frame_start, frame_end
        )
        if note_frames:
            node.flags |= KEY_NOTE
            node.note_frames = note_frames
            node.note_keys = [int(v[0]) for v in note_values]

        # When auto-flags is off, use manual flags from the armature object
        if not bpy.context.scene.ls3d_5ds_auto_flags:
            self.apply_manual_flags(node, arm_obj)

        if node.flags == 0:
            return None

        return node

    def execute(self, context):
        frame_start = context.scene.frame_start
        frame_end = context.scene.frame_end
        num_frames = frame_end - frame_start

        if self.export_scope == 'SELECTED':
            objects = list(context.selected_objects)
        else:
            objects = list(context.scene.objects)

        export_nodes = []

        for obj in objects:
            if obj.type == 'ARMATURE' and obj.pose:
                # Export armature's own animation as the skinned mesh ("base") node
                # reversing the delta transform applied on import
                skin_child = None
                for child in obj.children:
                    if is_skinned_mesh(child):
                        skin_child = child
                        break

                base_node = self.collect_armature_as_base(obj, skin_child, frame_start, frame_end)
                if base_node:
                    export_nodes.append(base_node)

                # Export pose bone animation
                base_mat = get_base_matrix_for_armature(obj)
                for pb in obj.pose.bones:
                    if pb.get("ls3d_is_base_bone"):
                        continue  # Base bone — not a 4DS joint
                    bone_node = self.collect_bone_anim(obj, pb.name, frame_start, frame_end, base_mat)
                    if bone_node:
                        export_nodes.append(bone_node)
            else:
                # Regular object animation
                node = self.collect_object_anim(obj, frame_start, frame_end)
                if node:
                    export_nodes.append(node)

        if not export_nodes:
            self.report({'WARNING'}, "No animated objects found to export")
            return {'CANCELLED'}

        write_5ds(self.filepath, num_frames, export_nodes)

        self.report(
            {'INFO'},
            f"5DS export: {len(export_nodes)} nodes, {num_frames + 1} frames. "
            f"File: {os.path.basename(self.filepath)}"
        )
        return {'FINISHED'}


# ── N-Panel ───────────────────────────────────────────────────────────────────

class VIEW3D_PT_5ds_animation(bpy.types.Panel):
    bl_label = "5DS Animation"
    bl_idname = "VIEW3D_PT_5ds_animation"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "5ds Animation"

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        if obj is None:
            return False
        if context.active_pose_bone is not None:
            return True
        return hasattr(obj, "ls3d_5ds_flags")

    def draw(self, context):
        layout = self.layout
        scene = context.scene

        # ── Global Auto-Flags Toggle ──────────────────────────────────────
        box = layout.box()
        box.label(text="Export Flags Mode", icon='SETTINGS')
        box.prop(scene, "ls3d_5ds_auto_flags")
        if scene.ls3d_5ds_auto_flags:
            box.label(text="Flags auto-created from animation data", icon='INFO')
        else:
            box.label(text="Using manual per-frame flags below", icon='ERROR')

        # ── Per-Frame Flags ───────────────────────────────────────────────
        obj = context.active_object
        pose_bone = context.active_pose_bone

        if pose_bone is not None:
            target = pose_bone
            label = f"Bone: {pose_bone.name}"
        else:
            target = obj
            label = f"Object: {obj.name}"

        box2 = layout.box()
        box2.label(text=label, icon='ANIM')
        box2.enabled = not scene.ls3d_5ds_auto_flags
        box2.prop(target, "ls3d_5ds_flags", text="Raw Flags")
        grid = box2.grid_flow(columns=2, align=True)
        grid.prop(target, "ls3d_5ds_flag_position", toggle=True)
        grid.prop(target, "ls3d_5ds_flag_rotation", toggle=True)
        grid.prop(target, "ls3d_5ds_flag_scale",    toggle=True)
        grid.prop(target, "ls3d_5ds_flag_note",     toggle=True)


# ── Menu Integration ──────────────────────────────────────────────────────────

def menu_func_import(self, context):
    self.layout.operator(Import5DS.bl_idname, text="5DS Mafia Animation File (.5ds)")

def menu_func_export(self, context):
    self.layout.operator(Export5DS.bl_idname, text="5DS Mafia Animation File (.5ds)")


# ── Registration ──────────────────────────────────────────────────────────────

classes = (
    Import5DS,
    Export5DS,
    VIEW3D_PT_5ds_animation,
)


def _register_5ds_props(owner):
    """Register 5DS flag properties on a Blender type (Object or PoseBone)."""
    owner.ls3d_5ds_flags = IntProperty(name="5DS Flags", default=0)
    owner.ls3d_5ds_flag_position = BoolProperty(
        name="Position",
        description="Export position animation keys",
        get=make_5ds_getter(KEY_POSITION),
        set=make_5ds_setter(KEY_POSITION),
    )
    owner.ls3d_5ds_flag_rotation = BoolProperty(
        name="Rotation",
        description="Export rotation animation keys",
        get=make_5ds_getter(KEY_ROTATION),
        set=make_5ds_setter(KEY_ROTATION),
    )
    owner.ls3d_5ds_flag_scale = BoolProperty(
        name="Scale",
        description="Export scale animation keys",
        get=make_5ds_getter(KEY_SCALE),
        set=make_5ds_setter(KEY_SCALE),
    )
    owner.ls3d_5ds_flag_note = BoolProperty(
        name="Note",
        description="Export note/sound event keys",
        get=make_5ds_getter(KEY_NOTE),
        set=make_5ds_setter(KEY_NOTE),
    )


def _unregister_5ds_props(owner):
    """Remove 5DS flag properties from a Blender type."""
    for attr in ("ls3d_5ds_flags", "ls3d_5ds_flag_position",
                 "ls3d_5ds_flag_rotation", "ls3d_5ds_flag_scale",
                 "ls3d_5ds_flag_note"):
        try:
            delattr(owner, attr)
        except:
            pass


def register():
    for cls in classes:
        bpy.utils.register_class(cls)

    _register_5ds_props(bpy.types.Object)
    _register_5ds_props(bpy.types.PoseBone)

    bpy.types.Scene.ls3d_5ds_auto_flags = BoolProperty(
        name="Auto-Create Flags",
        description="When enabled, export flags are automatically created from animation data. "
                    "When disabled, per-frame manual flags are used",
        default=True,
    )

    try:
        bpy.types.TOPBAR_MT_file_import.remove(menu_func_import)
        bpy.types.TOPBAR_MT_file_export.remove(menu_func_export)
    except:
        pass
    bpy.types.TOPBAR_MT_file_import.append(menu_func_import)
    bpy.types.TOPBAR_MT_file_export.append(menu_func_export)

def unregister():
    try:
        bpy.types.TOPBAR_MT_file_import.remove(menu_func_import)
        bpy.types.TOPBAR_MT_file_export.remove(menu_func_export)
    except:
        pass

    del bpy.types.Scene.ls3d_5ds_auto_flags

    _unregister_5ds_props(bpy.types.PoseBone)
    _unregister_5ds_props(bpy.types.Object)

    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)

if __name__ == "__main__":
    register()
