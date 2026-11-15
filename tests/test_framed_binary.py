"""framed_binary transport: spec validation, codegen, blind reverse-engineering, and learning.

The end-to-end tests are the point of the exercise. scripts/gen_legacy_demo.py plays an
unknown device with a hidden wire protocol (four profiles, each modelled on a real shape:
sync+length+CRC, an obscure CRC, Cressi-style trailer byte, APC-style no-sync fixed frames).
reverse/ sees ONLY capture.bin + anchors.yaml, infers specs, the generator turns them into
connectors, and the connectors are scored against the hidden ground truth (truth.csv).
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
from reverse.infer import InferenceError, fit_field, infer_specs, learn_from_spec
from reverse.knowledge import KnowledgeBase

ROOT = Path(__file__).resolve().parent.parent
PROFILES = ["vessel", "cressi", "apc"]


def _make_demo(tmp_path_factory, profile, seed=1337):
    out = tmp_path_factory.mktemp(f"demo_{profile}_{seed}")
    subprocess.run([sys.executable, str(ROOT / "scripts" / "gen_legacy_demo.py"), "--out", str(out),
                    "--profile", profile, "--seed", str(seed)], check=True, capture_output=True)
    data = (out / "capture.bin").read_bytes()
    anchors = yaml.safe_load((out / "anchors.yaml").read_text())["anchors"]
    truth = list(csv.DictReader(open(out / "truth.csv")))
    return data, anchors, truth


@pytest.fixture(scope="module")
def demos(tmp_path_factory):
    return {p: _make_demo(tmp_path_factory, p) for p in PROFILES}


@pytest.fixture(scope="module")
def inferred(demos):
    return {p: infer_specs(d[0], d[1], f"dev_{p}", KnowledgeBase(), learn=False) for p, d in demos.items()}


def _load(tmp_path_factory, name, spec_dict):
    path = tmp_path_factory.mktemp("conn") / f"{name}.py"
    path.write_text(generate(ProtocolSpec.from_dict(spec_dict)))
    mod_spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(mod_spec)
    mod_spec.loader.exec_module(mod)
    return mod


EXPECTED = {
    "vessel": dict(sync="7ec3", length_offset=2, length_overhead=7, type_offset=3, seq_offset=4, payload_offset=5,
                   trailer="", algo="crc16", poly=0x8005, reflected=True, init=0xFFFF, start=2, messages={"engine", "tank", "status"}),
    "cressi": dict(sync="aaaaaa", length_offset=3, length_overhead=8, type_offset=4, seq_offset=None, payload_offset=5,
                   trailer="55", algo="crc16", poly=0x1021, reflected=False, init=0, start=3, messages={"dive", "gas"}),
    "apc": dict(sync="", fixed_length=19, type_offset=None, seq_offset=0, payload_offset=1,
                trailer="", algo="fletcher8", start=0, messages={"status"}),
}


@pytest.mark.parametrize("profile", PROFILES)
def test_structure_is_recovered(profile, inferred):
    st, specs, _ = inferred[profile]
    want = EXPECTED[profile]
    fd = st.framing_dict()
    for key in ("sync", "length_offset", "length_overhead", "fixed_length", "type_offset", "seq_offset",
                "payload_offset", "trailer"):
        if key in want:
            assert fd.get(key, "" if key in ("sync", "trailer") else None) == want[key], key
    assert st.crc["algo"] == want["algo"] and st.crc["start"] == want["start"]
    for key in ("poly", "reflected", "init"):
        if key in want:
            assert st.crc[key] == want[key], key
    assert set(specs) == want["messages"]  # vessel heartbeat has no anchors -> no spec


@pytest.mark.parametrize("profile", PROFILES)
def test_inferred_specs_validate_like_handwritten_ones(profile, inferred):
    for d in inferred[profile][1].values():
        ProtocolSpec.from_dict(d)


@pytest.mark.parametrize("profile", PROFILES)
def test_decoders_match_hidden_truth_on_every_frame(profile, demos, inferred, tmp_path_factory):
    data, _, truth = demos[profile]
    st, specs, _ = inferred[profile]
    mods = {msg: _load(tmp_path_factory, f"{profile}_{msg}", d) for msg, d in specs.items()}
    clean = [r for r in truth if r["corrupt"] == "0"]

    # every generated connector must find exactly the frames the inference found, and
    # those must be exactly the uncorrupted frames: corrupt ones rejected, noise survived
    for mod in mods.values():
        assert list(mod.iter_frames(data)) == [f.raw for f in st.frames]
    assert len(st.frames) == len(clean)

    checked = expected = 0
    for frame, row in zip((f.raw for f in st.frames), clean):
        mod = mods.get(row["message"])
        if mod is None:  # no anchors for this message type -> no connector, nobody may claim it
            assert all(m.parse(frame, "dev", "t") is None for m in mods.values())
            continue
        reading = mod.parse(frame, "dev", "2026-11-15T00:00:00Z")
        assert reading is not None
        for meas in reading.measurements:
            assert meas.value == pytest.approx(float(row[meas.name]), abs=1e-9), (row["ord"], meas.name)
            checked += 1
        expected += len(specs[row["message"]]["fields"])  # one measurement per anchored field
        for other, m in mods.items():  # a frame of another message type must be rejected
            if other != row["message"] and m.__dict__.get("TYPE_VALUE") is not None:
                assert m.parse(frame, "dev", "t") is None
    assert checked == expected > 5000


@pytest.mark.parametrize("profile", PROFILES)
def test_parse_rejects_corrupt_and_truncated_frames(profile, inferred, tmp_path_factory, demos):
    st, specs, _ = inferred[profile]
    msg, d = next(iter(specs.items()))
    mod = _load(tmp_path_factory, f"rej_{profile}", d)
    want = d["message"].get("type_value")
    frame = next(f.raw for f in st.frames if want is None or f.raw[st.type_offset] == want)
    assert mod.parse(frame, "d", "t") is not None
    bad = bytearray(frame)
    bad[st.payload_offset] ^= 0x01
    assert mod.parse(bytes(bad), "d", "t") is None
    assert mod.parse(frame[:-1], "d", "t") is None
    assert mod.parse(b"", "d", "t") is None


def test_codegen_is_deterministic(inferred):
    d = inferred["vessel"][1]["engine"]
    assert generate(ProtocolSpec.from_dict(d)) == generate(ProtocolSpec.from_dict(d))


def test_inference_never_reads_the_hidden_truth():
    for path in (ROOT / "reverse").glob("*.py"):
        text = path.read_text()
        assert "truth.csv" not in text, path
        assert "gen_legacy_demo" not in text, path


# --------------------------------------------------------------------------- real bytes

CRESSI_IDENTITY = bytes.fromhex("aaaaaa0b21b21100000acc00010004004a7855")  # from the published write-up
CRESSI_LOGBOOK = bytes.fromhex("aaaaaa02212000785a55")


def _cressi_spec():
    return {
        "protocol": "cressi_nepto", "transport": "framed_binary",
        "framing": {"sync": "aaaaaa", "length_offset": 3, "length_overhead": 8, "type_offset": 4,
                    "payload_offset": 5, "trailer": "55",
                    "crc": {"algo": "crc16", "poly": 0x1021, "init": 0, "reflected": False, "xorout": 0,
                            "start": 3, "endian": "little"}},
        "message": {"id": "identity", "type_value": 0x21, "payload_length": 11},
        "fields": [{"name": "serial", "type": "uint32_le", "offset": 0},
                   {"name": "model", "type": "uint8", "offset": 4},
                   {"name": "firmware", "type": "uint8", "offset": 5}],
        "canonical": {"measurements": [{"name": n, "field": n} for n in ("serial", "model", "firmware")]},
    }


def test_real_cressi_frames_decode(tmp_path_factory):
    """Two frames published in a real reverse-engineering write-up (serial 4530, model 10, firmware 204)."""
    mod = _load(tmp_path_factory, "cressi_identity", _cressi_spec())
    stream = b"\x00\x13" + CRESSI_IDENTITY + b"\xaa\xaa" + CRESSI_LOGBOOK  # leading + inter-frame garbage
    assert list(mod.iter_frames(stream)) == [CRESSI_IDENTITY, CRESSI_LOGBOOK]
    vals = {m.name: m.value for m in mod.parse(CRESSI_IDENTITY, "d", "t").measurements}
    assert vals == {"serial": 4530, "model": 10, "firmware": 204}
    assert mod.parse(CRESSI_LOGBOOK, "d", "t") is None  # same command byte, different payload size
    flipped = bytearray(CRESSI_IDENTITY)
    flipped[8] ^= 0x01
    assert mod.parse(bytes(flipped), "d", "t") is None  # CRC catches it
    assert mod.parse(CRESSI_IDENTITY[:-1] + b"\x56", "d", "t") is None  # trailer must be 0x55


# --------------------------------------------------------------------------- learning

def _odd_spec():
    """What a person would write from a datasheet for the vessel_oddcrc device."""
    return {
        "protocol": "vessel_oddcrc", "transport": "framed_binary",
        "framing": {"sync": "7ec3", "length_offset": 2, "length_overhead": 7, "type_offset": 3, "seq_offset": 4,
                    "payload_offset": 5,
                    "crc": {"algo": "crc16", "poly": 0x2F15, "init": 0xFFFF, "reflected": False, "xorout": 1,
                            "start": 2, "endian": "big"}},
        "message": {"id": "engine", "type_value": 0x10, "payload_length": 5},
        "fields": [{"name": "rpm", "type": "uint16_be", "offset": 0, "scale": 1.0, "unit": "rpm"},
                   {"name": "coolant_c", "type": "int16_be", "offset": 2, "scale": 0.1, "unit": "celsius"},
                   {"name": "oil_bar", "type": "uint8", "offset": 4, "scale": 0.05, "unit": "bar"}],
        "canonical": {"measurements": [{"name": n, "field": n} for n in ("rpm", "coolant_c", "oil_bar")]},
    }


@pytest.fixture(scope="module")
def odd(tmp_path_factory):
    return _make_demo(tmp_path_factory, "vessel_oddcrc", 1337), _make_demo(tmp_path_factory, "vessel_oddcrc", 7)


def test_unknown_crc_is_missed_without_knowledge_and_not_learned(odd):
    (data, anchors, _), _ = odd
    kb = KnowledgeBase()
    st, _, rep = infer_specs(data, anchors, "odd", kb)
    assert st.crc is None
    assert any("WARNING: no checksum matched" in line for line in rep)
    assert kb.data["runs"] == 0  # an unvalidated run teaches nothing
    assert any("not updated" in line for line in rep)


def _score(data_anchors_truth, kb, tmp_path_factory, tag):
    data, anchors, truth = data_anchors_truth
    st, specs, rep = infer_specs(data, anchors, "odd", kb, learn=False)
    mods = {m: _load(tmp_path_factory, f"{tag}_{m}", d) for m, d in specs.items()}
    clean = [r for r in truth if r["corrupt"] == "0"]
    assert len(st.frames) == len(clean)
    for f, row in zip(st.frames, clean):
        mod = mods.get(row["message"])
        if mod:
            for meas in mod.parse(f.raw, "d", "t").measurements:
                assert meas.value == pytest.approx(float(row[meas.name]), abs=1e-9)
    return st, rep


def test_taught_framing_is_recognised_on_a_new_capture(odd, tmp_path_factory):
    (dataA, anchorsA, _), second = odd
    kb = KnowledgeBase()
    msg = learn_from_spec(kb, ProtocolSpec.from_dict(_odd_spec()), dataA)
    assert "frames explained" in msg and kb.data["runs"] == 1
    st, rep = _score(second, kb, tmp_path_factory, "taught")
    assert st.crc["poly"] == 0x2F15 and st.crc["endian"] == "big"
    assert any("matches a framing from the knowledge base" in line for line in rep)
    assert len(st.frames) < 4000  # and now the corrupted frames are rejected


def test_taught_checksum_extends_the_catalogue_even_for_a_new_layout(odd, tmp_path_factory):
    """Keep only the learned CRC parameters (not the whole framing): heuristics find the layout,
    the knowledge base supplies the one thing they could not guess."""
    (dataA, _, _), second = odd
    kb = KnowledgeBase()
    learn_from_spec(kb, ProtocolSpec.from_dict(_odd_spec()), dataA)
    kb.data["framings"] = {}
    st, rep = _score(second, kb, tmp_path_factory, "crc_only")
    assert st.crc["poly"] == 0x2F15
    assert not any("matches a framing from the knowledge base" in line for line in rep)


def test_teaching_with_a_wrong_spec_is_refused_and_changes_nothing(odd):
    (dataA, _, _), _ = odd
    bad = _odd_spec()
    bad["framing"]["crc"]["poly"] = 0x1021  # does not describe this device
    kb = KnowledgeBase()
    with pytest.raises(InferenceError, match="does not explain the capture"):
        learn_from_spec(kb, ProtocolSpec.from_dict(bad), dataA)
    assert kb.data["runs"] == 0 and not kb.data["framings"]


def test_validated_runs_accumulate_and_persist(demos, tmp_path):
    path = tmp_path / "kb.json"
    kb = KnowledgeBase(path)
    for p in ("vessel", "cressi", "apc"):
        infer_specs(demos[p][0], demos[p][1], f"dev_{p}", kb)
        kb.save()
    again = KnowledgeBase(path)
    assert again.data["runs"] == 3
    assert len(again.data["framings"]) == 3
    assert {c["algo"] for c in again.learned_checksums()} == {"crc16", "fletcher8"}
    assert again.prior("celsius", "int8", 1.0, 0.0) == 1  # seen once, in the vessel protocol
    assert "3 validated runs from 3 protocol(s)" in again.summary()


def test_prior_breaks_ties_between_equally_good_fits_only(demos, tmp_path):
    data, anchors, _ = demos["apc"]
    # Positive-only readings cannot tell uint8 from int8; the device simply never went below zero.
    plain = infer_specs(data, anchors, "apc", KnowledgeBase(), learn=False)[1]["status"]
    assert next(f for f in plain["fields"] if f["name"] == "temp_c")["type"] == "uint8"

    kb = KnowledgeBase()
    kb.record_encoding("celsius", "int8", 1.0, 0.0, "some_earlier_device")
    st, specs, rep = infer_specs(data, anchors, "apc", kb, learn=False)
    assert next(f for f in specs["status"]["fields"] if f["name"] == "temp_c")["type"] == "int8"
    assert any("temp_c" in line and "prior from 1 protocol(s) decided" in line for line in rep)

    # but evidence always wins: readings above 127 cannot be int8, whatever the prior says
    payloads = [bytes([v]) for v in (10, 200, 90, 250, 130, 60)]
    ff = fit_field(payloads, [float(p[0]) for p in payloads], 1, "t", "celsius", kb)
    assert ff.type == "uint8" and not ff.prior_decided


def test_a_guess_is_never_recorded_as_evidence(demos):
    """Regression: a tie broken by an earlier prior (or by preference) must not feed back as new evidence."""
    data, anchors, _ = demos["apc"]
    kb = KnowledgeBase()
    kb.record_encoding("celsius", "int8", 1.0, 0.0, "some_earlier_device")
    for _ in range(3):
        infer_specs(data, anchors, "apc", kb)  # temp_c is decided by the prior, three times over
    assert kb.prior("celsius", "int8", 1.0, 0.0) == 1  # still only the earlier device
    assert kb.prior("celsius", "uint8", 1.0, 0.0) == 0
    assert kb.data["runs"] == 3  # the run itself (framing, checksum, unambiguous fields) was learned


def test_identical_anchor_values_give_a_clear_error():
    with pytest.raises(InferenceError, match="all have the same value"):
        fit_field([b"\x20", b"\x20", b"\x20"], [32.0, 32.0, 32.0], 1, "o2_pct", "percent")


def test_cli_learn_and_infer_roundtrip(odd, tmp_path, capsys):
    from reverse.cli import main

    (dataA, _, _), (dataB, anchorsB, _) = odd
    cap_a, cap_b, kbfile = tmp_path / "a.bin", tmp_path / "b.bin", tmp_path / "kb.json"
    cap_a.write_bytes(dataA)
    cap_b.write_bytes(dataB)
    (tmp_path / "anchors.yaml").write_text(yaml.safe_dump({"anchors": anchorsB}))
    (tmp_path / "odd.yaml").write_text(yaml.safe_dump(_odd_spec(), sort_keys=False))

    assert main(["knowledge", "--knowledge", str(kbfile)]) == 0
    assert "0 validated runs" in capsys.readouterr().out
    assert main(["learn", "--spec", str(tmp_path / "odd.yaml"), "--capture", str(cap_a),
                 "--knowledge", str(kbfile)]) == 0
    assert kbfile.exists()
    assert main(["infer", "--capture", str(cap_b), "--anchors", str(tmp_path / "anchors.yaml"),
                 "--protocol", "odd", "--out-dir", str(tmp_path / "specs"), "--connectors-dir", str(tmp_path / "conn"),
                 "--knowledge", str(kbfile)]) == 0
    out = capsys.readouterr().out
    assert "matches a framing from the knowledge base" in out
    assert (tmp_path / "specs" / "odd_engine.yaml").exists() and (tmp_path / "conn" / "odd_engine.py").exists()
    assert KnowledgeBase(kbfile).data["runs"] == 2  # the taught run + the validated inference


# --------------------------------------------------------------------------- spec model

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


def test_fixed_length_unsynced_spec_loads():
    d = _base_spec()
    d["framing"] = {"fixed_length": 19, "payload_offset": 1, "seq_offset": 0,
                    "crc": {"algo": "fletcher8", "start": 0}}
    d["message"] = {"id": "m", "payload_length": 16}
    ProtocolSpec.from_dict(d)


@pytest.mark.parametrize("mutate", [
    lambda d: d.pop("framing"),
    lambda d: d["message"].pop("type_value"),  # type_offset without type_value
    lambda d: d["fields"][0].pop("offset"),
    lambda d: d["fields"][0].update(type="float"),
    lambda d: d["fields"][0].update(offset=3),  # u16 at 3 runs past payload_length 4
    lambda d: d["framing"].update(sync="zz"),
    lambda d: d["framing"].update(trailer="5"),
    lambda d: d["framing"]["crc"].update(endian="middle"),
    lambda d: d["framing"]["crc"].update(algo="md5"),
    lambda d: d["framing"]["crc"].update(poly=0),  # crc16 needs a poly
    lambda d: d["framing"].update(payload_offset=3),
    lambda d: d["framing"].update(fixed_length=12),  # length byte AND fixed length
    lambda d: d["framing"].update(sync="", fixed_length=None, length_offset=None, length_overhead=None),
    lambda d: d["framing"].update(sync="", length_overhead=7),  # no sync, no fixed_length
    lambda d: d["framing"].update(length_overhead=6),  # shorter than header + check
])
def test_invalid_framed_binary_specs_are_rejected(mutate):
    d = _base_spec()
    mutate(d)
    with pytest.raises(SpecError):
        ProtocolSpec.from_dict(d)
