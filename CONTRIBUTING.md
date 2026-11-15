# Contributing

This is a research tool: capture frames in, protocol grammar out. The most useful contribution is a real protocol
the tool gets wrong, or one it gets right that nobody has checked yet.

## Add a corpus case (about 10 minutes)

1. Find real frames: a datasheet example, a forum capture, a test vector in a public repo, or your own capture.
   No invented frames and no frames recalled from memory. If you cannot point to where they came from, they do not go in.
2. Copy `corpus/CASE_TEMPLATE.yaml` to `corpus/cases/<name>.yaml` and fill it in. Put the source URL in `source`.
3. Check the documented rule yourself by computing it. **The frame beats the page**: published parameters are sometimes
   wrong (see the WHOOP and Seplos cases). If the page and the frame disagree, the case documents the frame.
4. Run `python scripts/run_corpus.py`. If the tool misses your rule, that is a good case: keep it, and say in `note`
   what failed. Do not change the reference to match the tool.
5. Run the tests: `PYTHONPATH=. python -m pytest -q`. (`tests/test_influx_sink.py` and `tests/test_pipeline.py` need
   `influxdb_client`.)

## Changing the tool (`reverse/`)

- A new pass must price its extra tries into `confidence` (bits = frames x check bits, minus log2 of the tries). One or
  two frames stay `low`.
- Results from the tolerant and sweep passes are never learned into `reverse/knowledge.json`.
- Every pass gets a regression test in `tests/test_corpus.py`, and every failure goes in the README's "Known failures".
- Keep the corpus honest: do not add a family just to raise the score. More families mean more chance matches on tiny captures.

## What not to send

Frames from systems you are not allowed to share, anything with credentials or personal data, and copyrighted
documents (link to them; do not paste them). Short frame transcriptions with a source link are fine. By contributing you
agree your contribution is under the repo's MIT licence.

## Where to start

`HANDOFF.md` lists the next steps, and the README's "Known failures" section lists what is missing.
