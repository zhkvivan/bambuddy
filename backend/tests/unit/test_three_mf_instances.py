import io
from xml.etree import ElementTree as ET
from zipfile import ZipFile

import pytest

from backend.app.services.three_mf_instances import duplicate_plate_instances

MODEL = b"""<?xml version="1.0" encoding="UTF-8"?>
<model xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02" xmlns:p="http://schemas.microsoft.com/3dmanufacturing/production/2015/06">
  <resources><object id="1" type="model"><mesh/></object></resources>
  <build><item objectid="1" p:UUID="11111111-1111-1111-1111-111111111111" transform="1 0 0 0 1 0 0 0 1 10 10 0" printable="1"/></build>
</model>"""

SETTINGS = b"""<?xml version="1.0" encoding="UTF-8"?>
<config><plate><metadata key="plater_id" value="1"/>
  <model_instance><metadata key="object_id" value="1"/><metadata key="instance_id" value="0"/><metadata key="identify_id" value="7"/></model_instance>
</plate><assemble>
  <assemble_item object_id="1" instance_id="0" transform="1 0 0 0 1 0 0 0 1 10 10 0"/>
  <assemble_item object_id="1" volume_id="0" transform="1 0 0 0 1 0 0 0 1 0 0 0"/>
</assemble></config>"""


def make_project() -> bytes:
    output = io.BytesIO()
    with ZipFile(output, "w") as archive:
        archive.writestr("3D/3dmodel.model", MODEL)
        archive.writestr("Metadata/model_settings.config", SETTINGS)
        archive.writestr("Metadata/project_settings.config", b"keep me")
    return output.getvalue()


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def test_duplicates_build_items_and_plate_instances() -> None:
    result = duplicate_plate_instances(make_project(), copies=3, plate=1)

    with ZipFile(io.BytesIO(result)) as archive:
        model = ET.fromstring(archive.read("3D/3dmodel.model"))
        settings = ET.fromstring(archive.read("Metadata/model_settings.config"))
        assert archive.read("Metadata/project_settings.config") == b"keep me"

    items = [element for element in model.iter() if local_name(element.tag) == "item"]
    instances = [element for element in settings.iter() if local_name(element.tag) == "model_instance"]
    assemble_items = [element for element in settings.iter() if local_name(element.tag) == "assemble_item"]
    assert len(items) == 3
    assert len(instances) == 3
    assert {item.get("transform") for item in items} == {"1 0 0 0 1 0 0 0 1 10 10 0"}
    instance_ids = {
        int(next(m.get("value") for m in instance if m.get("key") == "instance_id")) for instance in instances
    }
    assert instance_ids == {0, 1, 2}
    uuids = [{local_name(key): value for key, value in item.attrib.items()}["UUID"] for item in items]
    assert len(set(uuids)) == 3
    assert {item.get("instance_id") for item in assemble_items if item.get("instance_id") is not None} == {
        "0",
        "1",
        "2",
    }
    assert len([item for item in assemble_items if item.get("volume_id") == "0"]) == 1


def test_one_copy_returns_input_unchanged() -> None:
    source = make_project()
    assert duplicate_plate_instances(source, copies=1) is source


def test_rejects_slice_all() -> None:
    with pytest.raises(ValueError, match="specific plate"):
        duplicate_plate_instances(make_project(), copies=2, plate=0)
