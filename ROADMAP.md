# Roadmap

Where iot-rosetta is going, in order of how soon. Nothing here is a promise; the corpus decides what gets built next
(a protocol the tool gets wrong beats a feature nobody asked for). Current gaps are listed in the README under "Known failures".

## Now: widen the evidence

- Grow the corpus from 31 to 50 real protocols. Priority families: smart meters (DSMR P1, IEC 62056-21), building
  automation, battery management, GPS trackers, industrial and manufacturing buses.
- Make contributing easy: a case is one YAML file ([CONTRIBUTING.md](CONTRIBUTING.md)).
- Keep every finding verified against a real frame, with the source linked. The frame beats the manual.

## Next: close known gaps

- **DSMR CRCs that drop leading zeros** (`!B9F` for `!0B9F`). The 4-digit form (CRC-16 over the ASCII text) is done.
- **Candidate ranking prior** to cut chance matches on one or two tiny frames.
- **Length fields wider than 2 bytes** (Teltonika's 4-byte length) and **payload CRC after a variable-length payload** (BACnet MS/TP).
- **Insertion-style byte stuffing** (Dynamixel 2.0).
- **Code generation** for the newer check types: stuffed frames, seeded and nested CRCs, text-wrapped frames, per-block CRCs, CAN,
  mid-frame, hex-text and zero-padded checks.
- **Real vessel captures** (Simrad, Kongsberg, TSS1) when available.

## Later: from grammar to meaning

- **Protocol knowledge graph.** Add optional `family`, `variant_of` and `contradicts` fields to corpus cases, then export to a
  graph once there are 50 to 100 cases across families. It gives priors for tiny captures (a new battery that matches the
  Pylontech family's framing starts from that family's rules) and records manual-vs-frame disagreements as data. Rule: an edge
  counts only if a real frame validates it, with the source attached.
- **The review step of the "universal translator".** An LLM proposes what fields mean from whatever documentation exists, and a
  domain expert signs off. No connector is trusted until a person who knows the equipment says yes. Deliberately not built yet.
- **A shared, data-only corpus** other projects can reference, with the tool as one consumer of it.

## Not planned

- Anything that decides field meaning without a human gate.
- Adding check families just to raise the score: more families mean more chance matches on small captures.
