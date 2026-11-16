"""Engine pose <-> Blender rig conversion shared by the build script and the add-on.

Pose files (rig package poses, build references and exported edits) share one
format: {"arms": {"frames", "tracks", "skeleton", "values"}, "weapon": {...}}
where values is a flat list of frames x tracks x [tx, ty, tz, qx, qy, qz, qw, scale]
in engine parent-local space.

This module must not use relative imports: build_weapon.py loads it as a
plain module, the add-on as part of its package.
"""
import gzip
import json

import bpy
import numpy as np
from mathutils import Matrix, Quaternion, Vector

# Engine root-motion children are rotated relative to Blender's convention.
AXIS_FIX = Quaternion((0.5, 0.5, 0.5, 0.5))
CONVERSION = AXIS_FIX.inverted().to_matrix().to_4x4()
TO_ENGINE = CONVERSION.inverted()


# ---- Files ----

def read_poses(path):
    return json.loads(gzip.decompress(path.read_bytes()))


def write_poses(path, document):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_bytes(gzip.compress(json.dumps(document, separators=(",", ":")).encode("utf-8")))
    temporary.replace(path)


# ---- Skeleton helpers ----

def parent_name(skeleton, bone):
    return skeleton["bones"][bone["parent"]]["name"] if bone["parent"] >= 0 else None


def needs_conversion(skeleton, bone, primary):
    return primary and parent_name(skeleton, bone) == "root_motion"


def pose_matrix(translation, rotation_xyzw, scale):
    x, y, z, w = rotation_xyzw
    return Matrix.LocRotScale(Vector(translation), Quaternion((w, x, y, z)), Vector((scale, scale, scale)))


def rest_locals(skeleton, primary):
    """Rest pose of each bone relative to its parent, in Blender space."""
    locals_ = []
    for bone in skeleton["bones"]:
        local = pose_matrix(bone["translation"], bone["rotation"], bone["scale"])
        if needs_conversion(skeleton, bone, primary):
            local = CONVERSION @ local
        locals_.append(local)
    return locals_


def worlds(skeleton, locals_):
    result = {}
    for bone, local in zip(skeleton["bones"], locals_):
        parent = parent_name(skeleton, bone)
        result[bone["name"]] = result[parent] @ local if parent else local.copy()
    return result


# ---- Engine poses -> keys ----

def frame_poses(stream):
    """Reshape the flat pose list to [frame][track] -> 8 floats."""
    return np.asarray(stream["values"], dtype=np.float64).reshape(stream["frames"], stream["tracks"], 8)


def frame_locals(stream, skeleton, primary, additive_base=None):
    """Parent-local matrices per frame in Blender space. Additive clips are
    composed over the first frame of the default clip so they show a full pose."""
    poses = frame_poses(stream)
    if poses.shape[1] != len(skeleton["bones"]):
        raise RuntimeError("Track count does not match skeleton " + skeleton["path"])
    base = frame_poses(additive_base)[0] if additive_base is not None else None
    frames = []
    for frame in poses:
        locals_ = []
        for index, (bone, pose) in enumerate(zip(skeleton["bones"], frame)):
            t, q, s = pose[0:3], pose[3:7], pose[7]
            if base is not None:
                bt, bq, bs = base[index][0:3], base[index][3:7], base[index][7]
                rotation = Quaternion((q[3], q[0], q[1], q[2])) @ Quaternion((bq[3], bq[0], bq[1], bq[2]))
                # Additive scale is a delta (zero = unchanged), not a multiplier.
                matrix = Matrix.LocRotScale(Vector(bt) + Vector(t), rotation, Vector([bs + s] * 3))
            else:
                matrix = pose_matrix(t, q, s)
            if needs_conversion(skeleton, bone, primary):
                matrix = CONVERSION @ matrix
            locals_.append(matrix)
        frames.append(locals_)
    return frames


def channel_values(rig, skeleton, frames, skip=()):
    """Pose-bone basis matrices per bone, per frame. skip: bones keyed some other way (props, see prop_values)."""
    bones = [bone for bone in skeleton["bones"] if bone["name"] not in skip]
    result = {bone["name"]: [] for bone in bones}
    for locals_ in frames:
        posed = worlds(skeleton, locals_)
        for bone in bones:
            data_bone = rig.data.bones[bone["name"]]
            parent = data_bone.parent
            result[bone["name"]].append(data_bone.convert_local_to_pose(
                posed[bone["name"]], data_bone.matrix_local,
                parent_matrix=posed[parent.name] if parent else Matrix.Identity(4),
                parent_matrix_local=parent.matrix_local if parent else Matrix.Identity(4),
                invert=True))
    return result


def free_bones(skeleton):
    """Bones ticked "Keep apart from the gun" in Weapon Lab (rig.json freeBones, 0.23), in skeleton order."""
    names = set(skeleton.get("freeBones") or ()) if skeleton else set()
    return [bone["name"] for bone in skeleton["bones"] if bone["name"] in names] if names else []


# A prop (e.g. a spare magazine in the other hand) is a weapon-model bone that should not move with the gun. The whole Weapon
# rig rides on wpn, so in the .blend its Blender parent is this hidden bone, which copies the Arms rig object
# and so cancels the gun's motion. The game skeleton keeps the prop under the gun's root; sample() writes it
# relative to that, so the game shows what Blender shows.
PROP_ANCHOR = "Props anchor - stays with the arms"


def prop_places(arms_skeleton, arms_frames, weapon_skeleton, weapon_frames, names):
    """Where the clip's own motion puts each prop, per frame, in Arms-rig space: wpn x its place on the
    weapon skeleton (as the game carries it). weapon_frames None = the weapon skeleton's rest."""
    rest = rest_locals(weapon_skeleton, primary=False)
    result = {name: [] for name in names}
    for index, arms_locals in enumerate(arms_frames):
        mount = worlds(arms_skeleton, arms_locals)["wpn"]
        posed = worlds(weapon_skeleton, weapon_frames[min(index, len(weapon_frames) - 1)] if weapon_frames else rest)
        for name in names:
            result[name].append(mount @ posed[name])
    return result


def prop_hold(default_poses, arms_skeleton, weapon_skeleton, names):
    """Where props hold still in every clip, in Arms-rig space: their place on the default clip's first frame."""
    arms_first = frame_locals(default_poses["arms"], arms_skeleton, True)[:1]
    weapon_first = frame_locals(default_poses["weapon"], weapon_skeleton, False)[:1] if "weapon" in default_poses else None
    return {name: places[0] for name, places in prop_places(arms_skeleton, arms_first, weapon_skeleton, weapon_first, names).items()}


def prop_values(weapon, arms_skeleton, arms_frames, places):
    """Pose-bone basis matrices for the Weapon rig's props, per frame, placing each where places says
    ({name: [Arms-rig-space matrix per frame]}). The anchor sits on the Arms rig object; the Weapon rig rides on
    wpn, so seen from the Weapon rig the anchor is at inverse(wpn) on each frame."""
    result = {name: [] for name in places}
    for index, arms_locals in enumerate(arms_frames):
        anchor_pose = worlds(arms_skeleton, arms_locals)["wpn"].inverted()
        for name, matrices in places.items():
            data_bone = weapon.data.bones[name]
            result[name].append(data_bone.convert_local_to_pose(
                anchor_pose @ matrices[index], data_bone.matrix_local,
                parent_matrix=anchor_pose, parent_matrix_local=data_bone.parent.matrix_local, invert=True))
    return result


def held(hold, frame_count):
    """A hold as places: the same place on every frame. Every frame stays keyed: Auto Key only replaces keys."""
    return {name: [matrix] * frame_count for name, matrix in hold.items()}


def clear_bones(bag, names):
    """Removes these bones' curves from a channelbag."""
    prefixes = tuple('pose.bones[' + json.dumps(name) + '].' for name in names)
    for curve in [curve for curve in bag.fcurves if curve.data_path.startswith(prefixes)]:
        bag.fcurves.remove(curve)



def _keyframe_point(curve, frame):
    """The key on an F-curve at an integer scene frame, or None."""
    return next((point for point in curve.keyframe_points if abs(point.co.x - frame) < 1e-4), None)


def _bone_channel_curves(bag, bone_name):
    """Location, quaternion rotation and scale curves for one pose bone."""
    prefix = 'pose.bones[' + json.dumps(bone_name) + '].'
    result = {}
    for prop, width in (("location", 3), ("rotation_quaternion", 4), ("scale", 3)):
        curves = [bag.fcurves.find(prefix + prop, index=index) for index in range(width)]
        result[prop] = curves if all(curves) else None
    return result



def keyed_pose_snapshot(bag, frame, bone_names):
    """Read the exact keyed transform values at a frame, not evaluated pose values."""
    snapshot = {}
    for bone_name in bone_names:
        channels = _bone_channel_curves(bag, bone_name)
        values = {}
        complete = True
        for prop, curves in channels.items():
            if curves is None:
                complete = False
                break
            points = [_keyframe_point(curve, frame) for curve in curves]
            if not all(points):
                complete = False
                break
            values[prop] = tuple(point.co.y for point in points)
        if complete:
            snapshot[bone_name] = values
    return snapshot

def propagate_bone_delta(bag, bone_name, frame, previous, current):
    """Apply one pose-bone transform delta to its immediately following keyed pose.

    Location uses an offset, rotation uses a quaternion delta, and scale uses a
    ratio (with an additive fallback when the old scale is zero). Only channels
    changed at the current frame are touched; no other keyframe is modified.
    """
    channels = _bone_channel_curves(bag, bone_name)
    all_curves = [curve for curves in channels.values() if curves for curve in curves]
    frames = sorted({float(point.co.x) for curve in all_curves for point in curve.keyframe_points
                     if point.co.x > frame + 1e-4})
    if not frames:
        return None
    next_frame = frames[0]
    changed = False

    for prop in ("location", "rotation_quaternion", "scale"):
        old_values = previous[prop]
        new_values = current[prop]
        if len(old_values) != len(new_values) or all(abs(a - b) < 1e-6 for a, b in zip(old_values, new_values)):
            continue
        curves = channels[prop]
        if curves is None:
            continue
        current_points = [_keyframe_point(curve, frame) for curve in curves]
        next_points = [_keyframe_point(curve, next_frame) for curve in curves]
        if not all(current_points) or not all(next_points):
            continue
        target = [point.co.y for point in next_points]

        if prop == "location":
            result = [value + (new - old) for value, old, new in zip(target, old_values, new_values)]
        elif prop == "rotation_quaternion":
            old_q = Quaternion(old_values)
            new_q = Quaternion(new_values)
            if old_q.dot(new_q) < 0:
                new_q.negate()
            delta = new_q @ old_q.inverted()
            target_q = Quaternion(target)
            result_q = delta @ target_q
            result_q.normalize()
            if result_q.dot(target_q) < 0:
                result_q.negate()
            result = list(result_q)
        else:
            result = []
            for value, old, new in zip(target, old_values, new_values):
                result.append(value * (new / old) if abs(old) > 1e-8 else value + (new - old))

        for point, value in zip(next_points, result):
            point.co.y = value
        for curve in curves:
            curve.update()
        changed = True

    return next_frame if changed else None


def new_curve(bag, data_path, index, group_name):
    """Creates an F-curve in a named group using the Blender 4.4+ channelbag API."""
    curve = bag.fcurves.new(data_path, index=index)
    group = bag.groups.get(group_name)
    if group is None:
        group = bag.groups.new(group_name)
    curve.group = group
    return curve


def key_channels(bag, bases):
    """Writes one linear key per frame for every bone's location, rotation and scale."""
    linear = bpy.types.Keyframe.bl_rna.properties["interpolation"].enum_items["LINEAR"].value
    for name, matrices in bases.items():
        decomposed = [matrix.decompose() for matrix in matrices]
        quaternions = []
        for _, quaternion, _ in decomposed:
            # Keep quaternions on one hemisphere so interpolation takes the short way.
            if quaternions and quaternion.dot(quaternions[-1]) < 0:
                quaternion.negate()
            quaternions.append(quaternion.copy())
        channels = [
            ("location", 3, [list(item[0]) for item in decomposed]),
            ("rotation_quaternion", 4, [list(q) for q in quaternions]),
            ("scale", 3, [list(item[2]) for item in decomposed]),
        ]
        for prop, width, rows in channels:
            for component in range(width):
                series = np.asarray([row[component] for row in rows], dtype=np.float32)
                coordinates = np.column_stack((np.arange(len(series)), series)).astype(np.float32).ravel()
                curve = new_curve(bag, 'pose.bones[' + json.dumps(name) + '].' + prop, component, name)
                curve.keyframe_points.add(len(series))
                curve.keyframe_points.foreach_set("co", coordinates)
                curve.keyframe_points.foreach_set("interpolation", np.full(len(series), linear, dtype=np.int32))
                curve.update()


def clear_channelbag(bag):
    for curve in list(bag.fcurves):
        bag.fcurves.remove(curve)


def same_rest(new_bone, old_bone):
    """A bone whose keys mean the same on both rigs: same Blender parent and the same rest (keys are rest-relative)."""
    new_parent = new_bone.parent.name if new_bone.parent else None
    old_parent = old_bone.parent.name if old_bone.parent else None
    if new_parent != old_parent:
        return False
    return all(abs(a - b) < 1e-4 for row_new, row_old in zip(new_bone.matrix_local, old_bone.matrix_local) for a, b in zip(row_new, row_old))


def copy_curve(bag, curve, group):
    """A copy of an F-Curve's keys (positions, handles, interpolation, easing) in another channelbag."""
    count = len(curve.keyframe_points)
    copy = new_curve(bag, curve.data_path, curve.array_index, group)
    copy.keyframe_points.add(count)

    def pairs(name):
        values = np.empty(count * 2, dtype=np.float32)
        curve.keyframe_points.foreach_get(name, values)
        copy.keyframe_points.foreach_set(name, values)

    pairs("co")
    for name in ("interpolation", "handle_left_type", "handle_right_type", "easing"):
        values = np.empty(count, dtype=np.int32)
        curve.keyframe_points.foreach_get(name, values)
        copy.keyframe_points.foreach_set(name, values)
    copy.extrapolation = curve.extrapolation
    copy.update()  # Sorts and recalculates auto handles...
    pairs("handle_left")  # ...then the saved handles exactly.
    pairs("handle_right")


# ---- Views (0.26) ----

# Third-person clips: rig.json clips with "view": "world", and the Actions built from them (action["weaponlab_view"]).
# Their primary stream is the body (worldmodel.vnmskel), and their weapon stream plays on a second weapon rig that
# rides the body's wpn. Pose documents keep the stream names "arms" (primary) and "weapon", and Actions the slot
# keys weaponlab_arms_slot / weaponlab_weapon_slot, so everything that samples or keys a clip works in both views.
WORLD = "world"


def is_world(item):
    """A third-person clip: a rig.json clip or an Action built from one."""
    return (item.get("view") if isinstance(item, dict) else item.get("weaponlab_view")) == WORLD


def skeletons_of(rig_doc, world):
    """(primary skeleton, weapon skeleton, primary flag) of a view. The body is not rotated like the viewmodel's
    root-motion children (needs_conversion): the engine's world model already stands Z-up, as Blender does."""
    skeletons = rig_doc["skeletons"]
    if world:
        return skeletons.get("body"), skeletons.get("worldWeapon"), False
    return skeletons["arms"], skeletons.get("weapon"), True


# Where the 3D views look per view (third person?): (location, distance). The arms sit around the player camera,
# the body stands on the floor at the origin.
VIEW_FRAMING = {False: ((0.0, -15.0, -3.0), 48.0), True: ((0.0, 0.0, 38.0), 150.0)}

FIRST_PERSON_COLLECTIONS = ("01 - Edit rigs", "02 - Preview meshes")
THIRD_PERSON_COLLECTION = "04 - Third person"


def show_view(view_layer, world):
    """Shows one view's rigs and preview meshes and hides the other's (the outliner's eye icons). A rig is sampled
    only while its view shows: Blender may skip evaluating what is hidden."""
    def walk(layer):
        for child in layer.children:
            yield child
            yield from walk(child)

    for layer in walk(view_layer.layer_collection):
        if layer.name == THIRD_PERSON_COLLECTION:
            layer.hide_viewport = not world
        elif layer.name in FIRST_PERSON_COLLECTIONS:
            layer.hide_viewport = world


def scene_rigs(scene, data, world):
    """(primary rig, weapon rig) of a view, by the names in the scene's Weapon Lab data (None where missing)."""
    if world:
        return scene.objects.get(data.get("body") or ""), scene.objects.get(data.get("worldWeapon") or "")
    return scene.objects.get(data["arms"]), scene.objects.get(data["weapon"]) if data.get("weapon") else None


def bag_of(action, key):
    """The action's channelbag for a slot key (weaponlab_arms_slot / weaponlab_weapon_slot), or None."""
    if key not in action or not action.layers:
        return None
    return action.layers[0].strips[0].channelbag(slot_for(action, key))


def key_document(rig_doc, arms, weapon, action, document, names=None):
    """Keys a pose document on this weapon's skeletons (the rig.json ones, full poses as sample() writes) onto a
    clip action, one key per frame: every bone, or only names = {"arms": [...], "weapon": [...]}. Props are placed
    under their anchor like the build does. Returns the bones keyed per stream (0.25). For a third-person clip
    (0.26) arms and weapon are the Body rig and its weapon rig, and the skeletons the body's."""
    arms_skeleton, weapon_skeleton, primary = skeletons_of(rig_doc, is_world(action))
    arms_frames = frame_locals(document["arms"], arms_skeleton, primary)
    keyed = {}
    wanted = (names or {}).get("arms")
    bag = bag_of(action, "weaponlab_arms_slot")
    values = {name: matrices for name, matrices in channel_values(arms, arms_skeleton, arms_frames).items() if wanted is None or name in wanted}
    clear_bones(bag, values.keys())
    key_channels(bag, values)
    keyed["arms"] = sorted(values)
    if weapon and weapon_skeleton and "weapon" in document and bag_of(action, "weaponlab_weapon_slot") is not None:
        props = free_bones(weapon_skeleton)
        weapon_frames = frame_locals(document["weapon"], weapon_skeleton, False)
        wanted = (names or {}).get("weapon")
        bag = bag_of(action, "weaponlab_weapon_slot")
        values = {name: matrices for name, matrices in channel_values(weapon, weapon_skeleton, weapon_frames, skip=props).items() if wanted is None or name in wanted}
        moved_props = [name for name in props if wanted is None or name in wanted]
        if moved_props:
            places = prop_places(arms_skeleton, arms_frames, weapon_skeleton, weapon_frames, moved_props)
            values.update(prop_values(weapon, arms_skeleton, arms_frames, places))
        clear_bones(bag, values.keys())
        key_channels(bag, values)
        keyed["weapon"] = sorted(values)
    return keyed


# ---- Rigs -> engine poses ----

def sample(scene, rig, skeleton, primary, frame_count):
    """Samples the rig's evaluated pose on frames 0..frame_count-1 as an engine pose stream.

    Constraints are evaluated; wpnHand_* values are replaced by Weapon Lab's hand glue on export.
    Scale is averaged to the uniform scale the engine stores. Each bone is sampled relative to its game
    parent, which is also its Blender parent except for props (their Blender parent is PROP_ANCHOR).
    """
    names = [bone["name"] for bone in skeleton["bones"]]
    parents = [parent_name(skeleton, bone) for bone in skeleton["bones"]]
    for name, expected_parent in zip(names, parents):
        actual = rig.pose.bones.get(name)
        actual_parent = actual.parent.name if actual is not None and actual.parent else None
        if actual is None or (actual_parent != expected_parent and not (expected_parent and actual_parent == PROP_ANCHOR)):
            raise RuntimeError("Bone missing or reparented: " + name)

    convert = [needs_conversion(skeleton, bone, primary) for bone in skeleton["bones"]]
    values = []
    previous = scene.frame_current
    try:
        for frame in range(frame_count):
            scene.frame_set(frame)
            evaluated = rig.evaluated_get(bpy.context.evaluated_depsgraph_get())
            for name, parent, to_engine in zip(names, parents, convert):
                posed = evaluated.pose.bones[name]
                local = evaluated.pose.bones[parent].matrix.inverted_safe() @ posed.matrix if parent else posed.matrix.copy()
                if to_engine:
                    local = TO_ENGINE @ local
                translation, rotation, scale = local.decompose()
                rotation.normalize()
                values.extend((translation.x, translation.y, translation.z, rotation.x, rotation.y, rotation.z, rotation.w, (scale.x + scale.y + scale.z) / 3))
    finally:
        scene.frame_set(previous)
    # Bone names and parents (0.25) let Weapon Lab and other weapons map this sample by name.
    return {"frames": frame_count, "tracks": len(names), "skeleton": skeleton["path"], "values": values,
            "bones": names, "parents": parents}


# ---- Actions ----

def slot_for(action, key):
    return next(slot for slot in action.slots if slot.identifier == action[key])


def assign(rig, action, slot):
    rig.animation_data_create()
    if rig.animation_data.action != action:
        rig.animation_data.action = action
    if rig.animation_data.action_slot != slot:
        rig.animation_data.action_slot = slot


def fps_of(clip):
    return (clip["frames"] - 1) / clip["duration"] if clip["duration"] else 30.0


def apply_timing(scene, clip):
    """Shows the clip at its own speed on frames 0..frames-1."""
    scene.frame_start = 0
    scene.frame_end = clip["frames"] - 1
    scene.render.fps = 30
    scene.render.fps_base = 30 / max(0.01, fps_of(clip))
