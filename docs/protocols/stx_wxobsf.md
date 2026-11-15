# STX-200 Weather Station: Serial Output Reference, section 4.3

Rev C. Same serial settings and framing as section 4.2 (9600 8N1, `$` ... `*hh`,
XOR checksum over the characters between `$` and `*`, two uppercase hex
digits, comma separated, 2-letter talker id then the sentence name; match on
the sentence name only).

### 4.3 WXOBSF: wind observation, full timestamp

Sent once per minute, right after WXOBS. Unlike WXOBS this sentence carries
its own date, so no host clock is needed.

| # | Field        | Format       | Notes |
|---|--------------|--------------|-------|
| 0 | Address      | `$STWXOBSF`  | |
| 1 | UTC time     | `hhmmss`     | |
| 2 | Status       | `A` or `V`   | A = data valid, V = sensor fault. Informational only. |
| 3 | Latitude     | `ddmm.mmmm`  | |
| 4 | Lat hemisphere | `N` / `S`  | |
| 5 | Longitude    | `dddmm.mmmm` | |
| 6 | Lon hemisphere | `E` / `W`  | |
| 7 | Wind speed   | decimal      | km/h, 1 minute average |
| 8 | Wind direction | integer    | degrees true, direction wind blows *from* |
| 9 | UTC date     | `ddmmyy`     | e.g. `150325` = 15 March 2025 |

Example:

    $STWXOBSF,081530,A,4807.038,N,01131.000,E,14.5,230,150325*64

Two-digit years: the firmware has no century. Treat as 20yy.

## Integration brief

Device protocol name: `wx_ascii` (same family as WXOBS). Message id: `WXOBSF`.
Canonical measurements wanted: `wind_speed_kmh` (kmh), `wind_dir_deg`
(degrees). Capture location from the position fields. Timestamp should come
from the sentence's own time and date.
