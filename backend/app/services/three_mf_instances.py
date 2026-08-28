"""Create additional object instances in a Bambu/Orca project 3MF.

The slicer CLI can arrange objects that already exist, but it has no exposed
"copies" option.  Bambu/Orca project files describe each printable instance
twice: as a ``build/item`` in ``3D/3dmodel.model`` and as a
``model_instance`` in ``Metadata/model_settings.config``.  This module keeps
those two views in sync before the existing ``--arrange`` slicing pass.
"""

from __future__ import annotations

import io
from copy import deepcopy
from xml.etree import ElementTree as ET
from zipfile import ZIP_DEFLATED, BadZipFile, ZipFile

_MODEL_PATH = "3D/3dmodel.model"
_SETTINGS_PATH = "Metadata/model_settings.config"


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _children(element: ET.Element, name: str) -> list[ET.Element]:
    return [child for child in element if _local_name(child.tag) == name]


def _metadata_value(element: ET.Element, key: str) -> str | None:
    for child in _children(element, "metadata"):
        if child.get("key") == key:
            return child.get("value")
    return None


def _set_metadata_value(element: ET.Element, key: str, value: str) -> None:
    for child in _children(element, "metadata"):
        if child.get("key") == key:
            child.set("value", value)
            return


def _register_namespaces(xml: bytes) -> None:
    """Keep the source's namespace prefixes when ElementTree serialises it."""
    for _event, value in ET.iterparse(io.BytesIO(xml), events=("start-ns",)):
        prefix, uri = value
        try:
            ET.register_namespace(prefix, uri)
        except ValueError:
            # Reserved prefixes (ns0, ns1, ...) are cosmetic; ElementTree can
            # choose a safe replacement without changing the document model.
            pass


def duplicate_plate_instances(project_bytes: bytes, *, copies: int, plate: int = 1) -> bytes:
    """Return a project 3MF with ``copies`` of every instance on ``plate``.

    ``copies`` is the desired total, not the number to add.  All new build
    items deliberately start at the source transform: the caller must enable
    the slicer's arrange pass, which separates and packs them for the target
    bed.
    """
    if copies < 1:
        raise ValueError("copies must be at least 1")
    if copies == 1:
        return project_bytes
    if plate < 1:
        raise ValueError("copies on plate requires one specific plate")

    try:
        with ZipFile(io.BytesIO(project_bytes), "r") as source:
            names = source.namelist()
            if _MODEL_PATH not in names or _SETTINGS_PATH not in names:
                raise ValueError("source is not a Bambu/Orca project 3MF")
            entries = [(info, source.read(info.filename)) for info in source.infolist()]
    except BadZipFile as exc:
        raise ValueError("source is not a valid 3MF archive") from exc

    entry_map = {info.filename: data for info, data in entries}
    model_xml = entry_map[_MODEL_PATH]
    settings_xml = entry_map[_SETTINGS_PATH]
    _register_namespaces(model_xml)
    _register_namespaces(settings_xml)

    try:
        model_root = ET.fromstring(model_xml)
        settings_root = ET.fromstring(settings_xml)
    except ET.ParseError as exc:
        raise ValueError("source 3MF contains invalid project XML") from exc

    plates = [element for element in settings_root.iter() if _local_name(element.tag) == "plate"]
    target_plate = next((item for item in plates if _metadata_value(item, "plater_id") == str(plate)), None)
    if target_plate is None and plate == 1 and len(plates) == 1:
        target_plate = plates[0]
    if target_plate is None:
        raise ValueError(f"plate {plate} was not found in the source 3MF")

    source_instances = _children(target_plate, "model_instance")
    if not source_instances:
        raise ValueError(f"plate {plate} has no model instances to copy")

    build = next((element for element in model_root.iter() if _local_name(element.tag) == "build"), None)
    if build is None:
        raise ValueError("source 3MF has no build section")
    build_items = _children(build, "item")

    items_by_object: dict[str, list[ET.Element]] = {}
    for item in build_items:
        object_id = item.get("objectid")
        if object_id:
            items_by_object.setdefault(object_id, []).append(item)

    used_instance_ids: dict[str, set[int]] = {}
    identify_values: list[int] = []
    for instance in [element for element in settings_root.iter() if _local_name(element.tag) == "model_instance"]:
        object_id = _metadata_value(instance, "object_id")
        if not object_id:
            continue
        try:
            instance_id = int(_metadata_value(instance, "instance_id") or "0")
        except ValueError:
            instance_id = 0
        used_instance_ids.setdefault(object_id, set()).add(instance_id)
        try:
            identify_values.append(int(_metadata_value(instance, "identify_id") or ""))
        except ValueError:
            pass
    next_identify_id = max(identify_values, default=0) + 1

    for source_instance in source_instances:
        object_id = _metadata_value(source_instance, "object_id")
        if not object_id or object_id not in items_by_object:
            raise ValueError(f"model instance {object_id or '?'} has no matching build item")
        try:
            source_instance_id = int(_metadata_value(source_instance, "instance_id") or "0")
        except ValueError:
            source_instance_id = 0
        object_items = items_by_object[object_id]
        template_item = object_items[source_instance_id] if source_instance_id < len(object_items) else object_items[0]

        for _copy_index in range(1, copies):
            used = used_instance_ids.setdefault(object_id, set())
            new_instance_id = 0
            while new_instance_id in used:
                new_instance_id += 1
            used.add(new_instance_id)

            new_instance = deepcopy(source_instance)
            _set_metadata_value(new_instance, "instance_id", str(new_instance_id))
            if _metadata_value(new_instance, "identify_id") is not None:
                _set_metadata_value(new_instance, "identify_id", str(next_identify_id))
                next_identify_id += 1
            target_plate.append(new_instance)
            build.append(deepcopy(template_item))

    replacements = {
        _MODEL_PATH: ET.tostring(model_root, encoding="utf-8", xml_declaration=True),
        _SETTINGS_PATH: ET.tostring(settings_root, encoding="utf-8", xml_declaration=True),
    }
    output = io.BytesIO()
    with ZipFile(output, "w", compression=ZIP_DEFLATED) as destination:
        for info, data in entries:
            destination.writestr(info, replacements.get(info.filename, data))
    return output.getvalue()
