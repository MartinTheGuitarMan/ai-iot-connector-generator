"""framed_binary transport: spec validation, codegen, and the blind-reverse end-to-end test.

The end-to-end test is the point of the exercise. scripts/gen_legacy_demo.py plays an
unknown device with a hidden wire protocol. reverse/ sees ONLY capture.bin + anchors.yaml,
infers specs, the generator turns them into connectors, and the connectors are scored
against the hidden ground truth (truth.csv) on every frame.
"""

import csv
import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from generator.codegen import generate
from generator.spec import ProtocolSpec, SpecError
from reverse.infer import infer_specs

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def demo(tmp_path_factory):
    out = tmp_path_factory.mktemp("legacy_demo")
    subprocess.run([sys.executable, str(ROOT / "scripts" / "gen_legacy_demo.py"), "--out", str(out)],
                   check=True, capture_output=True)
    data = (out / "capture.bin").read_bytes()
    anchors = yaml.safe_load((out / "anchors.yaml").read_text())["anchors"]
    truth = list(csv.DictReader(open(out / "truth.csv")))
    return data, anchors, truth


@pytest.fixture(scope="module")
def inferred(demo):
    data, anchors, _ = demo
    return infer_specs(data, anchors, "vessel_legacy")


def _load_connector(tmp_path_factory, name, source):
    path = tmp_path_factory.mktemp("conn") / f"{name}.py"
    path.write_text(source)
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_structure_is_recovered(inferred):
    st, specs, _ = inferred
    assert st.sync == bytes.fromhex("7ec3")
    assert (st.length_offset, st.length_overhead, st.type_offset, st.seq_offset, st.payload_offset) == (2, 7, 3, 4, 5)
    assert st.crc["reflected"] and st.crc["poly"] == 0x8005 and st.crc["init"] == 0xFFFF
    assert st.crc["endian"] == "little" and st.crc["start"] == 2
    assert set(specs) == {"engine", "tank", "status"}  # heartbeat has no anchors -> no spec


def test_inferred_specs_validate_like_handwritten_ones(inferred):
    for d in inferred[1].values():
        ProtocolSpec.from_dict(d)


def test_decoders_match_hidden_truth_on_every_frame(demo, inferred, tmp_path_factory):
    data, _, truth = demo
    mods = {
        msg: _load_connector(tmp_path_factory, f"vessel_legacy_{msg}", generate(ProtocolSpec.from_dict(d)))
        for msg, d in inferred[1].items()
    }
    by_ord = {int(r["ord"]): r for r in truth}
    clean = {o for o, r in by_ord.items() if r["corrupt"] == "0"}

    # frame iteration must reject exactly the corrupted frames and survive line noise
    frames = list(mods["engine"].iter_frames(data))
    ords, base, prev = [], 0, None
    for f in frames:
        s = f[mods["engine"].TYPE_OFFSET + 1]
        if prev is not None and s < prev - 128:
            base += 256
        prev = s
        ords.append(base + s)
    assert set(ords) == clean

    checked = 0
    for frame, o in zip(frames, ords):
        row = by_ord[o]
        if row["message"] == "heartbeat":
            assert all(m.parse(frame, "dev", "t") is None for m in mods.values())
            continue
        reading = mods[row["message"]].parse(frame, "dev", "2026-11-15T00:00:00Z")
        assert reading is not None
        for meas in reading.measurements:
            assert meas.value == pytest.approx(float(row[meas.name]), abs=1e-9), (o, meas.name)
            checked += 1
        for other, m in mods.items():  # wrong message type must be rejected
            if other != row["message"]:
                assert m.parse(frame, "dev", "t") is None
    per_msg = {"engine": 3, "tank": 3, "status": 2}  # measurements per frame
    assert checked == sum(per_msg.get(by_ord[o]["message"], 0) for o in clean)


def test_parse_rejects_corrupt_and_truncated_frames(inferred, demo, tmp_path_factory):
    mod = _load_connector(tmp_path_factory, "vessel_legacy_engine",
                          generate(ProtocolSpec.from_dict(inferred[1]["engine"])))
    frame = next(f for f in mod.iter_frames(demo[0]) if f[3] == 0x10)
    assert mod.parse(frame, "d", "t") is not None
    bad = bytearray(frame)
    bad[6] ^= 0x01
    assert mod.parse(bytes(bad), "d", "t") is None
    assert mod.parse(frame[:-1], "d", "t") is None
    assert mod.parse(b"", "d", "t") is None


def test_codegen_is_deterministic(inferred):
    d = inferred[1]["engine"]
    assert generate(ProtocolSpec.from_dict(d)) == generate(ProtocolSpec.from_dict(d))


def test_inference_never_reads_the_hidden_truth():
    for path in (ROOT / "reverse").glob("*.py"):
        text = path.read_text()
        assert "truth.csv" not in text, path
        assert "gen_legacy_demo" not in text, path


def _base_spec():
    return {
        "protocol": "p", "transport": "framed_binary",
        "framing": {"sync": "7ec3", "length_offset": 2, "length_overhead": 7,
                    "type_offset": 3, "payload_offset": 5,
                    "crc": {"poly": 0x8005, "init": 0xFFFF, "reflected": True, "start": 2}},
        "message": {"id": "m", "type_value": 0x10, "payload_length": 4},
        "fields": [{"name": "a", "type": "uint16_be", "offset": 0, "scale": 0.1}],
        "canonical": {"measurements": [{"name": "a", "field": "a"}]},
    }


def test_valid_framed_binary_spec_loads():
    ProtocolSpec.from_dict(_base_spec())


@pytest.mark.parametrize("mutate", [
    lambda d: d.pop("framing"),
    lambda d: d["message"].pop("type_value"),
    lambda d: d["fields"][0].pop("offset"),
    lambda d: d["fields"][0].update(type="float"),
    lambda d: d["fields"][0].update(offset=3),  # u16 at 3 runs past payload_length 4
    lambda d: d["framing"].update(sync="zz"),
    lambda d: d["framing"]["crc"].update(endian="middle"),
    lambda d: d["framing"].update(payload_offset=3),
])
def test_invalid_framed_binary_specs_are_rejected(mutate):
    d = _base_spec()
    mutate(d)
    with pytest.raises(SpecError):
        ProtocolSpec.from_dict(d)
