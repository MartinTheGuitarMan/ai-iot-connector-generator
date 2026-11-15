# AeroLog M4 Weather Logger: Modbus Register Map

Document AL-M4-REG-07. Modbus RTU/TCP slave, function 03 (read holding
registers). All registers are 16-bit. Addressing below is zero-based
(protocol address, not the 4xxxx convention). Unit id is configurable;
default 1.

Values are fixed-point integers. Divide/multiply as noted to get engineering
units. The device does not report a timestamp: the poller's clock is used.

| Addr | Name           | Type    | Resolution | Description |
|------|----------------|---------|------------|-------------|
| 0    | RAIN_ACC       | unsigned | 0.1 mm per count | Rainfall accumulated since the logger was last cleared. Rolls over at 65535. |
| 1    | WIND_GUST      | unsigned | 0.1 km/h per count | Peak 3-second wind gust over the last minute. |
| 2    | BATT_VOLT      | unsigned | 0.01 V per count | Logger battery voltage. 372 means 3.72 V. |
| 3    | UV_IDX         | unsigned | 0.1 per count | UV index, dimensionless. |
| 4-9  | reserved       |          |            | Reads as 0. Do not write. |

Registers 0-3 are always read together in one request (quantity 4). If any
of them is missing from a response, discard the whole poll; do not report a
partial reading.

Example poll response: `[1, 170, 373, 30]` is 0.1 mm rain, 17.0 km/h gust,
3.73 V battery, UV index 3.0.

## Integration brief

Device protocol name: `modbus_weather`. Message id: `station_telemetry`.
Canonical measurements wanted: `rain_mm` (mm), `wind_gust_kmh` (kmh),
`battery_v` (volts), `uv_index` (none). Use the same names for the raw
field entries.
