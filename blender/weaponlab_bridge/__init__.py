"""Weapon Lab Bridge: edit a Weapon Lab weapon .blend and export clips back.

Weapon Lab builds the .blend (rigs, one Action per clip, markers, previews)
and loads this add-on for the session when it opens the file. The add-on
keeps editing simple and hands export to the weaponlab command-line tool,
which verifies every clip it writes.
"""
import json
import os
import subprocess
from pathlib import Path

import blf
import bpy
import gpu
from bpy.props import BoolProperty, EnumProperty, PointerProperty, StringProperty
from bpy.types import Operator, Panel, PropertyGroup
from bpy_extras.io_utils import ExportHelper, ImportHelper
from gpu_extras.batch import batch_for_shader
from mathutils import Matrix

from . import animation_io, pose_io, rig_style

bl_info = {
    "name": "Weapon Lab Bridge",
    "author": "Froddoyo",
    "version": (0, 15, 0),
    "blender": (4, 4, 0),
    "location": "3D Viewport > Sidebar > Weapon Lab",
    "description": "Edit CS2 weapon animations built by Weapon Lab and export them back",
    "category": "Animation",
}

FOLLOW_CONSTRAINT = "Weapon Lab - follow hand"
WORLD_PREFIX = "world_"  # Third-person clip ids (Weapon Lab's RigPackage.WorldPrefix, 0.26).
CLI_VARIABLE = "WEAPONLAB_CLI"
EXPORT_PREFIX = "WEAPONLAB_EXPORT "
GUARD_PREFIX = "WEAPONLAB_GUARD "
RESULT_PREFIX = "WEAPONLAB_RESULT "


# ---- Scene data ----

def meta(scene):
    # "weaponlab" is also the registered PointerProperty for panel settings.
    # Keep serialized project metadata under its own ID-property key.
    raw = scene.get("weaponlab_meta")
    if raw is None:  # Older .blend files used the colliding key.
        raw = scene.get("weaponlab")
    return json.loads(raw) if isinstance(raw, str) and raw else None


_blend_folders = {}


def blend_folder():
    """The sandbox's blender folder: beside the .blend when its input is there (the default place, or a
    sandbox moved with its .blend), else the sandbox the .blend was built from (a .blend saved elsewhere)."""
    if not bpy.data.filepath:
        raise RuntimeError("Save or open the Weapon Lab .blend first.")
    cached = _blend_folders.get(bpy.data.filepath)
    if cached is not None and (cached / "input" / "rig.json").is_file():
        return cached
    here = Path(bpy.data.filepath).resolve().parent
    built = next((data.get("sandbox") for data in map(meta, bpy.data.scenes) if data and data.get("sandbox")), None)
    opened = os.environ.get("WEAPONLAB_SANDBOX")  # Set by Weapon Lab's Open in Blender.
    weapon_id = next((data.get("weaponId") for data in map(meta, bpy.data.scenes) if data), None)
    if opened and Path(opened).name != weapon_id:
        opened = None  # Another weapon's sandbox: the session opened a different .blend first.
    for folder in (here, Path(built) / "blender" if built else None, Path(opened) / "blender" if opened else None):
        if folder is not None and (folder / "input" / "rig.json").is_file():
            _blend_folders[bpy.data.filepath] = folder
            return folder
    raise RuntimeError("Weapon Lab cannot find this weapon's sandbox: no input folder beside the .blend"
                       + (f" or in {built}" if built else "")
                       + ". Open the .blend from Weapon Lab (Open in Blender), or build it again.")


def input_folder():
    return blend_folder() / "input"


def sandbox_folder():
    return blend_folder().parent


_rig_cache = {}


def rig_document():
    path = input_folder() / "rig.json"
    stamp = path.stat().st_mtime
    cached = _rig_cache.get(str(path))
    if cached is None or cached[0] != stamp:
        cached = (stamp, json.loads(path.read_text(encoding="utf-8")))
        _rig_cache[str(path)] = cached
    return cached[1]


def clip_info(clip_id):
    return next(clip for clip in rig_document()["clips"] if clip["id"] == clip_id)


GAME_TIMING_FILE = "game_timing.json"
LENGTHS_FILE = "clip_lengths.json"
_input_cache = {}


def input_json(name):
    """A JSON file Weapon Lab writes into the input folder, re-read when it changes; None when absent."""
    try:
        path = input_folder() / name
    except RuntimeError:
        return None
    if not path.is_file():
        return None
    stamp = path.stat().st_mtime
    cached = _input_cache.get(str(path))
    if cached is None or cached[0] != stamp:
        try:
            cached = (stamp, json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            return None
        _input_cache[str(path)] = cached
    return cached[1]


def game_timing():
    """Weapon Lab's game_timing.json: the gun the weapon plays like in game and
    that gun's marks per clip (the ammo counter follows them), or None."""
    return input_json(GAME_TIMING_FILE)


def clip_timing(clip_id):
    document = game_timing() if clip_id else None
    return document.get("clips", {}).get(clip_id) if document else None


def is_world_clip(scene, clip_id):
    """A third-person clip (0.26): its Action plays on the Body rig and the third-person weapon rig."""
    action = clip_action(scene, clip_id) if clip_id else None
    return action is not None and pose_io.is_world(action)


def rigs(scene, clip_id=None):
    """The (primary, weapon) rigs of a clip, the current one by default: Arms and Weapon, or for a third-person clip
    Body and Weapon (third person)."""
    world = is_world_clip(scene, clip_id or current_clip(scene))
    primary, weapon = pose_io.scene_rigs(scene, meta(scene), world)
    if primary is None:
        raise RuntimeError(("The body rig" if world else "The arms rig") + " is missing from the scene.")
    return primary, weapon


def first_person_rigs(scene):
    return pose_io.scene_rigs(scene, meta(scene), False)


def playing(rig):
    """The Weapon Lab clip a rig plays, or None."""
    action = rig.animation_data.action if rig is not None and rig.animation_data else None
    return action.get("weaponlab_clip") if action else None


def current_clip(scene):
    """The clip being edited: the one last picked (scene "weaponlab_current", 0.26) while a rig still plays it, else
    the clip the Arms rig plays (older .blend files), else the Body rig's."""
    data = meta(scene)
    playing_now = [playing(pose_io.scene_rigs(scene, data, world)[0]) for world in (False, True)]
    picked = scene.get("weaponlab_current")
    if picked and picked in playing_now:
        return picked
    return next((clip_id for clip_id in playing_now if clip_id), None)


def clip_action(scene, clip_id):
    return bpy.data.actions.get(meta(scene)["actions"].get(clip_id, ""))


def show_view(context, world):
    """Shows the clip's view (0.26): third-person clips hide the arms and show the body, and back. The 3D views
    move to the shown rigs, and posing carries on on the shown view's rig of the same kind."""
    scene = context.scene
    was_world = bool(scene.get("weaponlab_world_view", False))
    pose_io.show_view(context.view_layer, world)
    scene["weaponlab_world_view"] = world
    if was_world == world:
        return
    location, distance = pose_io.VIEW_FRAMING[world]
    for window in context.window_manager.windows:
        for area in window.screen.areas:
            if area.type == "VIEW_3D":
                region = area.spaces.active.region_3d
                region.view_location = location
                region.view_distance = distance
    active = context.view_layer.objects.active
    if context.mode == "POSE" and active is not None:
        old_primary, old_weapon = pose_io.scene_rigs(scene, meta(scene), was_world)
        new_primary, new_weapon = pose_io.scene_rigs(scene, meta(scene), world)
        rig = new_weapon if active == old_weapon and new_weapon else new_primary
        if rig is not None and rig != active:
            try:
                enter_pose_mode(context, rig)
            except RuntimeError:
                pass  # Called where modes cannot change (a redraw): the user picks the rig.


def clip_frames(clip_id):
    """The clip's length: the one set in Weapon Lab (clip_lengths.json), else the game's."""
    lengths = input_json(LENGTHS_FILE) or {}
    return int(lengths.get("clips", {}).get(clip_id) or clip_info(clip_id)["frames"])


def clip_at_length(clip_id):
    """The clip's frames and duration at its length (same frame rate), for the scene and sampling."""
    info = clip_info(clip_id)
    frames = clip_frames(clip_id)
    if frames == info["frames"] or info["frames"] < 2:
        return info
    return dict(info, frames=frames, duration=info["duration"] * (frames - 1) / (info["frames"] - 1))


def switch_clip(scene, clip_id):
    data = meta(scene)
    action = bpy.data.actions.get(data["actions"].get(clip_id, ""))
    if action is None:
        raise RuntimeError("No Action for clip " + clip_id)
    arms, weapon = rigs(scene, clip_id)
    pose_io.assign(arms, action, pose_io.slot_for(action, "weaponlab_arms_slot"))
    if weapon and "weaponlab_weapon_slot" in action:
        pose_io.assign(weapon, action, pose_io.slot_for(action, "weaponlab_weapon_slot"))
    scene["weaponlab_current"] = clip_id
    if bpy.context.scene == scene:
        show_view(bpy.context, pose_io.is_world(action))
    glue_hands(scene)
    pose_io.apply_timing(scene, clip_at_length(clip_id))
    scene.frame_set(0)


# Blender keeps only references to enum strings, so the list must outlive the callback.
_clip_items = []


def clip_items(settings, context):
    _clip_items.clear()
    data = meta(context.scene) if context and context.scene else None
    if data:
        for clip_id in data["actions"]:
            # Named like "Exported as" names it (user 2026-11-14): third-person clips by their world_ id.
            hint = "Edit the third-person clip on the body rig" if clip_id.startswith(WORLD_PREFIX) else "Edit " + clip_id
            _clip_items.append((clip_id, clip_id, hint))
    if not _clip_items:
        _clip_items.append(("NONE", "No clips", ""))
    return _clip_items


def on_clip_changed(settings, context):
    if settings.clip != "NONE" and current_clip(context.scene) != settings.clip:
        switch_clip(context.scene, settings.clip)


def glue_hands(scene):
    """wpnHand_L/R always follow hand_L/R while posing (the game pulls the arms onto them).
    Export glues them too, keeping CS2's own tiny offset, so this is only for the view.
    Older .blend files built the constraints muted; this switches them on. The body does the same in third person."""
    for rig in (pose_io.scene_rigs(scene, meta(scene), world)[0] for world in (False, True)):
        for side in ("L", "R"):
            bone = rig.pose.bones.get("wpnHand_" + side) if rig else None
            constraint = bone.constraints.get(FOLLOW_CONSTRAINT) if bone else None
            if constraint and constraint.mute:
                constraint.mute = False


# "Exported as" (0.25, like CS2 Animation Lab): the clip an Action's animation exports into, saved on the Action.
_export_items = []


def export_items(settings, context):
    _export_items.clear()
    data = meta(context.scene) if context and context.scene else None
    if data:
        for clip_id in data["actions"]:
            _export_items.append((clip_id, clip_id, "Export this animation into " + clip_id))
    if not _export_items:
        _export_items.append(("NONE", "No clips", ""))
    return _export_items


def get_export_as(settings):
    scene = settings.id_data
    data = meta(scene)
    if not data:
        return 0
    clips = list(data["actions"])
    target = export_target(scene)
    return clips.index(target) if target in clips else 0


def set_export_as(settings, value):
    scene = settings.id_data
    data = meta(scene)
    clips = list(data["actions"]) if data else []
    source = current_clip(scene)
    action = clip_action(scene, source) if source else None
    if action is None or not 0 <= value < len(clips):
        return
    if clips[value] == source:
        if "weaponlab_export_as" in action:
            del action["weaponlab_export_as"]
    else:
        action["weaponlab_export_as"] = clips[value]



# ---- Local pose propagation (experimental prototype) ----

class WeaponLabSettings(PropertyGroup):
    clip: EnumProperty(name="Clip used", description="The clip you animate here; switches both rigs", items=clip_items, update=on_clip_changed)
    export_as: EnumProperty(name="Exported as", items=export_items, get=get_export_as, set=set_export_as,
                            description="The clip this animation exports into (default: itself). Pick another to overwrite it, e.g. a legacy clip you do not need; saved in the .blend")
    last_result: StringProperty(name="Last export", default="")


# ---- Bone presets (from CS2 Animation Lab) ----

PRESETS = [
    ("WRIST_L", "Left Wrist", "hand_L"),
    ("WRIST_R", "Right Wrist", "hand_R"),
    ("HAND_L", "Left Hand", "wrist and fingers"),
    ("HAND_R", "Right Hand", "wrist and fingers"),
    ("FINGERS_L", "Left Fingers", "finger joints only"),
    ("FINGERS_R", "Right Fingers", "finger joints only"),
    ("ARM_L", "Left Arm", "shoulder, elbow, wrist, fingers"),
    ("ARM_R", "Right Arm", "shoulder, elbow, wrist, fingers"),
    ("ELBOW_L", "Left Elbow", "forearm rotation"),
    ("ELBOW_R", "Right Elbow", "forearm rotation"),
    ("HAND_WPN", "Right Hand + WPN", "turn the gun around the right wrist"),
    ("ELBOW_WPN", "Right Elbow + WPN", "swing the gun with the right forearm, around the elbow"),
    ("ARM_WPN", "Right Arm + WPN", "swing the gun with the whole right arm, around the shoulder"),
    ("GUN", "Gun Motion", "wpn carries the weapon rig"),
    ("MAGAZINE", "Magazine", "weapon clip bone"),
    ("BOLT", "Bolt", "weapon bolt bone"),
]
WEAPON_PRESETS = {"MAGAZINE": "clip", "BOLT": "bolt"}


def preset_bones(rig, preset):
    """Bone names and the active bone for a preset."""
    if preset == "GUN":
        return ["wpn"], "wpn"
    if preset == "HAND_WPN":
        return ["hand_R", "wpn"], "hand_R"
    if preset == "ELBOW_WPN":
        fingers = [bone.name for bone in rig.pose.bones if bone.name.startswith("finger_") and bone.name.endswith("_R")]
        return ["arm_lower_R", "hand_R", *fingers, "wpn"], "arm_lower_R"
    if preset == "ARM_WPN":
        fingers = [bone.name for bone in rig.pose.bones if bone.name.startswith("finger_") and bone.name.endswith("_R")]
        upper = upper_arm(rig, "R")
        return [upper, "arm_lower_R", "hand_R", *fingers, "wpn"], upper
    if preset in WEAPON_PRESETS:
        name = WEAPON_PRESETS[preset]
        return [name], name
    kind, side = preset.rsplit("_", 1)
    fingers = [bone.name for bone in rig.pose.bones if bone.name.startswith("finger_") and bone.name.endswith("_" + side)]
    names = {
        "WRIST": ["hand_" + side],
        "HAND": ["hand_" + side] + fingers,
        "FINGERS": fingers,
        "ARM": [upper_arm(rig, side), "arm_lower_" + side, "hand_" + side] + fingers,
        "ELBOW": ["arm_lower_" + side],
    }[kind]
    active = {"FINGERS": "finger_index_0_" + side, "ARM": upper_arm(rig, side), "ELBOW": "arm_lower_" + side}.get(kind, "hand_" + side)
    return names, active


def upper_arm(rig, side):
    """The upper arm the arm presets turn: armUpperShoulder_* on the viewmodel arms, arm_upper_* on the third-person body."""
    name = "armUpperShoulder_" + side
    return name if name in rig.pose.bones else "arm_upper_" + side


def set_selected(pose_bone, selected):
    # Blender 5 keeps pose selection on the pose bone; 4.x kept it on the bone.
    if hasattr(pose_bone, "select"):
        pose_bone.select = selected
    else:
        pose_bone.bone.select = selected


def enter_pose_mode(context, rig):
    if context.mode != "OBJECT":
        bpy.ops.object.mode_set(mode="OBJECT")
    for obj in context.view_layer.objects:
        obj.select_set(False)
    rig.hide_set(False)
    rig.select_set(True)
    context.view_layer.objects.active = rig
    bpy.ops.object.mode_set(mode="POSE")


# ---- Operators ----

class WeaponLabOperator:
    @classmethod
    def poll(cls, context):
        return meta(context.scene) is not None

    def fail(self, error):
        self.report({"ERROR"}, str(error))
        return {"CANCELLED"}


def enable_auto_key(scene):
    """Every pose change is keyed on the current frame; every frame already has keys, so posing never smears motion."""
    tools = scene.tool_settings
    tools.use_keyframe_insert_auto = True
    tools.auto_keying_mode = "REPLACE_KEYS"
    tools.use_keyframe_insert_keyingset = False


class WEAPONLAB_OT_edit_rig(WeaponLabOperator, Operator):
    bl_idname = "weaponlab.edit_rig"
    bl_label = "Edit Rig"
    bl_description = "Pose this rig on its own, with Auto Key Pose Edits on"
    bl_options = {"REGISTER", "UNDO"}

    rig: EnumProperty(items=[("ARMS", "Edit Arms", "Pose the arms rig (the body rig for a third-person clip)"),
                             ("WEAPON", "Edit Weapon", "Pose the weapon rig (magazine, bolt, props, …)")])

    def execute(self, context):
        try:
            arms, weapon = rigs(context.scene)
            rig = arms if self.rig == "ARMS" else weapon
            if rig is None:
                return self.fail("This weapon has no weapon rig.")
            enable_auto_key(context.scene)
            enter_pose_mode(context, rig)
            primary = "Body" if is_world_clip(context.scene, current_clip(context.scene)) else "Arms"
            self.report({"INFO"}, (primary if self.rig == "ARMS" else "Weapon") + " ready: Auto Key Pose Edits is on.")
            return {"FINISHED"}
        except RuntimeError as error:
            return self.fail(error)


class WEAPONLAB_OT_select_preset(WeaponLabOperator, Operator):
    bl_idname = "weaponlab.select_preset"
    bl_label = "Select Bones"
    bl_description = "Select a useful bone group without changing any pose"
    bl_options = {"REGISTER", "UNDO"}

    preset: EnumProperty(items=[(key, label, hint) for key, label, hint in PRESETS])

    def execute(self, context):
        try:
            arms, weapon = rigs(context.scene)
            rig = weapon if self.preset in WEAPON_PRESETS else arms
            if rig is None:
                return self.fail("This clip has no weapon rig.")
            names, active = preset_bones(rig, self.preset)
            names = [name for name in names if name in rig.pose.bones]
            if not names:
                return self.fail("This rig has no bones for " + self.preset.replace("_", " ").title())
            enter_pose_mode(context, rig)
            for bone in rig.pose.bones:
                set_selected(bone, bone.name in names)
            rig.data.bones.active = rig.data.bones[active if active in names else names[0]]
            # Rotate around the active bone: Right Hand + WPN turns the gun around the wrist, Right Arm + WPN around the shoulder.
            context.scene.tool_settings.transform_pivot_point = "ACTIVE_ELEMENT"
            return {"FINISHED"}
        except RuntimeError as error:
            return self.fail(error)


class WEAPONLAB_OT_reset_clip(WeaponLabOperator, Operator):
    bl_idname = "weaponlab.reset_clip"
    bl_label = "Reset Clip to Stock"
    bl_description = "Throw away all edits on the current clip and restore the game's original keys"
    bl_options = {"REGISTER", "UNDO"}

    def invoke(self, context, event):
        return context.window_manager.invoke_confirm(self, event)

    def execute(self, context):
        try:
            scene = context.scene
            clip_id = current_clip(scene)
            if not clip_id:
                return self.fail("No clip is active.")
            if context.mode != "OBJECT":
                bpy.ops.object.mode_set(mode="OBJECT")
            document = rig_document()
            clip = clip_info(clip_id)
            poses = pose_io.read_poses(input_folder() / clip["poses"])
            base = pose_io.read_poses(input_folder() / clip_info(document["defaultClip"])["poses"]) if clip["additive"] else None
            action = bpy.data.actions[meta(scene)["actions"][clip_id]]
            strip = action.layers[0].strips[0]
            arms, weapon = rigs(scene)

            # A third-person clip (0.26): the body's skeletons, no props.
            arms_skeleton, weapon_skeleton, primary = pose_io.skeletons_of(document, pose_io.is_world(clip))
            free = pose_io.free_bones(weapon_skeleton) if weapon else []
            if any(name in arms.data.bones for name in free):
                return self.fail("This .blend keeps its props in the Arms rig (Weapon Lab 0.22.3). "
                                 "Build .blend again in Weapon Lab, then reset.")
            # Props hold still (pose_io.PROP_ANCHOR); a .blend built before 0.22.3 keys them like any bone.
            props = free if free and pose_io.PROP_ANCHOR in weapon.data.bones else []

            def rekey(rig, slot_key, stream, skeleton, primary, skip=()):
                bag = strip.channelbag(pose_io.slot_for(action, slot_key), ensure=True)
                pose_io.clear_channelbag(bag)
                frames = pose_io.frame_locals(poses[stream], skeleton, primary, base[stream] if base else None)
                pose_io.key_channels(bag, pose_io.channel_values(rig, skeleton, frames, skip))
                return bag, frames

            _, arms_frames = rekey(arms, "weaponlab_arms_slot", "arms", arms_skeleton, primary)
            held = False
            if weapon and "weaponlab_weapon_slot" in action and "weapon" in poses:
                weapon_bag, _ = rekey(weapon, "weaponlab_weapon_slot", "weapon", weapon_skeleton, False, skip=props)
                if props:
                    default_id = document["defaultClip"]
                    default_poses = poses if clip_id == default_id else pose_io.read_poses(input_folder() / clip_info(default_id)["poses"])
                    hold = pose_io.prop_hold(default_poses, arms_skeleton, weapon_skeleton, props)
                    pose_io.key_channels(weapon_bag, pose_io.prop_values(weapon, arms_skeleton, arms_frames, pose_io.held(hold, len(arms_frames))))
                    held = True
            scene.frame_set(scene.frame_current)
            self.report({"INFO"}, clip_id + " restored to the game's original motion" + (" (props hold still)." if held else "."))
            return {"FINISHED"}
        except (RuntimeError, OSError, KeyError) as error:
            return self.fail(error)


def cli_path():
    path = os.environ.get(CLI_VARIABLE, "")
    if not path or not Path(path).is_file():
        raise RuntimeError("Open this .blend from Weapon Lab (Open in Blender) to export.")
    return path


def export_target(scene, clip_id=None):
    """Where a clip's animation exports to (0.25 "Exported as"): the clip itself, or another clip of the weapon
    picked in the panel (saved on the Action, so it stays in the .blend)."""
    clip_id = clip_id or current_clip(scene)
    action = clip_action(scene, clip_id) if clip_id else None
    target = action.get("weaponlab_export_as") if action else None
    return target if target and target in meta(scene)["actions"] else clip_id


def check_target(source, target):
    """Refuses an export into a clip that cannot take this clip's animation."""
    if source == target:
        return
    a, b = clip_info(source), clip_info(target)
    if pose_io.is_world(a) != pose_io.is_world(b):
        raise RuntimeError("%s and %s differ: one is third person (the body rig), so it cannot export into the other." % (source, target))
    if a["additive"] != b["additive"]:
        raise RuntimeError("%s and %s differ: one is additive (adds onto another pose), so it cannot export into the other." % (source, target))
    if a["hasWeapon"] != b["hasWeapon"]:
        raise RuntimeError("%s and %s differ: only one of them moves the weapon, so it cannot export into the other." % (source, target))


def sample_current_clip(scene, keep=True, target=None):
    """Samples the active clip from both rigs into the sandbox's edits folder, named for the clip it exports into.

    keep=False (Save Clip As) writes a scratch sample instead: the kept one
    must stay the sandbox export's, because Weapon Lab's Anim Events tab
    re-exports the clip from it."""
    clip_id = current_clip(scene)
    if not clip_id:
        raise RuntimeError("No clip is active.")
    document = rig_document()
    clip = clip_at_length(clip_id)
    arms, weapon = rigs(scene)
    show_view(bpy.context, pose_io.is_world(clip))  # Only a shown rig is sure to be evaluated.
    arms_skeleton, weapon_skeleton, primary = pose_io.skeletons_of(document, pose_io.is_world(clip))
    # Animation only: events (sounds, gameplay, timing) belong to Weapon Lab's Anim Events tab.
    sample = {"arms": pose_io.sample(scene, arms, arms_skeleton, primary, clip["frames"])}
    if weapon and weapon_skeleton and clip["hasWeapon"]:
        sample["weapon"] = pose_io.sample(scene, weapon, weapon_skeleton, False, clip["frames"])
    # A kept sample is written beside its final name and only takes it once the export succeeded (0.24): a refused
    # export must leave the sample that matches the sandbox's export, since Anim Events re-exports from it.
    name = target or clip_id
    path = sandbox_folder() / "edits" / (name + (".json.gz.new" if keep else ".save-as.json.gz"))
    pose_io.write_poses(path, sample)
    return clip_id, path


def unkeyed_bones(scene):
    """Bones whose current pose differs from their keys on this frame.

    Export steps through every frame, which would silently discard such
    changes, so they must be keyed first.
    """
    changed = []
    frame = scene.frame_current
    for rig in [rig for rig in rigs(scene) if rig and rig.animation_data and rig.animation_data.action]:
        action = rig.animation_data.action
        bag = action.layers[0].strips[0].channelbag(rig.animation_data.action_slot)
        if bag is None:
            continue
        for curve in bag.fcurves:
            if not curve.data_path.startswith("pose.bones["):
                continue
            name = curve.data_path.split('"')[1]
            prop = curve.data_path.rsplit(".", 1)[1]
            bone = rig.pose.bones.get(name)
            if bone is None or not hasattr(bone, prop):
                continue
            value = getattr(bone, prop)[curve.array_index]
            if abs(value - curve.evaluate(frame)) > 1e-4 and name not in changed:
                changed.append(name)
    return changed


class ExportGuarded(RuntimeError):
    """Weapon Lab refused an export that would replace an edited export with an unedited one (0.24)."""


def run_export(scene, out_path=None, force=False):
    if bpy.context.mode != "OBJECT":
        bpy.ops.object.mode_set(mode="OBJECT")
    unkeyed = unkeyed_bones(scene)
    if unkeyed:
        raise RuntimeError(
            "Unkeyed changes on frame %d (%s). Press I to key them, or turn on Auto Key Pose Edits before posing."
            % (scene.frame_current, ", ".join(unkeyed[:4]) + (" ..." if len(unkeyed) > 4 else "")))
    source = current_clip(scene)
    target = source if out_path else export_target(scene, source)
    check_target(source, target)
    clip_id, sample = sample_current_clip(scene, keep=not out_path, target=target)
    command = [cli_path(), "export-clip", str(sandbox_folder()), target, str(sample)]
    if target != source:
        command += ["--from", source]
    if out_path:
        command += ["--out", str(out_path)]
    if force:
        command += ["--force", "true"]
    options = {"capture_output": True, "text": True, "encoding": "utf-8", "errors": "replace", "timeout": 180}
    if os.name == "nt":
        options["creationflags"] = subprocess.CREATE_NO_WINDOW
    result = subprocess.run(command, **options)
    kept = sample.name.endswith(".new")
    if result.returncode != 0:
        if kept:
            sample.unlink(missing_ok=True)
        guard = next((line for line in result.stdout.splitlines() if line.startswith(GUARD_PREFIX)), None)
        if guard:
            raise ExportGuarded(guard[len(GUARD_PREFIX):])
        raise RuntimeError((result.stderr or result.stdout or "Export failed.").strip().splitlines()[-1])
    if kept:
        final = sample.with_name(sample.name[:-len(".new")])
        sample.replace(final)
        # The .blend's own sample: neither the source nor the clip it went into has a "newer version" to load.
        for clip_id in {source, target}:
            action = clip_action(scene, clip_id)
            if action is not None:
                action["weaponlab_synced"] = final.stat().st_mtime
    line = next((line for line in result.stdout.splitlines() if line.startswith(EXPORT_PREFIX)), None)
    if line is None:
        raise RuntimeError("Weapon Lab did not report a result.")
    return json.loads(line[len(EXPORT_PREFIX):])


def describe(report):
    if report["identicalToTemplate"]:
        return report["clipId"] + ": no edits; the original clip was written unchanged."
    bones = sum(len(names) for names in report["changedBones"].values())
    events = len(report.get("changedEvents") or [])
    return "%s: %d bone(s), %d event change(s); max error %.4f units / %.4f deg" % (
        report["clipId"], bones, events, report["maxTranslationError"], report["maxRotationErrorDegrees"])


class WEAPONLAB_OT_export(WeaponLabOperator, Operator):
    bl_idname = "weaponlab.export"
    bl_label = "Export to Weapon Lab"
    bl_description = "Write the edited clip into this weapon's Weapon Lab sandbox; only edited bones are re-encoded and the result is verified"

    force: bpy.props.BoolProperty(default=False, options={"HIDDEN", "SKIP_SAVE"})

    def execute(self, context):
        try:
            report = run_export(context.scene, force=self.force)
            context.scene.weaponlab.last_result = describe(report)
            self.report({"INFO"}, describe(report))
            return {"FINISHED"}
        except ExportGuarded as guard:
            context.scene.weaponlab.last_result = str(guard)
            offer_replace(context, str(guard))
            self.report({"WARNING"}, str(guard))
            return {"CANCELLED"}
        except (RuntimeError, OSError, subprocess.TimeoutExpired) as error:
            return self.fail(error)


def offer_replace(context, message):
    """A popup for a refused export (0.24): what it would replace, and a button that replaces it anyway."""
    def draw(menu, _context):
        for line in wrap(message, 60):
            menu.layout.label(text=line)
        menu.layout.separator()
        menu.layout.operator("weaponlab.export", text="Replace the export anyway", icon="ERROR").force = True
    context.window_manager.popup_menu(draw, title="Export would replace your animation", icon="ERROR")


class WEAPONLAB_OT_save_clip_as(WeaponLabOperator, Operator, ExportHelper):
    bl_idname = "weaponlab.save_clip_as"
    bl_label = "Save Clip As"
    bl_description = "Save the edited clip as a compiled .vnmclip_c file anywhere"
    filename_ext = ".vnmclip_c"
    filter_glob: StringProperty(default="*.vnmclip_c", options={"HIDDEN"})

    def invoke(self, context, event):
        clip_id = current_clip(context.scene) or "clip"
        self.filepath = clip_id + ".vnmclip_c"
        return ExportHelper.invoke(self, context, event)

    def execute(self, context):
        try:
            report = run_export(context.scene, self.filepath)
            context.scene.weaponlab.last_result = describe(report) + " Saved to " + report["extraOutput"]
            self.report({"INFO"}, "Saved " + report["extraOutput"])
            return {"FINISHED"}
        except (RuntimeError, OSError, subprocess.TimeoutExpired) as error:
            return self.fail(error)


class WEAPONLAB_OT_export_all(WeaponLabOperator, Operator):
    bl_idname = "weaponlab.export_all"
    bl_label = "Export All Clips"
    bl_description = "Export every clip of this weapon to Weapon Lab; unedited clips are written unchanged (a clip whose sandbox export has edits this .blend lacks is skipped)"

    def execute(self, context):
        scene = context.scene
        start_clip = current_clip(scene)
        edited, failed, kept, passed_over = [], [], [], []
        plan = [(clip_id, export_target(scene, clip_id)) for clip_id in meta(scene)["actions"]]
        claimed_elsewhere = {target for clip_id, target in plan if target != clip_id}
        taken = set()
        try:
            for clip_id, target in plan:
                if (clip_id == target and clip_id in claimed_elsewhere) or target in taken:
                    passed_over.append(clip_id)
                    continue
                taken.add(target)
                switch_clip(scene, clip_id)
                try:
                    report = run_export(scene)
                    if not report["identicalToTemplate"]:
                        edited.append(clip_id)
                except ExportGuarded:
                    kept.append(clip_id)
                except (RuntimeError, OSError, subprocess.TimeoutExpired) as error:
                    failed.append(clip_id + ": " + str(error))
        finally:
            if start_clip:
                switch_clip(scene, start_clip)
                scene.weaponlab.clip = start_clip
        summary = "Exported %d clips; edited: %s." % (len(meta(scene)["actions"]) - len(failed) - len(kept) - len(passed_over), ", ".join(edited) or "none")
        if passed_over:
            summary += " Not exported (another clip exports into them): " + ", ".join(passed_over) + "."
        if kept:
            summary += (" Skipped (their sandbox export has edits this .blend lacks; export one alone to replace it): "
                        + ", ".join(kept) + ".")
        if failed:
            summary += " Failed: " + "; ".join(failed)
        scene.weaponlab.last_result = summary
        self.report({"WARNING"} if failed or kept else {"INFO"}, summary)
        return {"FINISHED"}


def run_cli(arguments, timeout):
    """Runs Weapon Lab's command line (the one this .blend was opened with) and returns its WEAPONLAB_RESULT."""
    options = {"capture_output": True, "text": True, "encoding": "utf-8", "errors": "replace", "timeout": timeout}
    if os.name == "nt":
        options["creationflags"] = subprocess.CREATE_NO_WINDOW
    result = subprocess.run([cli_path()] + arguments, **options)
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout or "Weapon Lab failed.").strip().splitlines()[-1])
    line = next((line for line in result.stdout.splitlines() if line.startswith(RESULT_PREFIX)), None)
    if line is None:
        raise RuntimeError("Weapon Lab did not report a result.")
    return json.loads(line[len(RESULT_PREFIX):])


def replace_preview_fbx(scene, path):
    """An FBX as the weapon's preview mesh (like CS2 Animation Lab's Replace Preview FBX): its meshes are skinned
    straight to the Weapon rig, so they move as you animate. Blender only: Weapon Lab's viewport shows compiled
    models. Every bone the meshes are weighted to must be in the Weapon rig."""
    _, weapon = first_person_rigs(scene)  # The first-person gun; its third-person rig keeps its preview.
    if weapon is None:
        raise RuntimeError("This weapon has no Weapon rig to skin a preview to.")
    if bpy.context.mode != "OBJECT":
        bpy.ops.object.mode_set(mode="OBJECT")
    before = set(bpy.data.objects)
    try:
        bpy.ops.import_scene.fbx(filepath=str(path), use_anim=False, automatic_bone_orientation=False)
        imported = set(bpy.data.objects) - before
        source = next((obj for obj in imported if obj.type == "ARMATURE"), None)
        meshes = [obj for obj in imported if obj.type == "MESH"]
        if source is None or not meshes:
            raise RuntimeError("Pick a skinned weapon FBX (an armature with meshes).")
        weighted = set()
        for mesh in meshes:
            groups = {group.index: group.name for group in mesh.vertex_groups}
            weighted.update(groups[weight.group] for vertex in mesh.data.vertices for weight in vertex.groups if weight.weight > 1e-5)
        missing = weighted - set(weapon.data.bones.keys())
        if missing:
            raise RuntimeError("The FBX is weighted to bones the Weapon rig lacks: " + ", ".join(sorted(missing))
                               + ". Compile it in ModelDoc and pick the .vmdl_c with 'Use its bones as the skeleton'.")
        source_root = source.data.bones.get("weapon") or source.data.bones[0]
        target_root = weapon.data.bones.get("weapon") or weapon.data.bones[0]
        conversion = target_root.matrix_local @ source_root.matrix_local.inverted()
        material = next((slot.material for obj in scene.objects if obj.get("weaponlab_role") == "preview_mesh"
                         and obj.name.startswith("Mesh - weapon") for slot in obj.material_slots if slot.material), None)
        for number, mesh in enumerate(meshes, start=1):
            local = source.matrix_world.inverted() @ mesh.matrix_world
            mesh.data.transform(conversion @ local)
            mesh.parent = weapon
            mesh.matrix_parent_inverse = Matrix.Identity(4)
            mesh.matrix_basis = Matrix.Identity(4)
            for modifier in mesh.modifiers:
                if modifier.type == "ARMATURE":
                    modifier.object = weapon
            mesh.name = "Mesh - weapon (FBX)" + ("" if len(meshes) == 1 else " " + str(number))
            mesh["weaponlab_role"] = "preview_mesh"
            mesh.hide_select = True
            if material is not None:
                mesh.data.materials.clear()
                mesh.data.materials.append(material)
            for collection in list(mesh.users_collection):
                collection.objects.unlink(mesh)
            for collection in weapon.users_collection:
                collection.objects.link(mesh)
        # Only now that the new one is bound: the old weapon preview (meshes and their hidden preview rig) goes.
        old = [obj for obj in scene.objects if obj not in meshes and (obj.name.startswith("Mesh - weapon") or obj.name == "Preview weapon")]
        for obj in old:
            bpy.data.objects.remove(obj, do_unlink=True)
        for obj in imported:
            if obj not in meshes:
                bpy.data.objects.remove(obj, do_unlink=True)
    except Exception:
        for obj in set(bpy.data.objects) - before:
            bpy.data.objects.remove(obj, do_unlink=True)
        raise


def open_rebuilt(path):
    """Opens the .blend Weapon Lab just rebuilt (from a timer: the operator that asked for it has finished)."""
    def load():
        bpy.ops.wm.open_mainfile(filepath=path)
        return None
    return load


class WEAPONLAB_OT_change_model(WeaponLabOperator, Operator):
    bl_idname = "weaponlab.change_model"
    bl_label = "Use this model"
    bl_description = ("Change the gun model. A compiled .vmdl_c (from CS2 or your addon) can also become the skeleton: "
                      "Weapon Lab rebuilds this .blend with its bones and carries your keys over. An .fbx is a Blender-only preview")

    filepath: StringProperty(subtype="FILE_PATH", options={"SKIP_SAVE"})
    filter_glob: StringProperty(default="*.vmdl_c;*.fbx", options={"HIDDEN"})
    use_skeleton: BoolProperty(
        name="Use its bones as the skeleton",
        description="A .vmdl_c only: its bones become the weapon's custom skeleton (bones added in ModelDoc come in, your keys are kept)",
        default=True)

    def invoke(self, context, _event):
        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}

    def draw(self, _context):
        layout = self.layout
        layout.prop(self, "use_skeleton")
        for line in wrap("A .vmdl_c must be in CS2 or your target addon (compile it in ModelDoc). Weapon Lab saves "
                         "this file, rebuilds it with the new model and keeps your keys (a backup goes to "
                         "blender\\backups). An .fbx only swaps the preview mesh here.", 36):
            layout.label(text=line)

    def execute(self, context):
        path = Path(self.filepath)
        try:
            if path.suffix.lower() == ".fbx":
                replace_preview_fbx(context.scene, path)
                self.report({"INFO"}, "Preview mesh: " + path.name + " (Blender only; the skeleton is unchanged).")
                return {"FINISHED"}
            if not path.name.lower().endswith(".vmdl_c"):
                raise RuntimeError("Pick a compiled model (.vmdl_c) or an .fbx.")
            if bpy.data.is_dirty:
                bpy.ops.wm.save_mainfile()  # The rebuild carries the keys saved in the file.
            folder = sandbox_folder()  # <workspace>\profiles\<profile>\sandboxes\<weapon>
            arguments = ["blender", "set-model", meta(context.scene)["weaponId"],
                         "--workspace", str(folder.parent.parent.parent.parent), "--profile", folder.parent.parent.name,
                         "--model", str(path), "--skeleton", "true" if self.use_skeleton else "false"]
            context.window.cursor_set("WAIT")
            try:
                result = run_cli(arguments, timeout=1800)
            finally:
                context.window.cursor_set("DEFAULT")
        except (RuntimeError, OSError, subprocess.TimeoutExpired) as error:
            return self.fail(error)
        message = "Model " + result["model"].split(":")[-1].split("/")[-1]
        if result.get("skeleton"):
            message += "; skeleton " + result["skeleton"]
        message += "; keys carried for %d bone(s) in %d clip(s)" % (result["carriedBones"], result["carriedClips"])
        if result.get("notCarried"):
            message += "; kept the clips' motion on " + ", ".join(result["notCarried"])
        self.report({"INFO"}, message + ". Reopening the rebuilt .blend.")
        bpy.app.timers.register(open_rebuilt(result["blend"]), first_interval=0.2)
        return {"FINISHED"}


def draw_model(layout):
    """The gun model and the skeleton its bones make, with Change gun model (0.24)."""
    box = layout.box()
    box.label(text="Gun model and skeleton", icon="MESH_DATA")
    try:
        weapon = rig_document()["skeletons"].get("weapon")
    except (OSError, ValueError, RuntimeError):
        weapon = None
    if weapon:
        box.label(text="Skeleton: " + weapon["path"].split("/")[-1])
    box.operator("weaponlab.change_model", text="Change gun model…", icon="FILE_REFRESH")


ANIMATION_FORMATS = [
    ("KEYS", "Keys (.blend)", "The clip's keys, exact (curves, handles), with both rigs: for Blender", ".blend"),
    ("POSES", "Poses (.json.gz)", "Every bone's pose on every frame, with bone names: Weapon Lab and other weapons read it", ".json.gz"),
    ("COMPILED", "Compiled (.vnmclip_c)", "The clip as the game plays it (same as Save Clip As)", ".vnmclip_c"),
    ("FBX", "FBX (.fbx)", "Both rigs with this clip baked, for other tools", ".fbx"),
]


def weaponlab_sample(scene, clip_id):
    """The sandbox's pose sample of a clip (written by an export, or by an import in Weapon Lab)."""
    path = sandbox_folder() / "edits" / (clip_id + ".json.gz")
    return path if path.is_file() else None


def weaponlab_newer(scene, clip_id):
    """Weapon Lab has a version of the clip this .blend has not loaded (an import there, 0.25)."""
    action = clip_action(scene, clip_id)
    path = weaponlab_sample(scene, clip_id)
    return bool(action and path and path.stat().st_mtime > float(action.get("weaponlab_synced", 0)) + 1)


class WEAPONLAB_OT_export_animation(WeaponLabOperator, Operator, ExportHelper):
    bl_idname = "weaponlab.export_animation"
    bl_label = "Export Animation"
    bl_description = "Write this clip's animation to a file: keys (.blend), baked poses (.json.gz), compiled (.vnmclip_c) or FBX"

    format: EnumProperty(name="Format", items=[item[:3] for item in ANIMATION_FORMATS])
    filename_ext: StringProperty(default=".blend", options={"HIDDEN"})
    filter_glob: StringProperty(default="*.blend;*.json.gz;*.vnmclip_c;*.fbx", options={"HIDDEN"})

    def invoke(self, context, event):
        extension = next(item[3] for item in ANIMATION_FORMATS if item[0] == self.format)
        self.filename_ext = extension
        self.filepath = (current_clip(context.scene) or "clip") + extension
        return ExportHelper.invoke(self, context, event)

    def check(self, context):
        # ExportHelper adds the extension by its last suffix only; keep ".json.gz" whole.
        if self.format == "POSES" and not self.filepath.lower().endswith(".json.gz"):
            self.filepath = self.filepath.rsplit(".", 1)[0] + ".json.gz" if self.filepath.lower().endswith(".json") else self.filepath + ".json.gz"
            return True
        return ExportHelper.check(self, context) if self.format != "POSES" else False

    def execute(self, context):
        scene = context.scene
        try:
            clip_id = current_clip(scene)
            action = clip_action(scene, clip_id)
            arms, weapon = rigs(scene)
            frames = clip_at_length(clip_id)["frames"]
            if self.format == "KEYS":
                animation_io.export_keys(self.filepath, action, arms, weapon)
            elif self.format == "POSES":
                pose_io.write_poses(Path(self.filepath), animation_io.sample_action(scene, rig_document(), arms, weapon, action, frames))
            elif self.format == "COMPILED":
                run_export(scene, self.filepath)
            else:
                animation_io.export_fbx(self.filepath, scene, arms, weapon, action, frames)
                switch_clip(scene, clip_id)
            self.report({"INFO"}, "%s: wrote %s" % (clip_id, Path(self.filepath).name))
            return {"FINISHED"}
        except (RuntimeError, OSError, ValueError, subprocess.TimeoutExpired) as error:
            return self.fail(error)


class WEAPONLAB_OT_import_animation(WeaponLabOperator, Operator, ImportHelper):
    bl_idname = "weaponlab.import_animation"
    bl_label = "Import Animation"
    bl_description = ("Put an animation file on this clip, bone by bone (matched by name and parent): keys (.blend), baked poses "
                      "(.json.gz), a compiled clip (.vnmclip_c, e.g. a CS2 clip or another mod's) or FBX. Bones the file lacks keep their keys")
    bl_options = {"REGISTER", "UNDO"}

    filter_glob: StringProperty(default="*.blend;*.json.gz;*.gz;*.vnmclip_c;*.fbx", options={"HIDDEN"})

    def execute(self, context):
        scene = context.scene
        try:
            clip_id = current_clip(scene)
            action = clip_action(scene, clip_id)
            arms, weapon = rigs(scene)
            frames = clip_at_length(clip_id)["frames"]
            kind = animation_io.format_of(self.filepath)
            notes = []
            if kind == "keys":
                result = animation_io.import_keys(self.filepath, action, arms, weapon)
                taken, skipped = result["copied"], result["skipped"]
            elif kind == "fbx":
                result = animation_io.import_fbx(self.filepath, scene, arms, weapon, action, frames)
                taken, skipped = result["copied"], result["skipped"]
            elif kind in ("poses", "compiled"):
                converted = sandbox_folder() / "edits" / (clip_id + ".json.gz.convert")
                folder = sandbox_folder()
                arguments = ["animation", "convert", str(folder), clip_id, str(Path(self.filepath)), str(converted),
                             "--workspace", str(folder.parent.parent.parent.parent), "--profile", folder.parent.parent.name]
                try:
                    result = run_cli(arguments, timeout=300)
                    keyed = pose_io.key_document(rig_document(), arms, weapon, action, pose_io.read_poses(converted), names=result["matched"])
                finally:
                    converted.unlink(missing_ok=True)
                taken = [name for names in keyed.values() for name in names]
                skipped = [name for names in result.get("kept", {}).values() for name in names]
                if result.get("frames", frames) > frames:
                    notes.append("the file is %d frames and this clip %d: set its Length in Anim Events to keep the rest" % (result["frames"], frames))
            else:
                raise RuntimeError("Pick a .blend, .json.gz, .vnmclip_c or .fbx file.")
            switch_clip(scene, clip_id)
            message = "%s: %d bone(s) took %s" % (clip_id, len(taken), Path(self.filepath).name)
            if skipped:
                message += "; kept their own keys: " + ", ".join(skipped[:8]) + (" ..." if len(skipped) > 8 else "")
            if notes:
                message += "; " + "; ".join(notes)
            scene.weaponlab.last_result = message
            self.report({"INFO"}, message + ". Export it to send it to Weapon Lab.")
            return {"FINISHED"}
        except (RuntimeError, OSError, ValueError, KeyError, subprocess.TimeoutExpired) as error:
            return self.fail(error)


class WEAPONLAB_OT_load_weaponlab_version(WeaponLabOperator, Operator):
    bl_idname = "weaponlab.load_weaponlab_version"
    bl_label = "Load Weapon Lab's Version"
    bl_description = ("Key this clip from Weapon Lab's current version of it (its pose sample: an animation imported there, or "
                      "your last export), replacing every bone's keys with one key per frame")
    bl_options = {"REGISTER", "UNDO"}

    def invoke(self, context, event):
        return context.window_manager.invoke_confirm(
            self, event, title="Load Weapon Lab's version?",
            message="Replaces every key of this clip with one key per frame from Weapon Lab's version. Ctrl+Z undoes it.",
            confirm_text="Load")

    def execute(self, context):
        scene = context.scene
        try:
            clip_id = current_clip(scene)
            path = weaponlab_sample(scene, clip_id)
            if path is None:
                raise RuntimeError("Weapon Lab has no version of %s yet (no export or import)." % clip_id)
            action = clip_action(scene, clip_id)
            arms, weapon = rigs(scene)
            pose_io.key_document(rig_document(), arms, weapon, action, pose_io.read_poses(path))
            action["weaponlab_synced"] = path.stat().st_mtime
            switch_clip(scene, clip_id)
            self.report({"INFO"}, "%s: loaded Weapon Lab's version." % clip_id)
            return {"FINISHED"}
        except (RuntimeError, OSError, ValueError, KeyError) as error:
            return self.fail(error)


def draw_animation_files(layout, scene):
    """Export and import one clip's animation (0.25), and Weapon Lab's newer version when it has one."""
    box = layout.box()
    box.label(text="Animation files", icon="FILE_MOVIE")
    clip_id = current_clip(scene)
    try:
        newer = bool(clip_id) and weaponlab_newer(scene, clip_id)
    except RuntimeError:
        newer = False
    if newer:
        row = box.row()
        row.alert = True
        row.operator("weaponlab.load_weaponlab_version", text="Load Weapon Lab's newer version", icon="IMPORT")
    grid = box.grid_flow(columns=2, even_columns=True, align=True)
    for key, label, _, _ in ANIMATION_FORMATS:
        grid.operator("weaponlab.export_animation", text="Export " + label.split(" ")[0]).format = key
    row = box.row(align=True)
    row.operator("weaponlab.import_animation", text="Import animation…", icon="IMPORT")
    if not newer:
        row.operator("weaponlab.load_weaponlab_version", text="", icon="FILE_REFRESH")


class WEAPONLAB_OT_jump_to_event(WeaponLabOperator, Operator):
    bl_idname = "weaponlab.jump_to_event"
    bl_label = "Jump to Event"
    bl_description = "Go to this event's frame"

    frame: bpy.props.IntProperty()

    def execute(self, context):
        context.scene.frame_set(self.frame)
        return {"FINISHED"}


class WEAPONLAB_OT_toggle_view(WeaponLabOperator, Operator):
    bl_idname = "weaponlab.toggle_view"
    bl_label = "Player View"
    bl_description = "Switch between the in-game player camera and a free view"

    def execute(self, context):
        for area in context.screen.areas:
            if area.type == "VIEW_3D":
                region = area.spaces.active.region_3d
                region.view_perspective = "PERSP" if region.view_perspective == "CAMERA" else "CAMERA"
        return {"FINISHED"}


class WEAPONLAB_OT_open_exports(WeaponLabOperator, Operator):
    bl_idname = "weaponlab.open_exports"
    bl_label = "Open Exports Folder"
    bl_description = "Show this sandbox's exported clips in Explorer"

    def execute(self, context):
        folder = sandbox_folder() / "exports"
        folder.mkdir(exist_ok=True)
        os.startfile(str(folder))
        return {"FINISHED"}


# ---- Panel ----

class WEAPONLAB_PT_main(Panel):
    bl_idname = "WEAPONLAB_PT_main"
    bl_label = "Weapon Lab"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Weapon Lab"

    def draw(self, context):
        global _panel_seen
        if not _panel_seen:
            _panel_seen = True
            redraw_viewports()
        layout = self.layout
        data = meta(context.scene)
        if data is None:
            layout.label(text="Open a weapon .blend built by Weapon Lab.", icon="INFO")
            return
        settings = context.scene.weaponlab
        row = layout.row()
        row.label(text=data.get("label", data["weaponId"]), icon="ARMATURE_DATA")
        row.operator("weaponlab.toggle_view", text="", icon="CAMERA_DATA")
        layout.prop(settings, "clip")
        layout.prop(settings, "export_as")
        source = current_clip(context.scene)
        target = export_target(context.scene, source) if source else None
        if target and target != source:
            for line in wrap("Exports into %s; %s's own Action is skipped by Export All." % (target, target), 40):
                layout.label(text=line, icon="BLANK1")

        box = layout.box()
        world = is_world_clip(context.scene, source)
        box.label(text="Editing: third person" if world else "Editing", icon="POSE_HLT")
        if world:
            for line in wrap("The body as other players see it; its gun moves apart from first person's. "
                             "No sounds here: they play from first person.", 40):
                box.label(text=line)
        row = box.row(align=True)
        row.operator("weaponlab.edit_rig", text="Edit Body" if world else "Edit Arms", icon="ARMATURE_DATA").rig = "ARMS"
        row.operator("weaponlab.edit_rig", text="Edit Weapon", icon="TOOL_SETTINGS").rig = "WEAPON"
        box.label(text="Weapon hands follow the hands (always)", icon="LINKED")
        # CS2AL's "red dot": Blender's own auto-key switch, shown as a big toggle.
        row = box.row()
        row.scale_y = 1.3
        auto_key = context.scene.tool_settings.use_keyframe_insert_auto
        row.alert = auto_key  # Tints the button red while recording, like CS2AL.
        row.prop(context.scene.tool_settings, "use_keyframe_insert_auto",
                 text="Auto Key Pose Edits: ON" if auto_key else "Auto Key Pose Edits: off",
                 icon="RECORD_ON" if auto_key else "RECORD_OFF", toggle=True)
        grid = box.grid_flow(columns=2, even_columns=True, align=True)
        for key, label, _ in PRESETS:
            grid.operator("weaponlab.select_preset", text=label).preset = key
        box.operator("weaponlab.reset_clip", icon="LOOP_BACK")

        draw_animation_files(layout, context.scene)
        draw_model(layout)
        draw_game_timing(layout, settings)
        draw_events(layout, settings)

        box = layout.box()
        box.label(text="Export", icon="EXPORT")
        row = box.row()
        row.scale_y = 1.4
        row.operator("weaponlab.export", icon="CHECKMARK")
        box.operator("weaponlab.export_all", icon="DOCUMENTS")
        box.operator("weaponlab.save_clip_as", icon="FILE_TICK")
        box.operator("weaponlab.open_exports", icon="FILE_FOLDER")
        if not os.environ.get(CLI_VARIABLE):
            box.label(text="Open from Weapon Lab to export.", icon="ERROR")
        elif not has_reference(settings.clip):
            box.label(text="Needs updating before export:", icon="ERROR")
            box.label(text="in Weapon Lab click Update for export.")
            box.label(text="Your .blend and edits are kept.")
        if settings.last_result:
            for line in wrap(settings.last_result, 46):
                box.label(text=line)


def draw_events(layout, settings):
    """Events live in Weapon Lab: Blender exports the animation only."""
    box = layout.box()
    box.label(text="Events and clip length", icon="MARKER_HLT")
    frames = clip_frames(settings.clip) if settings.clip != "NONE" else None
    lines = ("Sounds, gameplay events and their timing are edited in Weapon Lab's Anim Events tab; "
             "export sends only the animation, and Weapon Lab adds the events.")
    if frames:
        lines += " This clip is %d frames long (set it in Anim Events)." % frames
    for line in wrap(lines, 38):
        box.label(text=line)


def draw_game_timing(layout, settings):
    """The game's timing for this clip: the frames of the gun whose reload the
    game counts ammo with (picked in Weapon Lab's Anim Events, else the
    vdata's). Shown as bands in the Dope Sheet, Timeline and Graph Editor."""
    document = game_timing()
    timing = clip_timing(settings.clip) if settings.clip != "NONE" else None
    if document is None or timing is None:
        return
    box = layout.box()
    box.label(text="Game timing: %s (%s)" % (document.get("gun") or "?", timing["reference"]), icon="TIME")
    source = document.get("source") or document.get("playsLike") or "?"  # "playsLike": files from Weapon Lab before 0.24.
    for line in wrap("The game counts ammo with %s's reload; the bands show its parts, which this clip cannot change. "
                     "Animate to them." % source, 38):
        box.label(text=line)
    for warning in document.get("warnings", []):
        for number, line in enumerate(wrap(warning, 34)):
            box.label(text=line, icon="ERROR" if number == 0 else "BLANK1")
    for mark in timing["marks"]:
        frame = int(round(mark["frame"]))
        row = box.row(align=True)
        row.operator("weaponlab.jump_to_event", text="%3d" % frame, emboss=False).frame = frame
        row.label(text=mark["label"])


BAND_COLORS = {
    "loop": (1.0, 0.62, 0.2, 0.10),
    "done": (0.0, 0.0, 0.0, 0.30),
    "after": (0.37, 0.82, 0.54, 0.06),
}


def timing_bands(timing):
    """The clip's game timing bands; files from Weapon Lab before 0.24 have only marks, so derive them."""
    if timing.get("bands"):
        return timing["bands"]
    frames = sorted(mark["frame"] for mark in timing["marks"])
    return [{"start": start, "end": end, "label": "", "kind": "intro"} for start, end in zip([0.0] + frames, frames)]


def draw_timing_lines():
    """The game's timing across the Dope Sheet, Timeline and Graph Editor: a tinted band per part
    (intro, the shell loop, outro, after the game is done) and a line per mark, so keys can be put on them."""
    try:
        context = bpy.context
        scene = context.scene
        if meta(scene) is None:
            return
        timing = clip_timing(current_clip(scene))
        if not timing:
            return
        region = context.region
        shader = gpu.shader.from_builtin("UNIFORM_COLOR")
        gpu.state.blend_set("ALPHA")
        font = 0
        blf.size(font, 10)
        for band in timing_bands(timing):
            left, _ = region.view2d.view_to_region(band["start"], 0, clip=False)
            right, _ = region.view2d.view_to_region(band["end"], 0, clip=False)
            left, right = max(left, 0), min(right, region.width)
            if right <= left:
                continue
            shader.uniform_float("color", BAND_COLORS.get(band["kind"], (0.5, 0.72, 1.0, 0.05)))
            corners = [(left, 0), (right, 0), (right, region.height), (left, region.height)]
            batch_for_shader(shader, "TRIS", {"pos": corners}, indices=((0, 1, 2), (0, 2, 3))).draw(shader)
            if band.get("label"):
                blf.color(font, 1.0, 0.72, 0.35, 0.8)
                blf.position(font, left + 4, 28, 0)
                blf.draw(font, band["label"])
        gpu.state.blend_set("NONE")
        points, labels = [], []
        for mark in timing["marks"]:
            x, _ = region.view2d.view_to_region(mark["frame"], 0, clip=False)
            if 0 <= x <= region.width:
                points += [(x, 0), (x, region.height), (x + 1, 0), (x + 1, region.height)]
                labels.append((x, mark["label"]))
        if not points:
            return
        shader = gpu.shader.from_builtin("UNIFORM_COLOR")
        gpu.state.blend_set("ALPHA")
        shader.uniform_float("color", (1.0, 0.62, 0.2, 0.55))
        batch_for_shader(shader, "LINES", {"pos": points}).draw(shader)
        gpu.state.blend_set("NONE")
        font = 0
        blf.size(font, 11)
        blf.color(font, 1.0, 0.72, 0.35, 0.95)
        for number, (x, label) in enumerate(labels):
            blf.position(font, x + 4, region.height - 44 - 14 * (number % 2), 0)  # Staggered: marks can be close.
            blf.draw(font, label)
    except Exception:  # noqa: BLE001 - a draw callback must never break the editor
        pass


def has_reference(clip_id):
    """Builds before Weapon Lab 0.5 lack the reference samples export needs."""
    try:
        return (input_folder() / "reference" / (clip_id + ".json.gz")).is_file()
    except RuntimeError:
        return False


def wrap(text, width):
    lines, line = [], ""
    for word in text.split():
        if line and len(line) + len(word) + 1 > width:
            lines.append(line)
            line = word
        else:
            line = (line + " " + word).strip()
    return lines + ([line] if line else [])


CLASSES = (
    WeaponLabSettings,
    WEAPONLAB_OT_edit_rig,
    WEAPONLAB_OT_select_preset,
    WEAPONLAB_OT_reset_clip,
    WEAPONLAB_OT_export,
    WEAPONLAB_OT_export_all,
    WEAPONLAB_OT_save_clip_as,
    WEAPONLAB_OT_change_model,
    WEAPONLAB_OT_export_animation,
    WEAPONLAB_OT_import_animation,
    WEAPONLAB_OT_load_weaponlab_version,
    WEAPONLAB_OT_jump_to_event,
    WEAPONLAB_OT_toggle_view,
    WEAPONLAB_OT_open_exports,
    WEAPONLAB_PT_main,
)


def sync_settings(scene):
    """Points the clip picker at the clip the rigs are playing and fits the timeline to it."""
    if meta(scene) is None:
        return
    clip_id = current_clip(scene)
    if clip_id and scene.weaponlab.clip != clip_id:
        scene.weaponlab.clip = clip_id
    fit_scene_to_clip(scene)


def fit_scene_to_clip(scene):
    """Puts the timeline on the current clip's frames. A .blend keeps the range it was
    saved with, and the clip's length may have changed in Weapon Lab since then."""
    try:
        clip_id = current_clip(scene)
        clip = clip_at_length(clip_id) if clip_id else None
    except (RuntimeError, StopIteration, KeyError):
        return
    if clip and (scene.frame_start != 0 or scene.frame_end != clip["frames"] - 1):
        pose_io.apply_timing(scene, clip)


_lengths_stamp = None


def follow_clip_lengths():
    """Weapon Lab may change a clip's length while Blender is open; the timeline follows."""
    global _lengths_stamp
    try:
        path = input_folder() / LENGTHS_FILE
        stamp = path.stat().st_mtime if path.is_file() else None
    except (RuntimeError, OSError):
        return 2.0
    if stamp != _lengths_stamp:
        _lengths_stamp = stamp
        for scene in bpy.data.scenes:
            if meta(scene) is not None:
                fit_scene_to_clip(scene)
    return 2.0


def show_action_channels():
    """The Action Editor's channel list (bone names beside the keys); .blend files built before 0.19 had it hidden."""
    for screen in bpy.data.screens:
        for area in screen.areas:
            if area.type == "DOPESHEET_EDITOR" and hasattr(area.spaces.active, "show_region_channels"):
                area.spaces.active.show_region_channels = True


def stamp_synced():
    """A .blend from before 0.25 has no "weaponlab_synced" stamps, so every clip it ever exported looked like it
    had a newer version in Weapon Lab (0.25.1 fix): the samples there are its own exports, so stamp them as loaded."""
    for scene in bpy.data.scenes:
        data = meta(scene)
        if data is None:
            continue
        for clip_id, action_name in data["actions"].items():
            action = bpy.data.actions.get(action_name)
            if action is None or "weaponlab_synced" in action:
                continue
            try:
                path = weaponlab_sample(scene, clip_id)
            except RuntimeError:
                return  # No sandbox found for this .blend: nothing to compare with.
            action["weaponlab_synced"] = path.stat().st_mtime if path else 0.0


def upgrade_blend():
    """Brings a .blend built by an older Weapon Lab up to date when it opens: the Action Editor's channel
    list, the arm bone colours and preview meshes that follow bone scale. Display and constraints only;
    bones and keys are not touched."""
    if not any(meta(scene) is not None for scene in bpy.data.scenes):
        return
    show_action_channels()
    stamp_synced()
    for obj in bpy.data.objects:
        if obj.type != "ARMATURE" or obj.library is not None:
            continue
        role = obj.get("weaponlab_role")
        if role == "arms":
            rig_style.apply_arm_colors(obj)
        elif role == "preview_rig":
            rig_style.add_scale_follow(obj)


@bpy.app.handlers.persistent
def on_load(_):
    for scene in bpy.data.scenes:
        sync_settings(scene)
    upgrade_blend()


_panel_seen = False
_hint_handler = None


def redraw_viewports():
    for window in bpy.context.window_manager.windows:
        for area in window.screen.areas:
            if area.type == "VIEW_3D":
                area.tag_redraw()


def draw_sidebar_hint():
    """Until the Weapon Lab panel has been opened once, tell the user where it is."""
    if _panel_seen or meta(bpy.context.scene) is None:
        return
    region = bpy.context.region
    font = 0
    blf.size(font, 18)
    blf.color(font, 1.0, 0.82, 0.3, 1.0)
    text = "Weapon Lab: press N, then open the 'Weapon Lab' tab on the right"
    width, _ = blf.dimensions(font, text)
    blf.position(font, max(20, (region.width - width) / 2), region.height - 60, 0)
    blf.draw(font, text)


_timing_handler = None
_graph_timing_handler = None


def register():
    global _hint_handler, _timing_handler, _graph_timing_handler
    for cls in CLASSES:
        bpy.utils.register_class(cls)
    bpy.types.Scene.weaponlab = PointerProperty(type=WeaponLabSettings)
    bpy.app.handlers.load_post.append(on_load)
    _hint_handler = bpy.types.SpaceView3D.draw_handler_add(draw_sidebar_hint, (), "WINDOW", "POST_PIXEL")
    _timing_handler = bpy.types.SpaceDopeSheetEditor.draw_handler_add(draw_timing_lines, (), "WINDOW", "POST_PIXEL")
    _graph_timing_handler = bpy.types.SpaceGraphEditor.draw_handler_add(draw_timing_lines, (), "WINDOW", "POST_PIXEL")
    if not bpy.app.timers.is_registered(follow_clip_lengths):
        bpy.app.timers.register(follow_clip_lengths, first_interval=2.0, persistent=True)


def unregister():
    global _hint_handler, _timing_handler, _graph_timing_handler
    if bpy.app.timers.is_registered(follow_clip_lengths):
        bpy.app.timers.unregister(follow_clip_lengths)
    if _hint_handler is not None:
        bpy.types.SpaceView3D.draw_handler_remove(_hint_handler, "WINDOW")
        _hint_handler = None
    if _timing_handler is not None:
        bpy.types.SpaceDopeSheetEditor.draw_handler_remove(_timing_handler, "WINDOW")
        _timing_handler = None
    if _graph_timing_handler is not None:
        bpy.types.SpaceGraphEditor.draw_handler_remove(_graph_timing_handler, "WINDOW")
        _graph_timing_handler = None
    if on_load in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(on_load)
    del bpy.types.Scene.weaponlab
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)
