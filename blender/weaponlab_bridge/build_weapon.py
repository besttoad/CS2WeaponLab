"""Build a per-weapon .blend from a Weapon Lab rig package.

Run by the Weapon Lab app, headless:

    blender --background --factory-startup --python build_weapon.py -- <input folder> <output.blend>

The input folder holds rig.json and poses/*.json.gz written by the app from
the sandbox's template clips. After building, every clip is sampled back from
the rigs into reference/*.json.gz: export compares an edited sample with this
reference, so only real edits reach the clip.

Progress lines start with WEAPONLAB_PROGRESS, the final summary with
WEAPONLAB_RESULT and failures with WEAPONLAB_ERROR.

Rig construction, additive composition, preview binding and staging are
ported from CS2 Animation Lab's library builder.
"""
import json
import math
import os
import sys
import traceback
from pathlib import Path

import bpy
from mathutils import Matrix, Quaternion, Vector

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pose_io  # noqa: E402  (needs the path above)
import rig_style  # noqa: E402

FOLLOW_CONSTRAINT = "Weapon Lab - follow hand"
ANCHOR_CONSTRAINT = "Weapon Lab - stay with the Arms rig, not the gun"
SCHEMA = "weaponlab.rig/1"
BLEND_SCHEMA = "weaponlab.blend/1"


def progress(message):
    print("WEAPONLAB_PROGRESS " + message, flush=True)


# ---- Rigs ----

def create_rig(name, skeleton, rest, size=1.0):
    """size: bone stick length (the third-person body is a whole player, so its sticks are longer)."""
    rig = bpy.data.objects.new(name, bpy.data.armatures.new(name + " bones"))
    bpy.context.scene.collection.objects.link(rig)
    bpy.context.view_layer.objects.active = rig
    rig.select_set(True)
    bpy.ops.object.mode_set(mode="EDIT")
    rest_world = pose_io.worlds(skeleton, rest)
    for bone in skeleton["bones"]:
        edit = rig.data.edit_bones.new(bone["name"])
        parent = pose_io.parent_name(skeleton, bone)
        edit.parent = rig.data.edit_bones.get(parent) if parent else None
        edit.use_connect = False
        edit.matrix = rest_world[bone["name"]]
        edit.length = (0.6 if bone["name"].startswith("finger") else 1.0) * size
    bpy.ops.object.mode_set(mode="OBJECT")
    for pose_bone in rig.pose.bones:
        pose_bone.rotation_mode = "QUATERNION"
        pose_bone.matrix_basis = Matrix.Identity(4)
    rig.show_in_front = True
    rig.data.display_type = "STICK"
    return rig


def add_follow_constraints(arms):
    """wpnHand_* follows hand_* while posing. Built muted so the reference sample keeps CS2's
    own tiny offset between them; switched on once the reference is written. Export glues them
    the same way (HandGlue in Weapon Lab), so what Blender shows is what the game plays."""
    for side in ("L", "R"):
        target = arms.pose.bones.get("wpnHand_" + side)
        hand = arms.pose.bones.get("hand_" + side)
        if target is None or hand is None:
            continue
        constraint = target.constraints.new("COPY_TRANSFORMS")
        constraint.name = FOLLOW_CONSTRAINT
        constraint.target = arms
        constraint.subtarget = hand.name
        constraint.target_space = "POSE"
        constraint.owner_space = "POSE"
        constraint.mix_mode = "REPLACE"
        constraint.mute = True


def bone_collections(rig, primary, extra=(), no_mesh=()):
    """Sorts bones into collections. Extra bones (a custom skeleton's own) get their own, coloured
    differently; bones without mesh (CS2 bones the model lacks, model bones nothing is weighted to)
    go to a hidden collection so they do not clutter the rig."""
    visible = rig.data.collections.new("Arms - edit these" if primary else "Weapon - edit these")
    hidden = rig.data.collections.new("Game anchors - original hierarchy")
    hidden.is_visible = False
    custom = rig.data.collections.new("Custom skeleton bones - edit these") if extra else None
    empty = rig.data.collections.new("No mesh - hidden") if no_mesh else None
    if empty:
        empty.is_visible = False
    for bone in rig.data.bones:
        if bone.name in no_mesh:
            empty.assign(bone)
            bone.color.palette = "THEME04"
            continue
        if bone.name.endswith("_end") and not bone.children and bone.name[:-4] in rig.data.bones:
            hidden.assign(bone)  # A Blender FBX leaf bone in an older custom skeleton.
            continue
        if bone.name in extra:
            custom.assign(bone)
            bone.color.palette = "THEME09"
            continue
        if primary:
            editable = bone.name.startswith(("arm", "hand_", "finger_")) or bone.name == "wpn"
        else:
            editable = not bone.name.endswith("_end") and bone.name not in ("weapon", "econ", "muzzle")
        (visible if editable else hidden).assign(bone)
        bone.color.palette = "THEME03" if primary else "THEME04"



# Body bones the game moves on its own or that only mark places (attachments, aim, camera): hidden like the arms' anchors.
BODY_ANCHORS = ("root_motion", "wpnHand_L", "wpnHand_R", "wpnTip", "wpnEnd", "wpnAimIntent", "attachWorld", "camera")


def body_collections(body):
    """The third-person Body rig (0.26): the body, legs, arms, fingers and the gun's wpn/wpnPivot to edit; anchors hidden."""
    visible = body.data.collections.new("Body - edit these")
    hidden = body.data.collections.new("Game anchors - original hierarchy")
    hidden.is_visible = False
    for bone in body.data.bones:
        anchor = bone.name in BODY_ANCHORS or bone.name.startswith("attach")
        (hidden if anchor else visible).assign(bone)
        bone.color.palette = "THEME03"


def add_prop_bones(arms, weapon, names):
    """Props kept apart from the gun (e.g. a spare magazine in the other hand) stay in the Weapon rig, but their Blender parent
    is a hidden anchor bone that copies the Arms rig object (pose_io.PROP_ANCHOR): the whole Weapon rig rides
    on wpn, and the anchor cancels that, so moving the gun leaves the prop where it is. The game skeleton keeps
    the prop under the gun's root; export samples it relative to that, so the game shows what Blender shows."""
    if not names:
        return
    bpy.context.view_layer.objects.active = weapon
    bpy.ops.object.mode_set(mode="EDIT")
    anchor = weapon.data.edit_bones.new(pose_io.PROP_ANCHOR)
    anchor.tail = (0, 1, 0)  # A new bone has no length, and .matrix would then keep only its head.
    # At rest the Weapon rig sits on wpn's rest, so this puts the anchor on the Arms rig's origin.
    anchor.matrix = arms.data.bones["wpn"].matrix_local.inverted()
    anchor.length = 1.0
    for name in names:
        edit = weapon.data.edit_bones[name]
        edit.parent = anchor  # Edit bones keep their rest place when reparented.
        edit.use_connect = False
    bpy.ops.object.mode_set(mode="OBJECT")

    helper = weapon.data.collections.new("Props anchor - hidden")
    helper.is_visible = False
    anchor_bone = weapon.data.bones[pose_io.PROP_ANCHOR]
    for collection in list(anchor_bone.collections):
        collection.unassign(anchor_bone)
    helper.assign(anchor_bone)
    anchor_bone.hide_select = True
    pose_anchor = weapon.pose.bones[pose_io.PROP_ANCHOR]
    pose_anchor.rotation_mode = "QUATERNION"
    pose_anchor.matrix_basis = Matrix.Identity(4)
    constraint = pose_anchor.constraints.new("COPY_TRANSFORMS")
    constraint.name = ANCHOR_CONSTRAINT
    constraint.target = arms  # The object, not a bone: the Arms rig's own space, which the gun never moves.
    constraint.target_space = "WORLD"
    constraint.owner_space = "WORLD"

    props = weapon.data.collections.new("Props - edit these")
    for name in names:
        bone = weapon.data.bones[name]
        bone["weaponlab_prop"] = True
        for collection in list(bone.collections):
            collection.unassign(bone)
        props.assign(bone)
    rig_style.apply_prop_colors(weapon)


# ---- Stock preview meshes ----

def import_preview(path):
    before = set(bpy.data.objects)
    bpy.ops.import_scene.gltf(filepath=str(path), import_pack_images=False, merge_vertices=False)
    objects = list(set(bpy.data.objects) - before)
    rigs = [obj for obj in objects if obj.type == "ARMATURE"]
    if len(rigs) != 1:
        for obj in objects:
            bpy.data.objects.remove(obj, do_unlink=True)
        raise RuntimeError("preview must contain exactly one skinned rig")
    rig = rigs[0]
    shapes = {bone.custom_shape for bone in rig.pose.bones if bone.custom_shape}
    for bone in rig.pose.bones:
        bone.custom_shape = None
    for obj in shapes:
        if obj in objects:
            objects.remove(obj)
        bpy.data.objects.remove(obj, do_unlink=True)
    meshes = [obj for obj in objects if obj.type == "MESH"]
    for mesh in meshes:
        mesh.hide_select = True
    for obj in objects:
        if obj not in meshes and obj != rig:
            # Flatten glTF scene roots while keeping world transforms.
            for child in list(obj.children):
                world = child.matrix_world.copy()
                child.parent = None
                child.matrix_world = world
            bpy.data.objects.remove(obj, do_unlink=True)
    return rig, meshes


def follow(bone, target, name):
    """Makes a preview bone copy the rig bone's location and rotation (world space), and its scale in
    pose space (a world-space scale would shrink the metre-sized meshes), so scaling a bone scales the mesh."""
    for constraint_type in ("COPY_LOCATION", "COPY_ROTATION"):
        constraint = bone.constraints.new(constraint_type)
        constraint.target = target
        constraint.subtarget = name
        constraint.target_space = "WORLD"
        constraint.owner_space = "WORLD"
    rig_style.scale_follow(bone, target, name)


def bind_preview(path, target, kind, material, label=None):
    """label: the name the meshes and preview rig get ("Mesh - <label>"), default the kind."""
    rig, meshes = import_preview(path)
    label = label or kind
    anchor = {"arms": "hand_R", "gloves": "hand_R", "body": "pelvis"}.get(kind, "weapon")
    if anchor not in rig.data.bones or anchor not in target.data.bones:
        for obj in meshes + [rig]:
            bpy.data.objects.remove(obj, do_unlink=True)
        raise RuntimeError("preview lacks anchor bone " + anchor)

    # glTF is in metres, the rigs in game units (1 unit = 0.0254 m). A fixed factor, not one guessed
    # from bone distances: custom models (an MP5-boned pistol in the Bizon slot) place shared bones differently.
    ratio = 1.0 / 0.0254

    correction = target.data.bones[anchor].matrix_local @ Matrix.Scale(ratio, 4) @ rig.data.bones[anchor].matrix_local.inverted()
    rig.parent = target
    rig.matrix_parent_inverse = Matrix.Identity(4)
    rig.matrix_basis = correction
    rig.name = "Preview " + label
    rig["weaponlab_role"] = "preview_rig"
    bpy.context.view_layer.update()
    first_person_arms = kind in ("arms", "gloves")
    for bone in rig.pose.bones:
        # The viewmodel rig names the upper arm armUpperShoulder_*; the third-person body keeps arm_upper_*.
        name = bone.name.replace("arm_upper_", "armUpperShoulder_") if first_person_arms and bone.name.startswith("arm_upper_") else bone.name
        if name not in target.pose.bones:
            continue
        # Every shared bone follows the rig. (A 0.11 check that skipped bones whose heads did not line up
        # compared bones the glTF importer re-orients, so it skipped nearly all of them; removed 2026-10-25.)
        follow(bone, target, name)
    if first_person_arms:
        # An agent's sleeves are also weighted to body bones the viewmodel rig lacks (clavicle, spine).
        # Left alone they stay where the standing body would be and drag the sleeve far off; instead each
        # rides with its nearest descendant that follows the rig.
        for bone in rig.pose.bones:
            if bone.constraints:
                continue
            followed = next((child for child in bone.children_recursive if child.constraints), None)
            if followed is not None:
                follow(bone, target, followed.constraints[0].subtarget)
    for number, mesh in enumerate(meshes, start=1):
        mesh.name = "Mesh - " + label + ("" if len(meshes) == 1 else " " + str(number))
        mesh["weaponlab_role"] = "preview_mesh"
        mesh.data.materials.clear()
        mesh.data.materials.append(material)
    rig.hide_set(True)
    rig.hide_select = True
    return meshes


def preview_material(name, color, metallic=0.0):
    material = bpy.data.materials.new("Weapon Lab - " + name)
    material.diffuse_color = color
    material.use_nodes = True
    shader = material.node_tree.nodes.get("Principled BSDF")
    if shader:
        shader.inputs["Base Color"].default_value = color
        shader.inputs["Roughness"].default_value = 0.55
        shader.inputs["Metallic"].default_value = metallic
    return material


# ---- Staging ----

def mount_weapon(scene, arms, weapon, name="Weapon mount - follows wpn"):
    """The Weapon rig rides on the arms' wpn bone, as the game carries the weapon model (and in third person
    the body's wpn)."""
    mount = bpy.data.objects.new(name, None)
    scene.collection.objects.link(mount)
    constraint = mount.constraints.new("COPY_TRANSFORMS")
    constraint.target = arms
    constraint.subtarget = "wpn"
    weapon.parent = mount
    weapon.matrix_parent_inverse = Matrix.Identity(4)
    mount.hide_set(True)
    mount.hide_select = True
    return mount


def third_person(*objects):
    """Tags objects of the third-person view (0.26), so staging puts them in its own collection."""
    for obj in objects:
        if obj is not None:
            obj["weaponlab_view"] = pose_io.WORLD


def stage_body(scene, body):
    """The Body rig stands on the floor at the origin, facing -Y like Blender's front view. Its engine space is
    already Z-up, so unlike the arms it needs no turn upright; poses are parent-local, so this never leaks into clips."""
    presentation = bpy.data.objects.new("Presentation (third person) - do not apply", None)
    scene.collection.objects.link(presentation)
    presentation.rotation_euler = (0, 0, -math.pi / 2)
    body.parent = presentation
    third_person(presentation)
    presentation.hide_set(True)
    presentation.hide_select = True


def stage(scene, arms, weapon, collections):
    presentation = bpy.data.objects.new("Presentation - do not apply", None)
    scene.collection.objects.link(presentation)
    # Engine Y-up to Blender Z-up for viewing. Export reads parent-local poses, so this never leaks into clips.
    presentation.rotation_euler = (math.pi / 2, 0, 0)
    arms.parent = presentation

    camera = bpy.data.objects.new("Player view", bpy.data.cameras.new("Player view"))
    scene.collection.objects.link(camera)
    camera.parent = presentation
    camera.rotation_euler = Vector((0, 0, 1)).to_track_quat("-Z", "Y").to_euler()
    camera.data.lens = 21
    camera.data.clip_start = 0.05
    camera.data.clip_end = 500
    scene.camera = camera

    world = bpy.data.worlds.new("Weapon Lab - charcoal")
    world.color = (0.045, 0.052, 0.065)
    scene.world = world
    for name, position, energy, size in [("Key", (22, -12, 28), 15000, 25), ("Fill", (-22, -20, 12), 9000, 20), ("Rim", (0, -48, 18), 13000, 18)]:
        light_data = bpy.data.lights.new(name, "AREA")
        light_data.energy = energy
        light_data.shape = "DISK"
        light_data.size = size
        light = bpy.data.objects.new(name, light_data)
        scene.collection.objects.link(light)
        light.location = position
        light.rotation_euler = (Vector((0, -18, -2)) - light.location).to_track_quat("-Z", "Y").to_euler()
        light.hide_select = True
        light.hide_set(True)  # Hidden in the viewport only; still lights renders.

    for obj in list(scene.objects):
        role = obj.get("weaponlab_role", "")
        if obj.get("weaponlab_view") == pose_io.WORLD:
            destination = collections["world"]  # Third person (0.26): its rigs, meshes, mount and presentation.
        elif role in ("arms", "weapon"):
            destination = collections["rigs"]
        elif obj.type == "MESH" or role == "preview_rig":
            destination = collections["preview"]
        else:
            destination = collections["studio"]
        for current in list(obj.users_collection):
            current.objects.unlink(obj)
        destination.objects.link(obj)
    presentation.hide_set(True)
    presentation.hide_select = True
    camera.hide_set(True)


def configure_views():
    for screen in bpy.data.screens:
        for area in screen.areas:
            if area.type == "VIEW_3D":
                space = area.spaces.active
                space.shading.type = "SOLID"
                space.shading.color_type = "MATERIAL"
                space.overlay.show_floor = False
                space.overlay.show_axis_x = False
                space.overlay.show_axis_y = False
                space.overlay.show_relationship_lines = False
                location, distance = pose_io.VIEW_FRAMING[False]
                space.region_3d.view_location = Vector(location)
                space.region_3d.view_distance = distance
                space.region_3d.view_rotation = Quaternion((0.82, 0.42, -0.16, -0.35)).normalized()
                space.region_3d.view_perspective = "PERSP"
            elif area.type == "DOPESHEET_EDITOR":
                space = area.spaces.active
                space.mode = "ACTION"
                space.show_pose_markers = True
                if hasattr(space, "show_region_channels"):
                    space.show_region_channels = True  # Bone names beside the keys, as in the Animation workspace.


# ---- Main ----

# Shared with the add-on's animation import (0.25): pose_io.same_rest, pose_io.copy_curve.
same_rest = pose_io.same_rest
copy_curve = pose_io.copy_curve


def carry_keys(previous, actions, views):
    """Carries the user's keys from the previous .blend of this weapon (Weapon Lab 0.24), so the rebuilt file
    animates as the old one did. Per clip and rig, every bone on both rigs with the same parent and rest gets its
    old curves instead of the clip's; one the old clip left unkeyed is left unkeyed again, at the old file's pose.
    Bones that are new, or whose rest or parent changed, keep the clip's motion (keys are rest-relative). Run after
    the references are sampled, so they stay the untouched clips and export sends the carried keys as edits.
    views: {third person?: {slot key: rig}}; a clip's keys go onto its own view's rigs (0.26). A .blend from before
    0.26 has no third-person clips, so those keep CS2's motion."""
    wanted = {rig.name for rigs in views.values() for rig in rigs.values() if rig}
    with bpy.data.libraries.load(str(previous), link=False) as (source, loaded):
        loaded.actions = list(source.actions)
        loaded.objects = [name for name in source.objects if name in wanted]
    old_actions = [action for action in loaded.actions if action is not None]
    old_rigs = [rig for rig in loaded.objects if rig is not None and rig.type == "ARMATURE"]
    # Loaded under the same name as the new rig, or with a ".001" suffix when that name is taken.
    by_name = {(rig.name if rig.name in wanted else rig.name.rsplit(".", 1)[0]): rig for rig in old_rigs}
    by_clip = {action.get("weaponlab_clip"): action for action in old_actions if action.get("weaponlab_clip")}
    carried_clips, carried_bones, kept, unkeyed = 0, set(), set(), {}
    try:
        for clip_id, action in actions.items():
            old = by_clip.get(clip_id)
            if old is None:
                continue
            carried = False
            for key, rig in views[pose_io.is_world(action)].items():
                old_rig = by_name.get(rig.name) if rig else None
                if rig is None or old_rig is None or key not in action or key not in old or not old.layers:
                    continue
                new_bag = action.layers[0].strips[0].channelbag(pose_io.slot_for(action, key))
                old_bag = old.layers[0].strips[0].channelbag(pose_io.slot_for(old, key))
                if new_bag is None or old_bag is None:
                    continue
                by_bone = {}
                for curve in old_bag.fcurves:
                    if curve.data_path.startswith('pose.bones["'):
                        by_bone.setdefault(curve.data_path.split('"')[1], []).append(curve)
                for bone in rig.data.bones:
                    old_bone = old_rig.data.bones.get(bone.name)
                    if old_bone is None:
                        continue  # A new bone: the clip's motion.
                    if not same_rest(bone, old_bone):
                        if bone.name in by_bone:
                            kept.add(bone.name)
                        continue
                    pose_io.clear_bones(new_bag, [bone.name])
                    for curve in by_bone.get(bone.name, []):
                        copy_curve(new_bag, curve, bone.name)
                    if bone.name not in by_bone:
                        unkeyed[(rig.name, bone.name)] = old_rig.pose.bones[bone.name].matrix_basis.copy()
                    carried_bones.add(bone.name)
                    carried = True
                kept.update(name for name in by_bone if name not in rig.data.bones)  # Gone from the new rig.
            carried_clips += carried
        # An unkeyed bone holds whatever pose it has; give it the old file's.
        for (rig_name, bone), basis in unkeyed.items():
            next(rig for rigs in views.values() for rig in rigs.values() if rig and rig.name == rig_name).pose.bones[bone].matrix_basis = basis
    finally:
        for action in old_actions:
            bpy.data.actions.remove(action)
        for rig in old_rigs:
            armature = rig.data
            bpy.data.objects.remove(rig)
            if armature.users == 0:
                bpy.data.armatures.remove(armature)
    return {"clips": carried_clips, "bones": len(carried_bones), "notCarried": sorted(kept)}


def build(input_dir, output, previous=None):
    rig_doc = json.loads((input_dir / "rig.json").read_text(encoding="utf-8"))
    if rig_doc.get("schema") != SCHEMA:
        raise RuntimeError("Unsupported rig package schema: " + str(rig_doc.get("schema")))

    weapon_id = rig_doc["weaponId"]
    progress("Building " + rig_doc["label"] + " (" + weapon_id + ")")
    bpy.ops.wm.read_factory_settings(use_empty=True)
    scene = bpy.context.scene
    scene.name = "Weapon Lab / " + weapon_id

    arms_skeleton = rig_doc["skeletons"]["arms"]
    weapon_skeleton = rig_doc["skeletons"].get("weapon")
    arms = create_rig("Arms - " + weapon_id, arms_skeleton, pose_io.rest_locals(arms_skeleton, primary=True))
    arms["weaponlab_role"] = "arms"
    add_follow_constraints(arms)
    bone_collections(arms, primary=True)
    rig_style.apply_arm_colors(arms)  # Finger sections tell apart; replaces the single theme colour.
    weapon = None
    if weapon_skeleton:
        weapon = create_rig("Weapon - " + weapon_id, weapon_skeleton, pose_io.rest_locals(weapon_skeleton, primary=False))
        weapon["weaponlab_role"] = "weapon"
        bone_collections(weapon, primary=False, extra=set(weapon_skeleton.get("extraBones") or ()),
                         no_mesh=set(weapon_skeleton.get("hiddenBones") or ()))
    props = pose_io.free_bones(weapon_skeleton) if weapon else []
    add_prop_bones(arms, weapon, props)

    # Third person (0.26): the Body rig (worldmodel.vnmskel) and the weapon's own rig on the body's wpn. The gun's
    # third-person motion differs from first person's, so it gets a rig of its own (CS2's weapon skeleton).
    body_skeleton = rig_doc["skeletons"].get("body")
    world_weapon_skeleton = rig_doc["skeletons"].get("worldWeapon")
    body = world_weapon = None
    if body_skeleton:
        body = create_rig("Body - " + weapon_id, body_skeleton, pose_io.rest_locals(body_skeleton, primary=False), size=2.5)
        body["weaponlab_role"] = "body"
        add_follow_constraints(body)
        body_collections(body)
        rig_style.apply_arm_colors(body)
        if world_weapon_skeleton:
            world_weapon = create_rig("Weapon (third person) - " + weapon_id, world_weapon_skeleton, pose_io.rest_locals(world_weapon_skeleton, primary=False))
            world_weapon["weaponlab_role"] = "weapon_world"
            bone_collections(world_weapon, primary=False)
        third_person(body, world_weapon)
    views = {False: {"weaponlab_arms_slot": arms, "weaponlab_weapon_slot": weapon},
             True: {"weaponlab_arms_slot": body, "weaponlab_weapon_slot": world_weapon}}

    clips = [clip for clip in rig_doc["clips"] if not pose_io.is_world(clip) or body is not None]
    poses = {clip["id"]: pose_io.read_poses(input_dir / clip["poses"]) for clip in clips}
    default_id = rig_doc["defaultClip"]
    base = poses[default_id]

    actions = {}
    prop_frames = {}  # clip id -> its arms frames, to key the props' hold once the references are taken.
    markers = 0
    for number, clip in enumerate(clips, start=1):
        progress("Clip %d/%d: %s" % (number, len(clips), clip["id"]))
        clip_poses = poses[clip["id"]]
        world = pose_io.is_world(clip)
        primary_rig, weapon_rig = views[world].values()
        primary_skeleton, weapon_skel, primary = pose_io.skeletons_of(rig_doc, world)
        action = bpy.data.actions.new(weapon_id + " / " + clip["id"])
        action.use_fake_user = True
        action["weaponlab_clip"] = clip["id"]
        action["weaponlab_resource"] = clip["resourcePath"]
        action["weaponlab_template_sha256"] = clip["sha256"]
        if world:
            action["weaponlab_view"] = pose_io.WORLD
        strip = action.layers.new("Motion").strips.new(type="KEYFRAME")

        # Additive first-person clips show composed over the default clip; third-person ones are never built (read-only).
        additive_base = base if clip["additive"] and not world else None
        arms_slot = action.slots.new(id_type="OBJECT", name="Body" if world else "Arms")
        action["weaponlab_arms_slot"] = arms_slot.identifier
        arms_frames = pose_io.frame_locals(clip_poses["arms"], primary_skeleton, primary, additive_base["arms"] if additive_base else None)
        arms_bag = strip.channelbag(arms_slot, ensure=True)
        pose_io.key_channels(arms_bag, pose_io.channel_values(primary_rig, primary_skeleton, arms_frames))

        if weapon_rig and clip["hasWeapon"] and "weapon" in clip_poses:
            weapon_slot = action.slots.new(id_type="OBJECT", name="Weapon (third person)" if world else "Weapon")
            action["weaponlab_weapon_slot"] = weapon_slot.identifier
            weapon_base = additive_base.get("weapon") if additive_base else None
            weapon_frames = pose_io.frame_locals(clip_poses["weapon"], weapon_skel, False, weapon_base)
            weapon_bag = strip.channelbag(weapon_slot, ensure=True)
            pose_io.key_channels(weapon_bag, pose_io.channel_values(weapon_rig, weapon_skel, weapon_frames, skip=() if world else props))
            if props and not world:
                # First keyed where the game carries them (with the gun), so the references below are the
                # stock clip; then they hold still (see below).
                places = pose_io.prop_places(arms_skeleton, arms_frames, weapon_skeleton, weapon_frames, props)
                pose_io.key_channels(weapon_bag, pose_io.prop_values(weapon, arms_skeleton, arms_frames, places))
                prop_frames[clip["id"]] = arms_frames

        actions[clip["id"]] = action

    # Mounted before the references: a prop's anchor copies the Arms rig object in world space, so the prop's
    # place relative to the gun is only right once the Weapon rig rides on wpn.
    if weapon:
        mount_weapon(scene, arms, weapon)
    if world_weapon:
        third_person(mount_weapon(scene, body, world_weapon, "Weapon mount (third person) - follows wpn"))

    # Sample every untouched clip back from the rigs: export subtracts this reference.
    for number, clip in enumerate(clips, start=1):
        progress("Reference %d/%d: %s" % (number, len(clips), clip["id"]))
        action = actions[clip["id"]]
        world = pose_io.is_world(clip)
        primary_rig, weapon_rig = views[world].values()
        primary_skeleton, weapon_skel, primary = pose_io.skeletons_of(rig_doc, world)
        pose_io.assign(primary_rig, action, pose_io.slot_for(action, "weaponlab_arms_slot"))
        reference = {"arms": pose_io.sample(scene, primary_rig, primary_skeleton, primary, clip["frames"])}
        if weapon_rig and "weaponlab_weapon_slot" in action:
            pose_io.assign(weapon_rig, action, pose_io.slot_for(action, "weaponlab_weapon_slot"))
            reference["weapon"] = pose_io.sample(scene, weapon_rig, weapon_skel, False, clip["frames"])
        pose_io.write_poses(input_dir / "reference" / (clip["id"] + ".json.gz"), reference)

    # Props now hold still where the default clip starts them, in every clip, rather than ride with the gun.
    # The references keep the stock motion, so export writes the hold and the game holds them still too.
    if prop_frames:
        hold = pose_io.prop_hold(base, arms_skeleton, weapon_skeleton, props)
        for clip_id, arms_frames in prop_frames.items():
            action = actions[clip_id]
            bag = action.layers[0].strips[0].channelbag(pose_io.slot_for(action, "weaponlab_weapon_slot"))
            pose_io.clear_bones(bag, props)
            pose_io.key_channels(bag, pose_io.prop_values(weapon, arms_skeleton, arms_frames, pose_io.held(hold, len(arms_frames))))

    # Rebuild that keeps the user's animation (0.24): their keys from the previous .blend, after the references.
    carried = None
    if previous:
        progress("Carrying your keys from " + Path(previous).name)
        carried = carry_keys(previous, actions, views)

    for rig in (arms, body):
        for side in ("L", "R"):
            target = rig.pose.bones.get("wpnHand_" + side) if rig else None
            constraint = target.constraints.get(FOLLOW_CONSTRAINT) if target else None
            if constraint:
                constraint.mute = False

    # Each view's rigs start on a clip of their own: first person on the default clip, third person on its first one.
    starts = [default_id] + [next((clip["id"] for clip in clips if pose_io.is_world(clip)), None)]
    for world, clip_id in zip((False, True), starts):
        primary_rig, weapon_rig = views[world].values()
        if clip_id is None or primary_rig is None:
            continue
        start_action = actions[clip_id]
        pose_io.assign(primary_rig, start_action, pose_io.slot_for(start_action, "weaponlab_arms_slot"))
        if weapon_rig and "weaponlab_weapon_slot" in start_action:
            pose_io.assign(weapon_rig, start_action, pose_io.slot_for(start_action, "weaponlab_weapon_slot"))
    pose_io.apply_timing(scene, next(clip for clip in clips if clip["id"] == default_id))
    scene.frame_set(0)

    progress("Adding preview meshes")
    warnings = list(rig_doc.get("warnings", []))
    materials = {
        "weapon": preview_material("Weapon", (0.22, 0.25, 0.27, 1), metallic=0.1),
        "arms": preview_material("Sleeve", (0.11, 0.16, 0.18, 1)),
        "gloves": preview_material("Glove", (0.045, 0.058, 0.065, 1)),
        "body": preview_material("Body", (0.13, 0.17, 0.19, 1)),
    }
    previews = rig_doc.get("previews", {})
    attached = []
    # The third-person weapon shows the same mesh as the first-person one (the game uses one model for both).
    bindings = (("arms", arms, "arms", None), ("gloves", arms, "gloves", None), ("weapon", weapon, "weapon", None),
                ("body", body, "body", None), ("weapon", world_weapon, "weapon", "third person weapon"))
    for kind, target, material, label in bindings:
        if kind not in previews or target is None:
            continue
        try:
            meshes = bind_preview(input_dir / previews[kind], target, kind, materials[material], label)
            if target in (body, world_weapon):
                third_person(*meshes, bpy.data.objects.get("Preview " + (label or kind)))
            attached.append(label or kind)
        except Exception as error:  # Previews are optional; keep building.
            warnings.append((label or kind) + " preview: " + str(error))

    # The glTF importer leaves an empty helper collection behind.
    for collection in [c for c in bpy.data.collections if c.name.startswith("glTF_not_exported")]:
        bpy.data.collections.remove(collection)

    collections = {}
    names = [("rigs", "01 - Edit rigs"), ("preview", pose_io.FIRST_PERSON_COLLECTIONS[1]), ("studio", "03 - Studio")]
    if body:
        names.append(("world", pose_io.THIRD_PERSON_COLLECTION))
    for key, name in names:
        collection = bpy.data.collections.new(name)
        scene.collection.children.link(collection)
        collections[key] = collection
    if body:
        stage_body(scene, body)
    stage(scene, arms, weapon, collections)
    configure_views()
    pose_io.show_view(bpy.context.view_layer, False)  # The default clip is first person.

    # The add-on finds the sandbox beside the .blend (sandbox/blender/<weapon>.blend), so sandboxes can move;
    # "sandbox" is where it was built, for a .blend saved somewhere else.
    scene["weaponlab_meta"] = json.dumps({
        "schema": BLEND_SCHEMA,
        "weaponId": weapon_id,
        "sandbox": str(input_dir.resolve().parent.parent),
        "label": rig_doc["label"],
        "arms": arms.name,
        "weapon": weapon.name if weapon else None,
        "body": body.name if body else None,
        "worldWeapon": world_weapon.name if world_weapon else None,
        "actions": {clip_id: action.name for clip_id, action in actions.items()},
        "defaultClip": default_id,
    })

    bpy.ops.object.select_all(action="DESELECT")
    arms.select_set(True)
    bpy.context.view_layer.objects.active = arms
    bpy.data.orphans_purge(do_local_ids=True, do_linked_ids=False, do_recursive=True)

    # Save beside the target, then replace it, so a failed save never leaves a broken file.
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(output.stem + ".building.blend")
    bpy.ops.wm.save_as_mainfile(filepath=str(temporary), compress=True)
    os.replace(temporary, output)

    return {
        "weaponId": weapon_id,
        "blend": str(output),
        "actions": len(actions),
        "markers": markers,
        "armsBones": len(arms_skeleton["bones"]),
        "weaponBones": len(weapon_skeleton["bones"]) if weapon_skeleton else 0,
        "bodyBones": len(body_skeleton["bones"]) if body else 0,
        "thirdPersonClips": sum(1 for clip in clips if pose_io.is_world(clip)),
        "previews": attached,
        "warnings": warnings,
        "blender": bpy.app.version_string,
        "carried": carried,
    }


def main():
    arguments = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    if len(arguments) not in (2, 3):
        raise RuntimeError("Usage: blender --background --python build_weapon.py -- <input folder> <output.blend> [<previous .blend to carry keys from>]")
    if bpy.app.version < (4, 4, 0):
        raise RuntimeError("Blender 4.4 or newer is required (slotted Actions); this is " + bpy.app.version_string)
    previous = Path(arguments[2]).resolve() if len(arguments) == 3 else None
    result = build(Path(arguments[0]).resolve(), Path(arguments[1]).resolve(), previous)
    print("WEAPONLAB_RESULT " + json.dumps(result), flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        traceback.print_exc()
        print("WEAPONLAB_ERROR " + str(error).replace("\n", " "), flush=True)
        sys.exit(1)
