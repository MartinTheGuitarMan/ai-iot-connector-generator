"""Mine a cloned repository for byte-string literals and see which framing groups share a checksum rule.

Treats the repo as untrusted data: it only reads text files and never imports or runs anything.
Frames are grouped by first byte and length band; a group is reported when one rule (from the
tool's checksum families) validates at least `--min-frac` of >= `--min-frames` frames."""
import argparse, collections, re, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from reverse.infer import _check_ok, _checksum_candidates
from reverse.knowledge import KnowledgeBase

PATS = [re.compile(r'b"((?:\\x[0-9a-fA-F]{2}){6,600})"'), re.compile(r"b'((?:\\x[0-9a-fA-F]{2}){6,600})'"),
        re.compile(r'"((?:\\x[0-9a-fA-F]{2}){6,600})"'),
        re.compile(r'fromhex\(\s*[bru]*["\']([0-9a-fA-F ]{12,1200})["\']'),
        re.compile(r'["\']((?:[0-9a-fA-F]{2}[ :]){7,599}[0-9a-fA-F]{2})["\']'),
        re.compile(r'["\']([0-9a-fA-F]{16,1200})["\']'),
        re.compile(r'[\{\[]\s*((?:0x[0-9a-fA-F]{2}\s*,\s*){5,599}0x[0-9a-fA-F]{2})\s*,?\s*[\}\]]')]
EXT = {".py", ".c", ".h", ".cpp", ".hpp", ".rs", ".go", ".java", ".js", ".ts", ".md", ".txt", ".ino", ".cs"}

def literals(root: Path):
    out = set()
    for p in root.rglob("*"):
        if p.suffix.lower() in EXT and p.is_file() and p.stat().st_size < 2_000_000 and ".git" not in p.parts:
            try: s = p.read_text(encoding="utf8", errors="ignore")
            except OSError: continue
            for pat in PATS:
                for m in pat.finditer(s):
                    raw = m.group(1).replace("\\x", " ").replace("0x", " ").replace(":", " ")
                    hx = raw.split() if " " in raw.strip() else re.findall(r"[0-9a-fA-F]{2}", raw)
                    if any(len(h) != 2 for h in hx): continue
                    if len(hx) >= 6: out.add(bytes.fromhex("".join(hx)))
    return out

def best_rule(frames, min_frac, kb):
    best = None
    for tl in (0, 1):
        for p in _checksum_candidates(1, min(len(f) for f in frames) // 2, kb):
            n = sum(_check_ok(f, p, tl) for f in frames if len(f) > p["start"] + 3)
            if n >= min_frac * len(frames) and (best is None or n > best[0]):
                best = (n, tl, p)
    return best

if __name__ == "__main__":
    ap = argparse.ArgumentParser(); ap.add_argument("repo", type=Path)
    ap.add_argument("--min-frames", type=int, default=8); ap.add_argument("--min-frac", type=float, default=0.8)
    ap.add_argument("--pool-frac", type=float, default=0.25)
    a = ap.parse_args(); kb = KnowledgeBase()
    lits = literals(a.repo)
    groups = collections.defaultdict(list)
    for b in lits: groups[(b[:2].hex(), 0)].append(b)
    groups[("pool", 0)] = list(lits)
    print(f"{a.repo.parent.name}: {len(lits)} byte strings, {len(groups)} groups")
    for k, fs in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        if len(fs) < a.min_frames: continue
        r = best_rule(fs, a.pool_frac if k[0] == 'pool' else a.min_frac, kb)
        if r:
            n, tl, p = r
            print(f"  {'ALL strings' if k[0]=='pool' else 'sync-ish prefix ' + k[0]}: {n}/{len(fs)} validate  trailer={tl}  {p}")
