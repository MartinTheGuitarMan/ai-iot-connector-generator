// Verifies the Node-RED flow's inline function-node logic without running
// Node-RED itself: extracts the actual "func" string from flows.json,
// executes it the way Node-RED would (msg in, msg/null out), and checks it
// behaves identically to nodered/canonical_to_line_protocol.js — the
// reference implementation these tests also exercise directly.
//
// Run with: node tests/test_nodered_flow.js

"use strict";

const assert = require("node:assert");
const fs = require("node:fs");
const path = require("node:path");

const ROOT = path.resolve(__dirname, "..");
const { toLineProtocol } = require(path.join(ROOT, "nodered", "canonical_to_line_protocol.js"));

function loadFlowFunction(nodeId) {
  const flows = JSON.parse(fs.readFileSync(path.join(ROOT, "nodered", "flows.json"), "utf8"));
  const node = flows.find((n) => n.id === nodeId);
  assert(node, `no node with id ${nodeId} in flows.json`);
  assert.strictEqual(node.type, "function");
  // eslint-disable-next-line no-new-func
  return new Function("msg", "node", node.func);
}

const runFlowFn = loadFlowFunction("fn-to-line-protocol");

function runFlow(reading) {
  const warnings = [];
  const mockNode = { warn: (m) => warnings.push(m) };
  const result = runFlowFn({ payload: reading }, mockNode);
  return { result, warnings };
}

let failures = 0;
function check(name, fn) {
  try {
    fn();
    console.log(`ok - ${name}`);
  } catch (err) {
    failures += 1;
    console.error(`FAIL - ${name}`);
    console.error(err);
  }
}

const OBS_READING = {
  device_id: "site-1",
  protocol: "wx_ascii",
  message_type: "WXOBS",
  timestamp: "2026-09-21T12:35:19Z",
  measurements: [
    { name: "temperature_c", value: 23.5, unit: "celsius" },
    { name: "humidity_pct", value: 60, unit: "percent" },
    { name: "pressure_hpa", value: null, unit: "hpa" },
  ],
  location: { lat: 48.1173, lon: 11.5167 },
};

const EMPTY_READING = {
  device_id: "d",
  protocol: "p",
  message_type: "m",
  timestamp: "2026-09-21T12:35:19Z",
  measurements: [{ name: "hdop", value: null }],
  location: null,
};

check("reference implementation produces expected line protocol", () => {
  const line = toLineProtocol(OBS_READING);
  assert.strictEqual(
    line,
    "iot_reading,device_id=site-1,protocol=wx_ascii,message_type=WXOBS temperature_c=23.5,humidity_pct=60i,lat=48.1173,lon=11.5167 1789994119"
  );
});

check("reference implementation drops null-valued measurements", () => {
  const line = toLineProtocol(OBS_READING);
  assert(!line.includes("pressure_hpa"));
});

check("reference implementation returns null when there are no fields", () => {
  assert.strictEqual(toLineProtocol(EMPTY_READING), null);
});

check("flow's inline function produces the same line protocol payload as the reference", () => {
  const { result } = runFlow(OBS_READING);
  assert(result, "flow function returned null/undefined for a reading with fields");
  assert.strictEqual(result.payload, toLineProtocol(OBS_READING));
});

check("flow's inline function sets the InfluxDB write URL, method, and auth header", () => {
  const { result } = runFlow(OBS_READING);
  assert.strictEqual(result.method, "POST");
  assert.strictEqual(
    result.url,
    "http://influxdb:8086/api/v2/write?org=iot&bucket=telemetry&precision=s"
  );
  assert.strictEqual(result.headers.Authorization, "Token devtoken-not-a-secret");
});

check("flow's inline function drops (returns null with a warning) when there is nothing to write", () => {
  const { result, warnings } = runFlow(EMPTY_READING);
  assert.strictEqual(result, null);
  assert.strictEqual(warnings.length, 1);
});

if (failures > 0) {
  console.error(`\n${failures} test(s) failed`);
  process.exit(1);
}
console.log("\nall tests passed");
