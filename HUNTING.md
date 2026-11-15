# HUNTING.md: how to hunt protocols, and why

Read this first if you are a Claude Code session asked to "go hunt more protocols".

## Why we hunt

iot-rosetta infers a spec from captured frames. The tool is only as good as the
real protocols it has been tested against. Every hunt does one of two things:

1. **The tool finds the rule.** One more protocol is proven, and goes in the README table.
2. **The tool fails.** This is the more valuable outcome. The failure shows what is missing, we fix the tool (or write the failure up in README "Known failures"), and the fix generalises to protocols we have not seen.

The hunt is therefore a test-driven loop for the tool. Collecting protocols is a by-product. Every protocol in the corpus is also open knowledge other IoT people can reuse.

## The rules (do not bend these)

- **Real frames only.** Bytes from a datasheet example, a vendor manual, a GitHub test file, a forum post or a real capture. Never invent a frame. Never write one from memory. Frames recalled from memory did not verify before and were discarded.
- **Verify by computing.** Before a case goes in, compute the checksum or CRC yourself and show that it matches the stored value. If it does not match, the source is wrong (manuals and spec examples often are: WHOOP, Seplos prose and DSMR spec examples all had errors). Exclude the case and note why. Do not massage the bytes until it fits.
- **The tool must find the rule itself.** The reference rule in the case file is the answer key. `identify()` never sees it.
- **Confidence is arithmetic.** frames x check bits, minus log2(candidates tried). One short frame is always "low". Do not inflate it.
- **Only validated results are learned.** No heuristic goes into the tool unless a verified case needs it.
- **Failures are written up openly** in README "Known failures", with what was tried.

## The loop

1. **Pick a family** we do not cover yet (see "Where to look").
2. **Find real frames** (see sources). Save the source URL.
3. **Verify by computing** the check yourself, outside the tool.
4. **Add a case:** copy `corpus/CASE_TEMPLATE.yaml` to `corpus/cases/<name>.yaml`. Fill in frames (hex), source URL, and the reference rule using the shapes in `scripts/run_corpus.py` (`stuffing`, `expected_outliers`, `append`, `header_len`, `padded`, `algo: ascii_hex_*` with `width_chars` and `start`).
5. **Run the scorer:** `python3 scripts/run_corpus.py`. It reports "reference rule found: N/N".
6. **If found:** done. Add a test only if it exercises a new convention.
7. **If not found:** read why, using `python3 -m reverse.cli identify --frames <file>`. Then either
   - extend `reverse/identify.py` (new pass or check shape), **or**
   - if it is a genuine limit (unknown polynomial, variable-length payload CRC, ...), write it up in README "Known failures".
8. **Guard against chance matches.** A new check family raises the chance-match rate on tiny captures. Keep the "evidence must beat tries" filter (`frames * check bits - log2(tries) > 0`). Re-run the whole corpus and tests after every tool change:
   `python3 scripts/run_corpus.py | tail -1`
   `PYTHONPATH=. python3 -m pytest -q --ignore=tests/test_influx_sink.py --ignore=tests/test_pipeline.py`
9. **Regenerate the README table:** `python3 scripts/run_corpus.py --markdown`, paste it between the table header and "*Regenerate with*". Update the protocol count in README and ROADMAP.
10. **Commit and push** to `framed-binary-reverse` and `main`: `git push origin framed-binary-reverse framed-binary-reverse:main`. End commit messages with the attribution trailers the session gives you.

## Where to look

- **GitHub:** test files and fixtures in protocol libraries (search for hex byte arrays next to a decoder). Prefer files that include the expected checksum.
- **Datasheets and vendor protocol PDFs** with worked example frames.
- **Forums** (Arduino, Home Assistant, marine/NMEA boards) where someone pastes a real capture.
- **Standards examples**, but treat them as suspect until computed.

Families still open, roughly by value:
- More DSMR variants (V5 `!6EEE`, Fluvius `!3AD7`): CRLF text, CRC-16/ARC over the text, 4 hex characters. Several frames would lift the DSMR case above "low".
- IEC 62056-21 (BCC). No real bytes found yet.
- BACnet MS/TP frames with payloads (payload CRC-16 is the known failure).
- Hayes-style text, LoRa/Meshtastic, more maritime (NMEA 2000 over serial, AIS wrappers, tank/level sensors, engine gateways).

## Hard limits to know about

- Unknown 16/32-bit polynomials cannot be recovered. Only the built-in sets are tried.
- Meters whose DSMR CRC drops leading zeros (`!B9F`) are not handled.
- Tiny captures (1 to 2 frames) always give chance candidates. Say so; do not hide it.
- No real vessel captures yet. If Martin supplies some, those outrank everything else.
- The review step ("a domain expert signs off on the meaning") is deliberately not built. It is on ROADMAP.md. Do not build it during a hunt.

## Not now

Knowledge graph, shared corpus, candidate-ranking prior: all on ROADMAP.md. Martin's words: "Not. Roadmap. Let's hunt for now."

## When to stop

Stop and report when one of these holds: saturation (new cases stop teaching the tool anything), the wall (the source is blocked or the family needs a capability that is a roadmap item), or Martin says stop. Report: protocols added, tool changes, and every failure.
