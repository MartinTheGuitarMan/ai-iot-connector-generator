# ai-iot-connector-generator

Experiment: can an LLM be used to build Python connectors for IoT device
protocols that translate into a canonical IoT structure?

The example domain is multi-vendor weather station telemetry, picked
deliberately: unlike something like Modbus-over-industrial-PLC, where the
transport is already a mature, decades-solved problem, weather sensor
hardware has no real standard. Every vendor's ASCII sentence dialect (or,
for some stations, Modbus register map) differs, and unifying them into
one schema is still mostly hand-coded, per-integration work — which is
exactly the kind of fragmentation this generator is meant to paper over.

## Stage 1 (this commit): template-based connector generator

Rather than hand-writing (or LLM-writing) each protocol connector directly,
this stage builds the **generator** first: a small deterministic tool that
turns a declarative protocol spec (YAML) into a standalone Python connector
module, so the connector-writing knowledge lives in the spec + codegen
backend, not in one-off hand-written parsers.

```
canonical.py            canonical IoT reading shape every connector targets
generator/spec.py       ProtocolSpec model (loads + validates YAML specs)
generator/codegen.py    renders a ProtocolSpec into connector source
generator/cli.py        `python -m generator.cli generate --spec ... --out ...`
specs/                  example protocol specs
connectors/             generated connector modules (checked in, regeneratable)
scripts/                sample data generator
sample_data/            synthetic telemetry capture (generated, checked in)
pipeline/                CanonicalReading -> InfluxDB point mapping + runner
nodered/                Node-RED flow: alternative ingest path, same canonical contract
grafana/provisioning/   auto-provisioned InfluxDB datasource + dashboard
docker-compose.yml      local InfluxDB + Grafana + Node-RED stack
tests/                  generator, connector, pipeline, and sample-data tests
```

Three transports are implemented. None is tied to any one protocol
standard or vendor — they're general field-addressing strategies that
cover most real device wire formats:

- **delimited_text** — comma-delimited, field-index addressed (the kind
  of ASCII sentence a lot of serial sensor hardware emits).
  - `specs/weather_station_obs.yaml` → `connectors/weather_station_obs.py`:
    parses `$..WXOBS` sentences (checksum-verified) into site position,
    temperature, humidity, and pressure. Timestamp comes from an external
    `reference_date` parameter, since the sentence carries only time-of-day
    (`hms_time` field type).
  - `specs/weather_station_obs_full.yaml` → `connectors/weather_station_obs_full.py`:
    parses `$..WXOBSF` sentences into position and wind speed/direction.
    Unlike the plain reading, this one carries its own `ddmmyy` date field
    (`canonical.date_field` in the spec, `ddmmyy_date` field type), so its
    timestamp is self-contained and its `parse()` takes no `reference_date`.
    A 2-digit year is inherently ambiguous across centuries — the generator
    resolves it to 2000-2099, documented (and tested) as a known limitation
    of that date encoding, not a parsing bug.
- **register_map** — address/scale addressed (Modbus-style). Example:
  `specs/weather_station_modbus.yaml` → `connectors/weather_station_modbus.py`:
  a *different vendor's* weather station, exposed over Modbus holding
  registers instead of ASCII sentences (rainfall, wind gust, battery
  voltage, UV index) — the same real-world fragmentation story, a second
  wire format for the same category of device.

- **framed_binary** — a byte stream of self-delimiting frames, fields addressed by
  byte offset (`uint8`/`int16_le`/`uint32_be`/..., `scale`, `bias`). The shape of many
  legacy serial/RS-485 device protocols. A frame is delimited by a length byte *or* a
  constant `fixed_length`; may start with `sync` bytes (or have none, then the stream
  re-synchronises on the check value); may have a message-type byte (or be
  single-message); carries a check value (`crc16` with any polynomial/init/reflection,
  `fletcher8`, `sum8`, `xor8`) and optionally constant `trailer` bytes after it.
  Generated modules expose `iter_frames(stream)` (resyncs after noise, drops corrupt
  frames) and `parse(frame, device_id, timestamp)`; the wire has no clock, so the caller
  supplies the timestamp. `specs/cressi_nepto_identity.yaml` is a real device (a dive
  computer) written from frames published in a reverse-engineering write-up, checked
  in tests against those exact bytes. See "Reverse: capture → spec" below.

Every generated connector's `parse(...)` returns a `canonical.CanonicalReading`
(device_id, protocol, message_type, ISO 8601 timestamp, location, a list of
named/unit-tagged measurements, and the original raw field values) —
one shape regardless of source protocol or vendor.

Regenerate a connector after editing its spec:

```
pip install -r requirements.txt
python -m generator.cli generate --spec specs/weather_station_obs.yaml --out connectors/weather_station_obs.py
python -m pytest -q
```

Generated modules are self-contained: they import only `canonical` and the
stdlib, so they can be committed and shipped without depending on
`generator/` or PyYAML at runtime.

## Why generator-first

The generator is a deterministic backbone, testable independent of any LLM
call: given a spec, the output is byte-for-byte reproducible (verified —
regenerating a connector produces an identical file). That gives a stable
target for the next stage to build against, and a way to tell "the LLM got
the mapping wrong" apart from "the codegen is non-deterministic/buggy."

## Sample data

`scripts/generate_sample_data.py` generates a synthetic but plausible
telemetry capture: one fixed weather station site emitting paired ASCII
observation sentences, plus a second vendor's station at the same site
reporting over Modbus. It's seeded (deterministic, reproducible) and writes:

```
sample_data/weather_obs_log.txt           60 sentences (30 WXOBS + 30 WXOBSF)
sample_data/weather_station_modbus.jsonl  30 register snapshots
```

This is fixture data for exercising the pipeline end-to-end — it is
generated *from* the connectors' own spec assumptions, so it proves the
generator is internally consistent, not that real hardware matches those
assumptions. See "What's actually verified" below.

## Pipeline: connectors → canonical → InfluxDB

```
pipeline/influx_sink.py    CanonicalReading -> influxdb_client.Point
pipeline/run_pipeline.py   reads sample_data/, runs it through the
                            connectors, writes points to InfluxDB
```

All readings share one InfluxDB measurement, `iot_reading`; protocol
and message type are tags, not the measurement name, so a single query can
span every protocol/vendor for a device.

```
# Print line-protocol output, no InfluxDB required:
python -m pipeline.run_pipeline --dry-run

# Write to a real InfluxDB (see the docker-compose stack below):
INFLUX_TOKEN=devtoken-not-a-secret python -m pipeline.run_pipeline
```

## Local test environment: InfluxDB + Grafana + Node-RED

```
docker compose up -d
```

- **InfluxDB** (`localhost:8086`) — org `iot`, bucket `telemetry`,
  admin token `devtoken-not-a-secret` (a local-dev placeholder baked into
  `docker-compose.yml` and `grafana/provisioning/datasources/influxdb.yaml`
  — not meant to be a real secret; change it if this ever runs anywhere
  beyond your own machine).
- **Grafana** (`localhost:3000`, anonymous viewer access enabled) —
  auto-provisioned with the InfluxDB datasource and an "IoT Telemetry
  Overview" dashboard (temperature, humidity, wind speed, rainfall from
  the second vendor's Modbus station, latest observation).
- **Node-RED** (`localhost:1880`) — an alternative ingest path to
  `pipeline/run_pipeline.py`: `POST /ingest/reading` with a
  `CanonicalReading.to_dict()`-shaped JSON body, and its one function node
  converts it to InfluxDB line protocol and writes it, exactly like
  `pipeline/influx_sink.py` does in Python. This demonstrates the canonical
  schema working as a real cross-language contract, not just a Python-internal
  convenience. Try it once the stack is up:

  ```
  curl -X POST http://localhost:1880/ingest/reading \
    -H 'Content-Type: application/json' \
    -d '{"device_id":"site-1","protocol":"wx_ascii","message_type":"WXOBS",
         "timestamp":"2026-09-21T12:35:19Z",
         "measurements":[{"name":"temperature_c","value":23.5,"unit":"celsius"}],
         "location":{"lat":48.1173,"lon":11.5167}}'
  ```

Then load the sample data into InfluxDB and open Grafana:

```
INFLUX_TOKEN=devtoken-not-a-secret python -m pipeline.run_pipeline
open http://localhost:3000   # dashboard: IoT Telemetry Overview
```

### What's actually verified, and what isn't

This sandbox can run the Docker CLI and daemon, but its network policy
blocks pulls from Docker Hub's image CDN (`production.cloudfront.docker.com`
returns 403 from the proxy) — confirmed, not assumed. So none of this stack
has been visually confirmed running end-to-end here. What *was* verified
in this environment, without a live container:

- `docker compose config` resolves the compose file cleanly (services,
  volumes, `depends_on`, port mappings all structurally valid).
- `grafana/provisioning/**/*.{yaml,json}` all parse as valid YAML/JSON —
  the dashboard JSON was hand-authored, not exported from a running
  Grafana, so treat it as a good-faith starting point rather than a
  guaranteed-perfect import; it may need minor adjustment on first load.
- The Node-RED flow's logic doesn't need Node-RED to check: the exact
  `func` string is extracted straight out of `nodered/flows.json` and
  executed under plain Node.js in `tests/test_nodered_flow.js`, asserting
  it produces the same line protocol as `pipeline/influx_sink.py`'s Python
  implementation for the same input, including the edge case (a reading
  with nothing to write). Run it with `node tests/test_nodered_flow.js`.
- The Python side of the pipeline (sample data → connectors → canonical →
  `Point.to_line_protocol()`) is fully covered by `pytest` without a live
  InfluxDB — `Point` serialization is pure and needs no network connection.

The images are official multi-arch builds (`influxdb:2`, `grafana/grafana`,
`nodered/node-red`) and should pull and run natively on Apple Silicon.
Once it's up on your machine, the honest verification loop is: confirm
`docker compose ps` shows all three healthy, run the pipeline, open
Grafana and see the panels populate, and try the `curl` above against
Node-RED and confirm the point shows up in Grafana too.

## Reverse: capture → spec (framed_binary)

Stage 2 below starts from *documentation*. This is the other starting point: no
documentation at all, just a captured byte stream and a few readings a human noted
off the device's own display.

```
reverse/infer.py            stage 1: frame structure from the bytes alone (sync, length
                            field or constant size, trailer, checksum family + parameters,
                            sequence counter, type byte, payload size)
                            stage 2: fit every (offset, width, endianness, signedness)
                            candidate to the anchors: value = raw * scale + bias
reverse/knowledge.py        what earlier validated runs learned (see below)
reverse/cli.py              python -m reverse.cli infer | learn | knowledge
scripts/gen_legacy_demo.py  simulates an unknown device with a HIDDEN protocol and writes
                            capture.bin + anchors.yaml + truth.csv (4 profiles, below)
tests/test_framed_binary.py end-to-end: infer from capture+anchors only, generate connectors,
                            score every decoded value against truth.csv
```

Inferred specs go through the same `ProtocolSpec` loader and codegen as hand-written
ones (`specs/vessel_legacy_*.yaml` → `connectors/vessel_legacy_*.py` are checked-in
output of the demo). Anchors look like
`{message: engine, field: rpm, unit: rpm, ordinal: 120, value: 1480}`, where `ordinal`
is the frame's (unwrapped) sequence counter, or its index among the valid frames when
the protocol has none.

The simulated devices are modelled on shapes that real devices have and that broke the
first version of the tool:

| profile | shape |
|---|---|
| `vessel` | sync `7E C3`, length byte, type byte, sequence counter, CRC-16/MODBUS |
| `vessel_oddcrc` | the same layout with an obscure CRC polynomial (in no catalogue) |
| `cressi` | 3-byte sync, length, command, CRC-16/XMODEM, then a constant end byte; no sequence counter |
| `apc` | constant 19-byte frames: **no sync, no length, no type byte**, Fletcher-8 check (not a CRC) |

### It gets smarter with use

`reverse/knowledge.json` (plain JSON, meant to be read and diffed) records what *validated*
runs established, and later runs use it:

- **checksums** — exact parameter sets that validated real frames are tried first, and
  their polynomials extend the built-in catalogue. A device with an unusual CRC only has
  to be identified once.
- **framings** — a capture that fits a known layout is recognised immediately, without
  the heuristics.
- **encodings** — when the anchors cannot tell two encodings apart (int8 vs uint8 when a
  temperature never went below zero), the encoding seen for that unit in more distinct
  protocols breaks the tie.

```
python -m reverse.cli infer ...              # records the run if it validated
python -m reverse.cli learn --spec s.yaml --capture c.bin   # teach it a device it cannot work out
python -m reverse.cli knowledge              # what has it learned so far
```

Rules that keep it honest:

- Only validated runs teach anything: a checksum must have matched the real frames and
  every anchor must have fitted. `learn` refuses a spec whose generated connector does
  not explain the capture (>= 20 frames, >= 90% of the bytes).
- A guess is never recorded as evidence. A prior can only choose between representations
  of the same bytes at the same offset and scale (signedness, width), never override what
  the anchors show, and a fit that needed a tie-break is not written back, otherwise the
  knowledge base would reinforce its own habits (there is a regression test for exactly
  that bug).
- Evidence is counted per distinct protocol, not per run.

What this does and doesn't show:

- The inference never sees the device's spec; the truth is only used to score. That
  avoids the circularity noted above ("generated from the connectors' own assumptions").
- Ambiguity is resolved by the data, not guessed, and reported when it remains: sub-zero
  anchors force `int8`, anchors spanning a low-byte rollover fix a counter's byte order.
  A field whose high byte is always zero reads equally well as `uint8` or `uint16_le`;
  the narrower one is chosen. Choose anchors that span the range.
- Still easy mode: no encryption, no bit-packed fields, no multi-frame messages, 1-byte
  checksums and CRC16 only (no CRC32/CRC8 yet), frames at most ~128 bytes for the no-sync
  case, and a type byte in a constant-size stream must switch value often (interleaved
  messages) to be told apart from a slowly drifting sensor.
- Deterministic heuristics plus the knowledge base, no LLM. Only two frames of real
  hardware (the Cressi write-up) have been run through this; real captures are the actual test.

## Stage 2 (planned, not yet built): LLM-assisted spec/connector authoring

Have an LLM draft the declarative spec (or the connector directly) from raw,
unstructured protocol documentation — a sensor's ASCII sentence table, a
vendor's Modbus register map datasheet — instead of a human writing YAML by
hand. This is the part that actually tests the "can an LLM build IoT device
connectors" question; stage 1 is the harness it will be evaluated against
(does the generated spec validate, does the generated connector's test
output match real captured device data).
