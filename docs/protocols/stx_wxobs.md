# STX-200 Weather Station: Serial Output Reference, section 4.2

Rev C. Applies to firmware 2.1 and later.

## 4. Serial output

The STX-200 transmits ASCII sentences at 9600 8N1 once per minute, roughly
NMEA-style. Each sentence starts with `$`, ends with `*hh` (two hex digits)
and a CR/LF. Fields are separated by commas. Empty fields mean the sensor did
not report; do not treat them as zero.

The first field is the address:
a 2-letter talker id (we ship `ST`, but resellers re-badge it, so don't
hardcode it) followed by the sentence name. Always match on the sentence name.

### 4.2 WXOBS: basic observation

| # | Field        | Format          | Notes |
|---|--------------|-----------------|-------|
| 0 | Address      | `$STWXOBS`      | talker + `WXOBS` |
| 1 | UTC time     | `hhmmss`        | e.g. `081530` = 08:15:30. **Time only, no date.** The host is expected to supply the date. |
| 2 | Latitude     | `ddmm.mmmm`     | degrees and decimal minutes |
| 3 | Lat hemisphere | `N` / `S`     | |
| 4 | Longitude    | `dddmm.mmmm`    | degrees and decimal minutes |
| 5 | Lon hemisphere | `E` / `W`     | |
| 6 | Air temperature | decimal      | degrees C |
| 7 | Relative humidity | decimal    | percent |
| 8 | Station pressure | decimal     | hPa |

Checksum: XOR of every character between `$` and `*`, exclusive, printed as
two uppercase hex digits.

Example:

    $STWXOBS,081530,4807.038,N,01131.000,E,21.4,63.0,1013.2*7D

Position is that of the station mast and is repeated in every sentence even
though the station is fixed.

Note: see also WXOBSF (separate document) which carries wind. It has a
similar name. Do not confuse them: a parser for WXOBS must reject WXOBSF.

## Integration brief

Device protocol name: `wx_ascii`. Message id: `WXOBS`.
Canonical measurements wanted: `temperature_c` (celsius), `humidity_pct`
(percent), `pressure_hpa` (hpa). Capture location from the position fields.
