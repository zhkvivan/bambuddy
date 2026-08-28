"""Merge several Bambu/Orca project 3MFs onto the first project's plate."""

from __future__ import annotations

import io
from copy import deepcopy
from uuid import uuid4
from xml.etree import ElementTree as ET
from zipfile import ZIP_DEFLATED, BadZipFile, ZipFile

from backend.app.services.three_mf_instances import _children, _local_name, _metadata_value, _register_namespaces

_MODEL = "3D/3dmodel.model"
_SETTINGS = "Metadata/model_settings.config"
_RELS = "3D/_rels/3dmodel.model.rels"


def _read_project(data: bytes) -> tuple[list[tuple[object, bytes]], dict[str, bytes]]:
    try:
        with ZipFile(io.BytesIO(data)) as archive:
            entries = [(info, archive.read(info.filename)) for info in archive.infolist()]
    except BadZipFile as exc:
        raise ValueError("one of the selected files is not a valid 3MF") from exc
    mapped = {info.filename: payload for info, payload in entries}
    if _MODEL not in mapped or _SETTINGS not in mapped:
        raise ValueError("all selected files must be Bambu/Orca project 3MF files")
    return entries, mapped


def merge_projects_on_plate(projects: list[bytes], *, plate: int = 1) -> bytes:
    """Place every object from ``projects`` on one plate in a new project.

    The first project supplies print/project settings. Geometry, object metadata,
    instances and relationship entries from later projects are imported with
    collision-free ids and paths. All transforms are intentionally preserved;
    the caller should ask Bambu Studio to auto-arrange the merged project.
    """
    if len(projects) < 2:
        raise ValueError("select at least two 3MF files")

    base_entries, base_map = _read_project(projects[0])
    _register_namespaces(base_map[_MODEL])
    _register_namespaces(base_map[_SETTINGS])
    model_root = ET.fromstring(base_map[_MODEL])
    settings_root = ET.fromstring(base_map[_SETTINGS])
    resources = next(e for e in model_root if _local_name(e.tag) == "resources")
    build = next(e for e in model_root if _local_name(e.tag) == "build")
    plates = [e for e in settings_root.iter() if _local_name(e.tag) == "plate"]
    target_plate = next((p for p in plates if _metadata_value(p, "plater_id") == str(plate)), None)
    if target_plate is None and plate == 1 and len(plates) == 1:
        target_plate = plates[0]
    if target_plate is None:
        raise ValueError(f"plate {plate} was not found in the first 3MF")
    assemble = next((e for e in settings_root.iter() if _local_name(e.tag) == "assemble"), None)
    if assemble is None:
        assemble = ET.SubElement(settings_root, "assemble")

    used_ids = {int(e.get("id")) for e in resources if e.get("id", "").isdigit()}
    next_id = max(used_ids, default=0) + 1
    identify_ids = []
    for instance in settings_root.iter():
        if _local_name(instance.tag) == "model_instance":
            try:
                identify_ids.append(int(_metadata_value(instance, "identify_id") or ""))
            except ValueError:
                pass
    next_identify = max(identify_ids, default=0) + 1
    additions: dict[str, bytes] = {}

    rel_ns = "http://schemas.openxmlformats.org/package/2006/relationships"
    ET.register_namespace("", rel_ns)
    rel_root = ET.fromstring(base_map.get(_RELS, f'<Relationships xmlns="{rel_ns}"/>'.encode()))

    for source_index, project in enumerate(projects[1:], start=2):
        _entries, mapped = _read_project(project)
        _register_namespaces(mapped[_MODEL])
        _register_namespaces(mapped[_SETTINGS])
        src_model = ET.fromstring(mapped[_MODEL])
        src_settings = ET.fromstring(mapped[_SETTINGS])
        src_resources = next(e for e in src_model if _local_name(e.tag) == "resources")
        src_build = next(e for e in src_model if _local_name(e.tag) == "build")
        id_map: dict[str, str] = {}
        for obj in _children(src_resources, "object"):
            old_id = obj.get("id")
            if not old_id:
                continue
            new_obj = deepcopy(obj)
            new_id = str(next_id)
            next_id += 1
            id_map[old_id] = new_id
            new_obj.set("id", new_id)
            for attr in list(new_obj.attrib):
                if _local_name(attr).lower() == "uuid":
                    new_obj.set(attr, str(uuid4()))
            for component in new_obj.iter():
                if _local_name(component.tag) != "component":
                    continue
                for attr, value in list(component.attrib.items()):
                    if _local_name(attr).lower() == "uuid":
                        component.set(attr, str(uuid4()))
                    if _local_name(attr) == "path":
                        old_path = value.lstrip("/")
                        if old_path not in mapped:
                            raise ValueError(f"selected 3MF is missing geometry entry {old_path}")
                        geometry_root = ET.fromstring(mapped[old_path])
                        geometry_resources = next(
                            (e for e in geometry_root if _local_name(e.tag) == "resources"), None
                        )
                        source_object_id = component.get("objectid")
                        geometry_object = next(
                            (e for e in _children(geometry_resources, "object") if e.get("id") == source_object_id),
                            None,
                        ) if geometry_resources is not None else None
                        if geometry_object is None:
                            raise ValueError(f"selected 3MF has no geometry object {source_object_id or '?'}")
                        imported_object = deepcopy(geometry_object)
                        imported_id = str(next_id)
                        next_id += 1
                        imported_object.set("id", imported_id)
                        for element in imported_object.iter():
                            for uuid_attr in list(element.attrib):
                                if _local_name(uuid_attr).lower() == "uuid":
                                    element.set(uuid_attr, str(uuid4()))
                        resources.append(imported_object)
                        component.set("objectid", imported_id)
                        del component.attrib[attr]
            resources.append(new_obj)

        for item in _children(src_build, "item"):
            old_id = item.get("objectid")
            if old_id not in id_map:
                continue
            new_item = deepcopy(item)
            new_item.set("objectid", id_map[old_id])
            for attr in list(new_item.attrib):
                if _local_name(attr).lower() == "uuid":
                    new_item.set(attr, str(uuid4()))
            build.append(new_item)

        src_objects = {o.get("id"): o for o in _children(src_settings, "object")}
        for old_id, new_id in id_map.items():
            if old_id in src_objects:
                new_cfg_obj = deepcopy(src_objects[old_id])
                new_cfg_obj.set("id", new_id)
                for element in new_cfg_obj.iter():
                    if "uuid" in element.attrib:
                        element.set("uuid", str(uuid4()))
                settings_root.insert(len(_children(settings_root, "object")), new_cfg_obj)

        src_plates = [e for e in src_settings.iter() if _local_name(e.tag) == "plate"]
        for src_plate in src_plates:
            for instance in _children(src_plate, "model_instance"):
                old_id = _metadata_value(instance, "object_id")
                if old_id not in id_map:
                    continue
                new_instance = deepcopy(instance)
                for metadata in _children(new_instance, "metadata"):
                    if metadata.get("key") == "object_id":
                        metadata.set("value", id_map[old_id])
                    elif metadata.get("key") == "identify_id":
                        metadata.set("value", str(next_identify))
                        next_identify += 1
                target_plate.append(new_instance)

        src_assemble = next((e for e in src_settings.iter() if _local_name(e.tag) == "assemble"), None)
        if src_assemble is not None:
            for row in _children(src_assemble, "assemble_item"):
                old_id = row.get("object_id")
                if old_id in id_map:
                    new_row = deepcopy(row)
                    new_row.set("object_id", id_map[old_id])
                    assemble.append(new_row)

    replacements = {
        _MODEL: ET.tostring(model_root, encoding="utf-8", xml_declaration=True),
        _SETTINGS: ET.tostring(settings_root, encoding="utf-8", xml_declaration=True),
        _RELS: ET.tostring(rel_root, encoding="utf-8", xml_declaration=True),
    }
    out = io.BytesIO()
    with ZipFile(out, "w", compression=ZIP_DEFLATED) as destination:
        existing = set()
        for info, payload in base_entries:
            existing.add(info.filename)
            destination.writestr(info, replacements.get(info.filename, payload))
        if _RELS not in existing:
            destination.writestr(_RELS, replacements[_RELS])
        for name, payload in additions.items():
            destination.writestr(name, payload)
    return out.getvalue()
