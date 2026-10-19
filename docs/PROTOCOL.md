# Juliet Wi-Fi / PLT reference

This reference describes the subset implemented by Verona. Other firmware or
machines may behave differently. Romeo compatibility has not been tested.

## Connection

- Transport: a TCP stream to the single IPv4 address displayed by the cutter.
- Default port: **8080**; configurable through `--port` or the GUI.
- No account, internet service, broadcast discovery or login handshake.
- Read-only version request: ASCII `;RSVER;`. Expected reply: `SVER=<version>;`.
- A version reply is not an idle/ready signal and does not confirm mat loading.

Jobs are raw ASCII PLT bytes. Each write is at most 1024 bytes, followed by a
0.3-second pacing delay. Short writes continue from the accepted offset on
the same connection. Connect timeout is 2 seconds, write timeout is up to
60 seconds, and the total budget is at least 300 seconds, extended for large
paced jobs. Interrupted jobs are never reconnected or automatically replayed.

There is no verified job-completion acknowledgement or emergency-stop command
in this implementation. Byte counts report local socket acceptance only.

## Coordinates and settings

Editor coordinates are millimetres: X across the mat and Y along the feed.
The implemented work area is **300 × 300 mm**. The protocol uses **40 units/mm**
and sends coordinates in **feed,cross** order: `(Y × 40, X × 40)`.
Coordinates are quantized to float32 before integer rounding; one protocol
unit is 0.025 mm. Exactly forward-collinear intermediate points may be omitted.

| Command | Verona's use |
| --- | --- |
| `IN;` | Job initialization. |
| `FSIZE<feed>,<cross>;` | Maximum ordinary-job extents in protocol units. |
| `SP1;` / `SP2;` | Blade / pen tool selection. |
| `!FS<n>;` | Force setting, integer 1–160. Omitted to retain device settings. |
| `VS<n>;` | Speed setting, integer 1–13. Omitted to retain device settings. |
| `U<feed>,<cross> ` | Raised move; note the trailing ASCII space. |
| `D<feed>,<cross> ` | Tool-down move; note the trailing ASCII space. |
| `U ` | Raise tool without a coordinate pair. |
| `!PG;` | Job finalization as used by this implementation. |

Force and speed numbers are device setting values; physical units are not
established here. Values outside these ranges are rejected, never clamped.
Omitting a setting can leave a previously high device setting active.

Example: a 20 × 10 mm rectangle beginning at `(5,5)` mm, using a pen:

```text
IN;FSIZE600,1000;U SP2;!FS1;VS1;U200,200 D200,1000 D600,1000 D600,200 D200,200 U0,0 U !PG;
```

Ordinary jobs finish with a raised move to protocol `(0,0)`. The physical
origin depends on the machine's loading/setup state; validate it with a pen.

## Print & Cut

The application prints four filled L marks. The bottom-left arms are 12.5 mm;
the other arm lengths and stroke width are configurable. Default short arms
are 4 mm and stroke width is 1 mm. Changing marks requires a matching reprint.

A registered job begins:

```text
IN;PA;BD:103,0;IN;TB26,<height>,<width>;CT1;IN;PU;U SP1;
```

`height` and `width` describe the registration frame in protocol units.
For page bounds `(left, top, right, bottom)` and measured correction `(dx,dy)`:

```text
cross_mm = X - left + dx
feed_mm  = bottom - Y - dy
```

Corrections must be finite and within ±5 mm. Both original and corrected
paths must stay inside the registration frame, whose dimensions must not
exceed 300 mm. Printed marks/artwork are not shifted by the correction.

The footer is `U<maximum_encoded_feed>,0 U !PG;!PG;`. Its combined behavior
has been tested with mat retention and left-side parking; individual command
semantics are not fully isolated. Do not invent alternate registration or
motion commands based on general HP-GL documentation.
