# Raw protocol docs (Stage 2 inputs)

These are deliberately vendor-style prose and tables, not specs. They are the
*input* an LLM will be asked to turn into a `ProtocolSpec` YAML.

Ground truth for each doc is the hand-written spec in `specs/` plus the
captures in `sample_data/`. Nothing in these docs mentions the spec schema,
field type names, or generator internals. Each ends with an "Integration
brief" naming the canonical measurement names/units wanted, since those are a
house convention no vendor doc could supply.

| Doc | Reference spec |
|-----|----------------|
| `stx_wxobs.md` | `specs/weather_station_obs.yaml` |
| `stx_wxobsf.md` | `specs/weather_station_obs_full.yaml` |
| `aerolog_modbus.md` | `specs/weather_station_modbus.yaml` |
