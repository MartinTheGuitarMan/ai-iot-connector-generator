"use strict";

/**
 * Reference implementation of the same CanonicalReading -> InfluxDB line
 * protocol mapping that pipeline/influx_sink.py implements in Python, and
 * that nodered/flows.json's "canonical reading -> line protocol" function
 * node implements inline (Node-RED function nodes can't require() an
 * external module without extra container setup, so the logic is
 * duplicated there — tests/test_nodered_flow.js checks both stay
 * behaviorally identical).
 *
 * Same measurement name, same tag set, same field-dropping rule for
 * null-valued measurements, same lat/lon field names as the Python sink.
 */

function escapeTagValue(value) {
  return String(value).replace(/[ ,=]/g, "\\$&");
}

function formatFieldValue(value) {
  if (typeof value === "number" && Number.isInteger(value)) {
    return `${value}i`;
  }
  return String(value);
}

function toLineProtocol(reading) {
  const tags = [
    `device_id=${escapeTagValue(reading.device_id)}`,
    `protocol=${escapeTagValue(reading.protocol)}`,
    `message_type=${escapeTagValue(reading.message_type)}`,
  ].join(",");

  const fields = [];
  for (const m of reading.measurements || []) {
    if (m.value === null || m.value === undefined) continue;
    fields.push(`${m.name}=${formatFieldValue(m.value)}`);
  }
  if (reading.location) {
    fields.push(`lat=${reading.location.lat}`);
    fields.push(`lon=${reading.location.lon}`);
  }

  if (fields.length === 0) {
    return null; // caller decides how to handle "nothing to write"
  }

  const epochSeconds = Math.floor(new Date(reading.timestamp).getTime() / 1000);
  return `iot_reading,${tags} ${fields.join(",")} ${epochSeconds}`;
}

module.exports = { toLineProtocol, escapeTagValue, formatFieldValue };
