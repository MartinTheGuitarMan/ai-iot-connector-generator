import pytest

from generator.codegen import generate
from generator.spec import ProtocolSpec, SpecError


def test_load_weather_obs_spec():
    spec = ProtocolSpec.from_yaml("specs/weather_station_obs.yaml")
    assert spec.protocol == "wx_ascii"
    assert spec.transport == "delimited_text"
    assert spec.message.match_suffix == "WXOBS"
    assert spec.field_by_name("temperature_c").type == "float"


def test_load_weather_modbus_spec():
    spec = ProtocolSpec.from_yaml("specs/weather_station_modbus.yaml")
    assert spec.protocol == "modbus_weather"
    assert spec.transport == "register_map"
    assert spec.field_by_name("rain_mm").address == 0


def test_generated_source_is_valid_python():
    for path in (
        "specs/weather_station_obs.yaml",
        "specs/weather_station_obs_full.yaml",
        "specs/weather_station_modbus.yaml",
    ):
        spec = ProtocolSpec.from_yaml(path)
        source = generate(spec)
        compile(source, path, "exec")  # raises SyntaxError if malformed


def test_canonical_mapping_referencing_unknown_field_is_rejected():
    data = {
        "protocol": "bogus",
        "transport": "delimited_text",
        "message": {"id": "XXX", "match_suffix": "XXX"},
        "fields": [{"name": "a", "type": "str", "index": 1}],
        "canonical": {"measurements": [{"name": "a", "field": "does_not_exist"}]},
    }
    with pytest.raises(SpecError):
        ProtocolSpec.from_dict(data)


def test_delimited_text_field_requires_index():
    data = {
        "protocol": "bogus",
        "transport": "delimited_text",
        "message": {"id": "XXX", "match_suffix": "XXX"},
        "fields": [{"name": "a", "type": "str"}],
        "canonical": {},
    }
    with pytest.raises(SpecError):
        ProtocolSpec.from_dict(data)


def test_register_map_field_requires_address():
    data = {
        "protocol": "bogus",
        "transport": "register_map",
        "message": {"id": "XXX"},
        "fields": [{"name": "a", "type": "uint16"}],
        "canonical": {},
    }
    with pytest.raises(SpecError):
        ProtocolSpec.from_dict(data)
