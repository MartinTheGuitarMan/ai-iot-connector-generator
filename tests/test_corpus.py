"""Real published frames, identified blind. Each case's documented rule must be found."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from run_corpus import run  # noqa: E402

ROWS = {r["id"]: r for r in run()}


@pytest.mark.parametrize("case", sorted(ROWS))
def test_documented_rule_found(case):
    r = ROWS[case]
    assert r["ref_valid"], "transcription error: documented rule does not hold on the frames"
    assert r["top_validates"]
    assert r["reference_found"]


def test_high_confidence_cases_are_unambiguous():
    for r in ROWS.values():
        if r["conf"] == "high":
            assert r["cands"] == 1


def test_new_checksum_families_roundtrip_through_codegen():
    from generator.codegen import generate
    from generator.spec import ProtocolSpec
    for algo, extra in (("fletcher8_256", {"endian": "little"}), ("sum16", {"init": 0, "xorout": 0, "endian": "big"}),
                        ("negsum8", {}), ("negsum16", {"endian": "big"})):
        spec = ProtocolSpec.from_dict({
            "protocol": "t", "transport": "framed_binary",
            "framing": {"sync": "aa", "payload_offset": 1, "fixed_length": 6,
                        "crc": {"algo": algo, "start": 0, **extra}},
            "message": {"id": "m"},
            "fields": [{"name": "a", "offset": 0, "type": "uint8", "unit": "count"}]})
        compile(generate(spec), "<gen>", "exec")


def test_short_capture_does_not_crash():
    from reverse.infer import InferenceError, infer_structure
    with pytest.raises(InferenceError):
        infer_structure(bytes.fromhex("55aa00000000ff55aa000100000055aa0002000001" * 4))


def test_text_layers():
    from reverse.textlayer import classify, decode_hex_ascii, nmea_check
    assert decode_hex_ascii(":7A8ED00B9") == bytes.fromhex("07a8ed00b9")
    assert decode_hex_ascii("7A8ED00B9") is None                       # no ':' no frame
    assert decode_hex_ascii(":7A8ED00B") == bytes.fromhex("7A8ED00B")   # even digit count: Modbus ASCII pairs
    assert nmea_check("$GPGGA,123519,4807.038,N,01131.000,E,1,08,0.9,545.4,M,46.9,M,,*47")["ok"]
    assert not nmea_check("$GPGLL,4916.45,N,12311.12,W,225444,A,*00")["ok"]
    assert classify([":7A8ED00B9", ":7FF0F0040"]) == "hex_ascii"
    assert classify(["55 aa 00 00 00 00 ff"]) == "binary"


def test_victron_constant_is_solved_not_enumerated():
    from reverse.identify import identify
    from reverse.textlayer import decode_hex_ascii
    r = identify([decode_hex_ascii(x) for x in (":7A8ED00B9", ":89DED00F609C4", ":8F0ED0064000C", ":7FF0F0040")])
    assert r["constant_solved_from_data"] and r["matches"][0]["init"] == 0x55
    assert r["confidence"] != "high"  # one free parameter


def test_dnp3_block_crc_repeats_every_16_bytes():
    import yaml
    from reverse.identify import detect_block_crc, identify
    root = Path(__file__).resolve().parent.parent / "corpus"
    frames = [bytes.fromhex(f.replace(" ", "")) for f in yaml.safe_load(open(root / "dnp3_blocks_frames.yaml"))["frames"]]
    rule = identify([f[:10] for f in frames])["matches"][0]
    assert detect_block_crc(frames, 10, rule) == 16
    # a frame with a corrupted data block must not pass
    bad = [bytearray(f) for f in frames]; bad[1][20] ^= 1
    assert detect_block_crc([bytes(b) for b in bad], 10, rule) is None


def test_seatalk_nibble_length_rule_found_without_a_checksum():
    import yaml
    from reverse.identify import find_length_rule
    root = Path(__file__).resolve().parent.parent / "corpus"
    frames = [bytes.fromhex(f.replace(" ", "")) for f in yaml.safe_load(open(root / "seatalk1_frames.yaml"))["frames"]]
    top = find_length_rule(frames)[0]
    assert (top["offset"], top["mask"], top["overhead"]) == (1, 0x0F, 3)


def test_length_rule_needs_varying_lengths():
    from reverse.identify import find_length_rule
    assert find_length_rule([bytes(8), bytes(8), bytes(8)]) == []
    plain = [bytes([2, 9, 9]), bytes([3, 9, 9, 9]), bytes([4, 9, 9, 9, 9])]
    assert find_length_rule(plain)[0]["mask"] == 0xFF


def _fast_packet(cid, payload, ctr):
    """Frames of one NMEA 2000 fast-packet message: byte0 = counter<<5 | index, frame 0 byte1 = length."""
    frames, first, rest = [], payload[:6], payload[6:]
    frames.append(bytes([ctr << 5, len(payload)]) + first.ljust(6, b"\xff"))
    for k in range(0, len(rest), 7):
        frames.append(bytes([ctr << 5 | (k // 7 + 1)]) + rest[k:k + 7].ljust(7, b"\xff"))
    return [(0.0, cid, f) for f in frames]


def test_fast_packet_found_and_sid_counters_rejected():
    from reverse.canlog import fast_packet_ids
    log = []
    for m in range(5):
        log += _fast_packet(0x19FA0406, bytes(range(m, m + 30)), m % 8)
    for sid in range(60):                                     # single-frame PGN whose byte 0 counts every frame
        log.append((0.0, 0x09F11223, bytes([sid & 0xFF, 0x78, 0x57, 0, 0, 0x2F, 0xF6, 0xFD])))
    for m in range(1):                                        # one long message is enough
        log += _fast_packet(0x1DFF280A, b"No rudder data. Steering is unreliable", 3)
    found = fast_packet_ids(log)
    assert set(found) == {0x19FA0406, 0x1DFF280A}


def test_real_n2k_capture_if_available():
    import os, pytest
    path = os.environ.get("N2K_SAMPLE_LOG", "/tmp/hunt/n2k/src/testdata/sample.log")
    if not os.path.exists(path):
        pytest.skip("real capture not downloaded (git clone https://github.com/open-ships/n2k)")
    from reverse.canlog import fast_packet_ids, parse_candump, pgn_of
    found = {pgn_of(c) for c in fast_packet_ids(parse_candump(open(path).read()))}
    assert {127237, 129540, 129545, 130577, 130578, 130856} <= found
    assert 127250 not in found and 127251 not in found        # single-frame PGNs


def test_mbus_inferred_spec_generates_a_connector_that_reads_real_telegrams():
    import importlib.util, tempfile, yaml
    from generator.codegen import generate
    from generator.spec import ProtocolSpec
    root = Path(__file__).resolve().parent.parent
    frames = [bytes.fromhex(f.replace(" ", "")) for f in yaml.safe_load(open(root / "corpus/cases/mbus_libmbus.yaml"))["frames"]]
    code = generate(ProtocolSpec.from_yaml(root / "specs/mbus_long_frame.yaml"))
    path = Path(tempfile.mkdtemp()) / "mbus_gen.py"
    path.write_text(code)
    sp = importlib.util.spec_from_file_location("mbus_gen", path)
    mod = importlib.util.module_from_spec(sp)
    sp.loader.exec_module(mod)
    assert list(mod.iter_frames(b"".join(frames))) == frames
    noisy = bytearray(b"\x00\xff" + b"\x13".join(frames))
    noisy[40] ^= 0x40                                     # corrupt one frame: it must be dropped, the rest kept
    assert 0 < len(list(mod.iter_frames(bytes(noisy)))) < len(frames)


def test_mbus_full_set_if_available():
    import glob, os, re, pytest
    d = os.environ.get("LIBMBUS_FRAMES", "/tmp/hunt/libmbus/src/test/test-frames")
    files = sorted(glob.glob(d + "/*.hex"))
    if not files:
        pytest.skip("libmbus not cloned (git clone https://github.com/rscada/libmbus)")
    from reverse.identify import find_length_rule, identify
    frames = []
    for p in files:
        hx = re.findall(r"[0-9A-Fa-f]{2}", re.sub(r"[^0-9A-Fa-f\s]", "", open(p, errors="ignore").read()))
        if len(hx) >= 6:
            frames.append(bytes.fromhex("".join(hx)))
    assert len(frames) >= 50
    assert (find_length_rule(frames)[0]["offset"], find_length_rule(frames)[0]["overhead"]) == (1, 6)
    top = identify(frames)["matches"][0]
    assert (top["algo"], top["start"], top["trailer_len"]) == ("sum8", 4, 1)


def test_locate_finds_bcd_and_integer_readings_in_a_real_mbus_frame():
    from reverse.locate import locate
    frame = bytes.fromhex(
        "68 3C 3C 68 08 08 72 78 03 49 11 77 04 0E 16 0A 00 00 00 0C 78 78 03 49 11 04 13 31 D4 00 00 42 6C 00 00 "
        "44 13 00 00 00 00 04 6D 0B 0B CD 13 02 27 00 00 09 FD 0E 02 09 FD 0F 06 0F 00 01 75 13 D3 16".replace(" ", ""))
    fab = locate(frame, 11490378, scales=(1,), start=7, end=len(frame) - 2)          # fabrication number, BCD
    assert any(h["encoding"] == "bcd_le" and h["offset"] == 7 and h["width"] == 4 for h in fab)
    vol = locate(frame, 54321, scales=(1,), start=7, end=len(frame) - 2)             # volume, 32-bit LE
    assert any(h["encoding"] == "uint_le" and h["offset"] == 27 and h["width"] == 4 for h in vol)


def test_locate_float32_and_scale():
    import struct
    from reverse.locate import locate
    frame = b"\x01\x02" + struct.pack("<f", 357.621735) + b"\x00"
    assert any(h["encoding"] == "float32_le" and h["offset"] == 2 for h in locate(frame, 357.621735))
    scaled = b"\xaa" + (2345).to_bytes(2, "little")
    assert any(h["scale"] == 0.01 and h["width"] == 2 for h in locate(scaled, 23.45))
    assert locate(b"\x00\x00\x00\x00", 5) == []


def test_learned_rule_breaks_a_tie_but_luck_is_not_recorded():
    import yaml
    from reverse.identify import identify, learn_from_identification
    from reverse.knowledge import KnowledgeBase
    root = Path(__file__).resolve().parent.parent / "corpus/cases"
    winsen = [bytes.fromhex(f.replace(" ", "")) for f in yaml.safe_load(open(root / "winsen_sensors.yaml"))["frames"]]
    base = identify(winsen)
    assert base["n_candidates_validating"] == 2 and base["matches"][0]["algo"] == "sum8"   # ties: two equivalent rules
    kb = KnowledgeBase()
    other = [bytes([0x11, (x * 5) & 0xFF, (x * 11 + 3) & 0xFF, x, (-((x * 5) + (x * 11 + 3) + x)) & 0xFF]) for x in range(30, 50)]          # an unrelated negsum8 protocol
    assert learn_from_identification(kb, identify(other, kb), "other").startswith("recorded")
    assert identify(winsen, kb)["matches"][0]["algo"] == "negsum8"                            # learned rule now wins the tie
    # a single frame validates by luck: it must not be recorded
    kb2 = KnowledgeBase()
    one = identify([bytes.fromhex("7E 00 0A 01 01 50 01 00 48 65 6C 6C 6F B8")], kb2)
    assert learn_from_identification(kb2, one, "xbee").startswith("not recorded")
    assert kb2.summary().startswith("0 validated runs")


def test_crc8_crc32_catalogue_values_and_generated_code_agree():
    import importlib.util, tempfile, zlib
    from generator.codegen import generate
    from generator.spec import ProtocolSpec
    from reverse.infer import checksum
    d = b"123456789"
    assert checksum(d, "crc8", 0x07, 0, False, 0) == 0xF4          # CRC-8/SMBUS
    assert checksum(d, "crc8", 0x31, 0, True, 0) == 0xA1           # CRC-8/MAXIM-DOW (1-Wire)
    assert checksum(d, "crc8", 0x31, 0xFF, False, 0) == 0xF7       # CRC-8/NRSC-5 (Sensirion sensors)
    assert checksum(d, "crc32", 0x04C11DB7, 0xFFFFFFFF, True, 0xFFFFFFFF) == zlib.crc32(d) == 0xCBF43926
    assert checksum(d, "crc32", 0x1EDC6F41, 0xFFFFFFFF, True, 0xFFFFFFFF) == 0xE3069283   # CRC-32C
    for algo, poly, init, refl, xo, extra in (
            ("crc8", 0x31, 0xFF, False, 0, {}), ("crc8", 0x31, 0, True, 0, {}),
            ("crc32", 0x04C11DB7, 0xFFFFFFFF, True, 0xFFFFFFFF, {"endian": "little"}),
            ("crc32", 0x04C11DB7, 0xFFFFFFFF, False, 0xFFFFFFFF, {"endian": "big"})):
        spec = ProtocolSpec.from_dict({
            "protocol": "t", "transport": "framed_binary",
            "framing": {"sync": "aa", "length_offset": 1, "length_overhead": 2 + {"crc8": 1, "crc32": 4}[algo],
                        "payload_offset": 2, "crc": {"algo": algo, "poly": poly, "init": init, "reflected": refl,
                                                     "xorout": xo, "start": 0, **extra}},
            "message": {"id": "m"}, "fields": [{"name": "a", "offset": 0, "type": "uint8", "unit": "count"}]})
        path = Path(tempfile.mkdtemp()) / "g.py"
        path.write_text(generate(spec))
        sp = importlib.util.spec_from_file_location("g", path)
        mod = importlib.util.module_from_spec(sp)
        sp.loader.exec_module(mod)
        body = b"\xaa\x05" + b"\x10"
        size = {"crc8": 1, "crc32": 4}[algo]
        body = b"\xaa" + bytes([1 + size + 2 - 2 + 1]) + b"\x10"
        stored = mod._checksum(body)
        assert stored == checksum(body, algo, poly, init, refl, xo)


def test_whoop_crc32_blind_finds_payload_crc_not_the_documented_one():
    import zlib
    from reverse.identify import identify
    pairs = [("aa100057236d4201d036656600000000", "f62deb81"), ("aa100057236e42010c37656600000000", "1023ccef"),
             ("aa100057236f4201207d656600000000", "fea1e060"), ("aa100057237042015011656600000000", "f226a8bd"),
             ("aa0800a823070e00", "c7e40f08"), ("aa0800a823080e01", "6c935474")]
    frames = [bytes.fromhex(d + c) for d, c in pairs]
    for f in frames:
        assert zlib.crc32(f[4:-4]).to_bytes(4, "little") == f[-4:]
    r = identify(frames)
    assert r["confidence"] == "high" and r["n_candidates_validating"] == 1
    m = r["matches"][0]
    assert (m["algo"], m["start"], m["poly"], m["init"], m["xorout"], m["reflected"]) == \
           ("crc32", 4, 0x04C11DB7, 0xFFFFFFFF, 0xFFFFFFFF, True)


def test_ppp_hdlc_unstuffing_and_fcs_found_blind():
    from reverse.identify import identify
    from reverse.stuffing import detect, unstuff
    horo = bytes.fromhex("ff03c02101010017020600" "0a0000" "0506002a2b78" "0702" "0802" "0d0306" "a5f8")
    gist = bytes.fromhex("7eff7d23c0217d217d217d207d397d217d247d227d207d227d26ffffffff7d237d25c2237d257d257d267d207d2030c8e2d47e")
    def stuff(d):
        o = bytearray()
        for b in d:
            o += bytes([0x7d, b ^ 0x20]) if b < 0x20 or b in (0x7d, 0x7e) else bytes([b])
        return b"\x7e" + bytes(o) + b"\x7e"
    frames = [stuff(horo), gist]
    assert unstuff(frames, detect(frames))[0] == horo
    r = identify(frames)
    assert r["stuffing"] == "hdlc" and r["n_candidates_validating"] == 1
    m = r["matches"][0]
    assert (m["algo"], m["poly"], m["init"], m["xorout"], m["reflected"], m["endian"]) == \
           ("crc16", 0x1021, 0xFFFF, 0xFFFF, True, "little")


def test_stuffing_not_assumed_without_flag_evidence():
    from reverse.stuffing import detect, unstuff_hdlc, unstuff_slip
    assert detect([b"\xaa\x7d\x01\x02\x03", b"\xaa\x7d\x05\x06\x07"]) is None      # stray 7D, no flags
    assert detect([b"\x7e\x01\x7e\x02\x7e", b"\x7e\x03\x04\x7e"]) is None            # raw flag inside the frame
    assert unstuff_hdlc(b"\x7e\x01\x7d\x7e") is None                                 # dangling escape
    assert unstuff_hdlc(b"\x7e\x7d\x5e\x7d\x5d\x7e") == b"\x7e\x7d"


def test_slip_unstuffing():
    from reverse.stuffing import detect, unstuff
    frames = [b"\xc0\x01\xdb\xdc\x02\xc0", b"\xc0\xdb\xdd\x05\xc0"]
    d = detect(frames)
    assert d["scheme"] == "slip"
    assert unstuff(frames, d) == [b"\x01\xc0\x02", b"\xdb\x05"]
    assert detect([b"\xc0\x01\xdb\x02\xc0"]) is None                                 # bad escape pair


def test_davis_vector_single_frame_is_low_confidence_but_candidate_present():
    from reverse.identify import identify
    from reverse.infer import checksum
    assert checksum(bytes.fromhex("c6cea203"), "crc16", 0x1021, 0, False, 0) == 0xE2B4
    r = identify([bytes.fromhex("c6cea203e2b4")])
    assert r["confidence"] == "low"
    assert any(m["algo"] == "crc16" and m["poly"] == 0x1021 and m["init"] == 0 and not m["reflected"]
               and m["endian"] == "big" for m in r["matches"])


def test_modbus_ascii_text_layer_and_tolerant_identification():
    from reverse.textlayer import classify, decode_hex_ascii
    from reverse.identify import identify, learn_from_identification
    from reverse.knowledge import KnowledgeBase
    lines = [":0003007C00027F", ":000304008D008EDE", ":0083027B", ":02840179", ":1103007C00026E",
             ":110304008D008ECD", ":1183026A", ":F7031389000A60", ":FF03007C000280", ":FF0304008D008EDF",
             ":FF83027C", ":010105040017DF", ":03080001F7"]               # last two: LRC without the address byte
    assert classify(lines) == "hex_ascii"
    assert decode_hex_ascii(":0003007C00027F") == bytes.fromhex("0003007C00027F")
    assert decode_hex_ascii(":7A8ED00B9") == bytes.fromhex("07A8ED00B9")        # Victron shape still works
    r = identify([decode_hex_ascii(l) for l in lines])
    assert r["partial"] == {"validated": 11, "of": 13, "outliers": [11, 12]}
    assert r["confidence"] == "medium"                                          # never high on a partial fit
    assert r["matches"][0]["algo"] == "negsum8"
    assert learn_from_identification(KnowledgeBase(), r, "x").startswith("not recorded")


def test_tolerant_pass_needs_enough_frames_and_a_clear_majority():
    from reverse.identify import identify
    good = [bytes([0xAA, i, i + 1, (i + i + 1) & 0xFF]) for i in range(1, 9)]
    # 3 of 8 broken: 5/8 is below 70%, so no partial answer is invented
    broken = [g[:-1] + bytes([(g[-1] + 1) & 0xFF]) if i in (0, 3, 6) else g for i, g in enumerate(good)]
    assert identify(broken)["partial"] is None
    # 1 of 8 broken: 7/8 passes
    one = [g[:-1] + b"\x00" if i == 2 else g for i, g in enumerate(good)]
    r = identify(one)
    assert r["partial"] and r["partial"]["outliers"] == [2]


def test_mavlink_seeded_crc_real_heartbeat_single_frame():
    from reverse.identify import identify
    f = bytes.fromhex("fe094e0101000000000002035104031c7f")
    r = identify([f])
    assert r["seeded_confidence"] == "low"
    hit = [s for s in r["seeded_crc16"] if s["poly"] == 0x1021 and s["start"] == 1]
    assert hit and hit[0]["append"] == [50] and hit[0]["init"] == 0xFFFF and hit[0]["reflected"]
    assert not any(m["algo"] == "crc16" for m in r["matches"])      # the plain search cannot see it


def test_mavlink_seeded_crc_several_message_types_synthetic():
    """SYNTHETIC frames (random payloads) with CRC_EXTRA values from the NI forum table: 0->50, 1->124, 2->137."""
    import random
    from reverse.identify import identify
    from reverse.infer import _crc_generic
    rnd = random.Random(7)
    extra = {0: 50, 1: 124, 2: 137}
    plen = {0: 9, 1: 31, 2: 12}
    frames = []
    for seq in range(9):
        mid = seq % 3
        body = bytes([plen[mid], seq, 1, 1, mid]) + bytes(rnd.randrange(256) for _ in range(plen[mid]))
        crc = _crc_generic(body + bytes([extra[mid]]), 16, 0x1021, 0xFFFF, True, 0)
        frames.append(b"\xfe" + body + crc.to_bytes(2, "little"))
    r = identify(frames)
    hit = [s for s in r["seeded_crc16"] if s["poly"] == 0x1021 and s["init"] == 0xFFFF and s["reflected"] and s["start"] == 1]
    assert hit, r["seeded_crc16"]
    h = hit[0]
    assert h["append_by"] in (1, 5)                      # length byte and message id both separate the types
    assert sorted(h["append"].values()) == [50, 124, 137] and h["groups"] == 3
    assert r["seeded_confidence"] == "high"


def test_seeded_group_needs_two_frames_per_message_type():
    from reverse.identify import identify
    from reverse.infer import _crc_generic
    frames = []
    for mid, ex in ((0, 50), (1, 124), (2, 137)):          # one frame per type: nothing to cross-check the byte on
        body = bytes([4, mid, 1, 1, mid, 9, 8, 7, 6][:9])
        crc = _crc_generic(body + bytes([ex]), 16, 0x1021, 0xFFFF, True, 0)
        frames.append(b"\xfe" + body + crc.to_bytes(2, "little"))
    assert not [s for s in identify(frames)["seeded_crc16"] if s["groups"] > 1]


def test_dynamixel_protocol2_all_frames_blind_unambiguous():
    import yaml
    from reverse.identify import identify
    c = yaml.safe_load(open(Path(__file__).resolve().parent.parent / "corpus/cases/dynamixel_protocol2.yaml"))
    r = identify([bytes.fromhex(f.replace(" ", "")) for f in c["frames"]])
    assert len(c["frames"]) == 24 and r["confidence"] == "high" and r["n_candidates_validating"] == 1
    m = r["matches"][0]
    assert (m["algo"], m["poly"], m["init"], m["reflected"], m["xorout"], m["endian"], m["start"]) == \
           ("crc16", 0x8005, 0, False, 0, "little", 0)


def test_crsf_single_real_frame_outer_found_inner_not():
    from reverse.identify import identify
    from reverse.infer import _crc_generic
    f = bytes.fromhex("c80832eeea1005362" "6a0")
    assert _crc_generic(f[2:-1], 8, 0xD5, 0, False, 0) == f[-1] == 0xA0         # both CRCs hold on the real frame
    assert _crc_generic(f[2:-2], 8, 0xBA, 0, False, 0) == f[-2] == 0x26
    r = identify([f])
    assert r["confidence"] == "low" and r["matches"][0]["poly"] == 0xD5
    assert r["nested"] is None and not r["poly_sweep"]                           # one frame cannot pay for the sweep


def test_nested_crc_and_poly_sweep_synthetic():
    """SYNTHETIC CRSF-shaped frames (random byte) with the documented polynomials D5 and BA."""
    import random
    from reverse.identify import identify
    from reverse.infer import _crc_generic
    rnd = random.Random(3)
    frames = []
    for _ in range(4):
        body = bytes([0xC8, 9, 0x32, 0xEE, 0xEA, 0x10, 0x05, rnd.randrange(256)])
        b2 = body + bytes([_crc_generic(body[2:], 8, 0xBA, 0, False, 0)])
        frames.append(b2 + bytes([_crc_generic(b2[2:], 8, 0xD5, 0, False, 0)]))
    r = identify(frames)
    assert r["matches"][0]["poly"] == 0xD5
    assert r["nested"] and r["nested"]["matches"][0]["poly"] == 0xBA and r["nested"]["poly_sweep"]


def test_bacnet_header_crc_found_mid_frame():
    from reverse.identify import identify
    full = bytes.fromhex("55ff051000000 6e8010301040101e399".replace(" ", ""))
    hdr = bytes.fromhex("55ff067f02001d90")
    r = identify([full, hdr])
    assert r["matches"] == []                       # nothing at the end of the frames
    h = r["header_checks"][0]
    assert (h["algo"], h["poly"], h["init"], h["reflected"], h["xorout"], h["start"], h["header_len"]) == ("crc8", 0x81, 0xFF, True, 0xFF, 2, 8)
    assert h["confidence"] == "low"                 # 2 frames cannot earn more


def test_header_checks_need_two_frames():
    from reverse.identify import identify
    assert identify([bytes.fromhex("55ff051000000 6e8010301040101e399".replace(" ", ""))])["header_checks"] == []


def test_ascii_hex_checks_and_conventions_differ():
    from reverse.identify import identify
    seplos = identify([b"~20004642E00201FD35\r"])["ascii_hex_checks"]
    pace = identify([b"~1203400356ABCEFEFC72\r"])["ascii_hex_checks"]
    assert [(h["algo"], h["width_chars"], h["start"]) for h in seplos] == [("ascii_hex_invsum", 4, 1)]
    assert [(h["algo"], h["width_chars"], h["start"]) for h in pace] == [("ascii_hex_negsum", 4, 1)]
    # the typo variant printed in the Seplos manual does not satisfy either convention
    assert identify([b"~1203400456ABCEFEFC72\r"])["ascii_hex_checks"] == []


def test_ascii_hex_ignores_binary_and_plain_text():
    from reverse.identify import identify
    assert identify([bytes([0xAA, 1, 2, 3, 4, 5, 6, 7, 8])])["ascii_hex_checks"] == []


def test_teltonika_padded_crc16():
    from reverse.identify import identify
    a = bytes.fromhex("000000000000003608010000016B40D8EA30010000000000000000000000000000000105021503010101425E0F01F10000601A014E0000000000000000010000C7CF")
    b = bytes.fromhex("000000000000002808010000016B40D9AD80010000000000000000000000000000000103021503010101425E100000010000F22A")
    r = identify([a, b])
    assert r["matches"] == []                      # nothing at the plain end of the frames
    m = r["padded_check"]["matches"][0]
    assert (m["algo"], m["poly"], m["init"], m["reflected"], m["xorout"], m["start"], m["endian"]) == ("crc16", 0x8005, 0, True, 0, 8, "big")
    assert identify([a])["padded_check"] is None   # one frame is not enough to claim a pad


def test_header_checks_ignore_constant_prefixes():
    """Teltonika frames start with five zero bytes: a check over constant data validates trivially and proves nothing."""
    from reverse.identify import identify
    a = bytes.fromhex("000000000000003608010000016B40D8EA30010000000000000000000000000000000105021503010101425E0F01F10000601A014E0000000000000000010000C7CF")
    b = bytes.fromhex("000000000000002808010000016B40D9AD80010000000000000000000000000000000103021503010101425E100000010000F22A")
    assert identify([a, b])["header_checks"] == []


def test_report_is_readable_and_honest():
    from reverse.identify import identify
    from reverse.report import format_report, found_anything
    r = identify([b"~20004642E00201FD35\r"])
    text = "\n".join(format_report(r))
    assert "inverted sum of ASCII characters" in text and "low confidence" in text
    assert found_anything(r)
    # a frame set with no checksum at all: the report says so instead of inventing a rule
    import os
    junk = [bytes((i * 37 + j * 91 + 5) % 251 for j in range(40)) for i in range(6)]
    rj = identify(junk)
    assert found_anything(rj) == bool(rj["matches"] or rj.get("header_checks") or rj.get("padded_check") or rj.get("seeded_crc16") or rj.get("nested") or rj.get("ascii_hex_checks"))


def test_dsmr_crc16_over_ascii_text():
    """DSMR P1: CRC-16/ARC over the text from '/' up to and including '!', written as 4 hex characters."""
    import yaml
    from pathlib import Path
    from reverse.identify import identify
    c = yaml.safe_load((Path(__file__).resolve().parents[1] / "corpus/cases/dsmr_p1_v42.yaml").read_text())
    telegram = bytes.fromhex(c["frames"][0])
    assert telegram.startswith(b"/KFM5KAIFA-METER") and telegram.endswith(b"!6796\r\n")
    hits = identify([telegram])["ascii_hex_checks"]
    crc = [h for h in hits if h["algo"] == "ascii_hex_crc16"]
    assert crc and (crc[0]["poly"], crc[0]["init"], crc[0]["reflected"], crc[0]["xorout"], crc[0]["start"]) == (0x8005, 0, True, 0, 0)
    assert crc[0]["confidence"] == "low"          # one frame, 16 bits of evidence, minus the tries
    corrupted = telegram.replace(b"001581.123", b"001581.124")
    assert not [h for h in identify([corrupted])["ascii_hex_checks"] if h["algo"] == "ascii_hex_crc16"]
