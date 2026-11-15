# Handoff: reverse/ (capture to spec), for Claude Code

Branch `framed-binary-reverse`, head at the time of writing: see `git log`. Repo: MartinTheGuitarMan/ai-iot-connector-generator.

## State
- Corpus: 30 of 30 real-frame cases find the documented rule (`python scripts/run_corpus.py`), 13 at high confidence.
- Tests: `PYTHONPATH=. python -m pytest -q --ignore=tests/test_influx_sink.py --ignore=tests/test_pipeline.py` gives 123 passed. The two ignored modules need `influxdb_client` (not installed in the chat sandbox; install it locally and drop the ignores).
- Stop condition reached: coverage (30 cases, more than 8 families, every failure written up). Saturation and budget did not trigger: each of the last rounds exposed a new failure class.

## Layout that matters
- `reverse/identify.py`: `identify(frames, kb=None, max_results=5, _depth=0)`. Passes in order: unstuffing, strict checksum search, solved-constant negsum, CRC-8 poly sweep, tolerant (>=70% of >=5 frames), seeded CRC-16, nested, header checks (mid-frame), ASCII-hex checks, zero-padded field. Result keys are listed in README.
- `reverse/infer.py` (checksum families, `_crc_generic`), `reverse/stuffing.py`, `reverse/textlayer.py`, `reverse/knowledge.py` (validated-only learning), `reverse/locate.py`, `reverse/canlog.py`.
- `corpus/cases/*.yaml` (30 cases: id, source, doc_rule, note, frames, reference). Scorer: `scripts/run_corpus.py`; special reference keys: `stuffing`, `expected_outliers`, `append` (seeded), `header_len`, `padded`, `algo: ascii_hex_*`.
- `tests/test_corpus.py` holds the regression tests for every pass.

## Rules this project learned
1. Real frames only, with the source URL in the case. Verify the documented rule by computing before trusting it.
2. The frame beats the page (WHOOP, Seplos manual prose both wrong). Never fix a case by changing the reference to match the tool.
3. Every new pass must price its extra tries into `confidence` (bits = frames x check bits minus log2(tries)); single tiny frames stay `low`.
4. Tolerant and swept results are never learned into `knowledge.json`.
5. Write up each failure in the case `note` and the README "Known failures" list.

## Next steps (suggested order)
1. IEC 62056-21 meter readout (BCC, XOR after STX): needs real captured bytes; none found online yet.
2. More real frames for the weak cases: MS/TP with payloads, CRSF (3+ frames for the inner CRC), Davis LOOP, Hayes-style text, LoRa/Meshtastic.
3. Candidate ranking prior to cut chance matches on tiny captures.
4. Length-field search for 4-byte fields (Teltonika); Dynamixel insertion stuffing.
5. Codegen for the new features (stuffed, seeded, nested, text-wrapped, per-block, CAN, header, ASCII-hex, padded checks): `generator/spec.py`, `generator/codegen.py`.
6. Real vessel captures (Simrad, Kongsberg, TSS1) when available; this is the "wall" condition.
7. Deferred on purpose: the review step of the "universal translator" (LLM proposes meaning from docs, a domain expert signs off before any connector is trusted).

## Environment notes
- Chat sandbox had no pip/npm and allowlisted network; GitHub clones need the repo attached. In Code on the Mac none of that applies.
- Clones used by some tests (`libmbus`, `n2k`) are fetched by the tests and skipped when absent.
