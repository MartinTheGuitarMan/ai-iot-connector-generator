"""Run few-frame identification blind over corpus/cases/*.yaml.

Scored per case: does the top-ranked rule validate every published frame, and is the
documented rule among the candidates? Equivalent rules count (e.g. ~sum from byte 0 ==
-sum from byte 1 when byte 0 is 0xFF)."""
import sys, glob, yaml
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from reverse.identify import identify
from reverse.infer import checksum as chk, _check_ok

def reference_rule(ref):
    a = ref["algo"]; p = dict(start=ref.get("start", 0))
    if a == "fletcher8": p.update(algo="fletcher8_256" if ref.get("mod") == 256 else "fletcher8", endian="little")
    elif a == "sum8": p.update(algo="sum8", init=0, xorout=ref.get("xorout", 0))
    elif a == "sum16": p.update(algo="sum16", init=0, xorout=0, endian="big")
    elif a == "negsum8":
        p.update(algo="negsum8")
        if "init" in ref: p.update(init=ref["init"], xorout=0)
    elif a == "negsum16": p.update(algo="negsum16", endian="big")
    elif a in ("crc8", "crc16", "crc32"):
        p.update(algo=a, poly=ref["poly"], init=ref["init"], reflected=ref["reflected"],
                 xorout=ref["xorout"])
        if a != "crc8": p["endian"] = ref.get("endian", "little")
    return p, ref.get("trailer", 0)

def run():
    rows = []
    for path in sorted(glob.glob(str(ROOT / "corpus/cases/*.yaml"))):
        c = yaml.safe_load(open(path)); frames = [bytes.fromhex(f.replace(" ", "")) for f in c["frames"]]
        r = identify(frames)
        if c.get("stuffing"):
            from reverse.stuffing import unstuff, detect
            frames = unstuff(frames, detect(frames))
        rp, rt = reference_rule(c["reference"])
        ref_ok = all(_check_ok(f, rp, rt) for f in frames)
        top = r["matches"][0] if r["matches"] else None
        top_ok = bool(top) and all(_check_ok(f, {k: v for k, v in top.items() if k != "trailer_len"}, top["trailer_len"]) for f in frames)
        in_list = any({k: v for k, v in m.items() if k != "trailer_len"} == rp and m["trailer_len"] == rt for m in r["matches"])
        rows.append(dict(id=c["id"], n=len(frames), conf=r["confidence"], cands=r["n_candidates_validating"],
                         ref_valid=ref_ok, top=top and (top["algo"], top["start"], top["trailer_len"]),
                         top_validates=top_ok, reference_found=in_list))
    return rows

if __name__ == "__main__":
    rows = run()
    for x in rows:
        print(f'{x["id"]:24s} n={x["n"]:2d} conf={x["conf"]:6s} cands={x["cands"]:2d} top={x["top"]} reference_found={x["reference_found"]}')
    print(f'\nreference rule found: {sum(x["reference_found"] for x in rows)}/{len(rows)}; '
          f'high-confidence cases: {sum(x["conf"]=="high" for x in rows)}')
