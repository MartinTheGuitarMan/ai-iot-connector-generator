import copy

import pytest
import yaml

from authoring.eval import PROTOCOLS, evaluate, evaluate_references


def _load(key):
    return yaml.safe_load(PROTOCOLS[key].reference_spec.read_text())


@pytest.mark.parametrize("key", sorted(PROTOCOLS))
def test_reference_specs_score_perfectly(key):
    r = evaluate_references()[key]
    assert r.ok, r.summary()
    assert r.total >= 10


@pytest.mark.parametrize("key", sorted(PROTOCOLS))
def test_docs_exist_and_are_nonempty(key):
    assert len(PROTOCOLS[key].doc.read_text()) > 500


def test_accepts_yaml_text_and_dict():
    text = PROTOCOLS["modbus"].reference_spec.read_text()
    assert evaluate("modbus", text).ok
    assert evaluate("modbus", yaml.safe_load(text)).ok


def _mutate(key, fn):
    d = copy.deepcopy(_load(key))
    fn(d)
    return evaluate(key, d)


def test_wrong_scale_is_caught():
    r = _mutate("modbus", lambda d: d["fields"][2].update(scale=0.1))
    assert not r.ok and r.failures


def test_wrong_unit_is_caught():
    r = _mutate("modbus", lambda d: d["canonical"]["measurements"][0].update(unit="inches"))
    assert not r.ok


def test_wrong_address_is_caught():
    r = _mutate("modbus", lambda d: d["fields"][0].update(address=4))
    assert not r.ok  # also fails the missing-register / extra-register cases


def test_swapped_field_index_is_caught():
    def swap(d):
        d["fields"][6]["index"], d["fields"][7]["index"] = 7, 6
    assert not _mutate("wxobs", swap).ok


def test_wrong_hemisphere_field_is_caught():
    def f(d):
        d["canonical"]["location"]["lat_dir_field"] = "site_lon_dir"
    assert not _mutate("wxobs", f).ok


def test_behaviourally_equivalent_spec_is_not_flagged():
    # "OBSF" vs "WXOBSF" as match_suffix behaves identically on every input,
    # so the eval (which judges behaviour, not spelling) must pass it.
    r = _mutate("wxobsf", lambda d: d["message"].update(match_suffix="OBSF"))
    assert r.ok


def test_dropping_date_field_is_caught():
    def f(d):
        del d["canonical"]["date_field"]
    assert not _mutate("wxobsf", f).ok


def test_missing_measurement_is_caught():
    assert not _mutate("wxobs", lambda d: d["canonical"]["measurements"].pop()).ok


def test_invalid_spec_reports_spec_stage_not_exception():
    r = _mutate("modbus", lambda d: d["fields"][0].update(type="float"))
    assert r.stage == "spec" and "float" in r.error and not r.ok


def test_unparseable_yaml_reports_spec_stage():
    r = evaluate("modbus", "protocol: [unclosed\nfields: {")
    assert r.stage == "spec" and not r.ok


def test_dangling_field_reference_reports_spec_stage():
    r = _mutate("modbus", lambda d: d["canonical"]["measurements"][0].update(field="nope"))
    assert r.stage == "spec"
