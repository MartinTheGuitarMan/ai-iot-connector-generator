# llm-iot-connector-generator

Experiment: can an LLM be used to build Python connectors for IoT device
protocols that translate into a canonical IoT structure?

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

Two transports are implemented. Neither is tied to any one protocol
standard or industry — they're general field-addressing strategies that
cover most real device wire formats:

- **delimited_text** — comma-delimited, field-index addressed (the kind
  of ASCII sentence a lot of serial sensor hardware emits).
  - `specs/process_sensor_reading.yaml` → `connectors/process_sensor_reading.py`:
    parses `$..PROC` sentences (checksum-verified) into site position,
    temperature, pressure, and flow rate. Timestamp comes from an external
    `reference_date` parameter, since the sentence carries only time-of-day
    (`hms_time` field type).
  - `specs/process_sensor_reading_full.yaml` → `connectors/process_sensor_reading_full.py`:
    parses `$..PROCF` sentences into position, vibration, and motor RPM.
    Unlike the plain reading, this one carries its own `ddmmyy` date field
    (`canonical.date_field` in the spec, `ddmmyy_date` field type), so its
    timestamp is self-contained and its `parse()` takes no `reference_date`.
    A 2-digit year is inherently ambiguous across centuries — the generator
    resolves it to 2000-2099, documented (and tested) as a known limitation
    of that date encoding, not a parsing bug.
- **register_map** — address/scale addressed (Modbus-style, common across
  industrial controllers regardless of what they're attached to). Example:
  `specs/plc_line_telemetry.yaml` → `connectors/plc_line_telemetry.py`,
  which decodes signed/unsigned 16- and 32-bit holding registers (line
  speed, motor current, motor temperature, uptime) with per-field scaling.

Every generated connector's `parse(...)` returns a `canonical.CanonicalReading`
(device_id, protocol, message_type, ISO 8601 timestamp, location, a list of
named/unit-tagged measurements, and the original raw field values) —
one shape regardless of source protocol.

Regenerate a connector after editing its spec:

```
pip install -r requirements.txt
python -m generator.cli generate --spec specs/process_sensor_reading.yaml --out connectors/process_sensor_reading.py
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
telemetry capture: one fixed production site emitting paired process
sensor sentences and PLC register snapshots once per simulated minute.
It's seeded (deterministic, reproducible) and writes:

```
sample_data/process_sensor_log.txt    60 sentences (30 PROC + 30 PROCF)
sample_data/plc_line_telemetry.jsonl  30 register snapshots
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
span every protocol for a device.

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
  Overview" dashboard (motor RPM, motor temp, line speed, process flow
  rate, latest sensor status).
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
    -d '{"device_id":"site-1","protocol":"industrial_ascii","message_type":"PROC",
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

## Stage 2 (planned, not yet built): LLM-assisted spec/connector authoring

Have an LLM draft the declarative spec (or the connector directly) from raw,
unstructured protocol documentation — a sensor's ASCII sentence table, a
PLC's register map, a manufacturer's Modbus datasheet — instead of a human
writing YAML by hand. This is the part that actually tests the "can an LLM
build IoT device connectors" question; stage 1 is the harness it will be
evaluated against (does the generated spec validate, does the generated
connector's test output match real captured device data).
