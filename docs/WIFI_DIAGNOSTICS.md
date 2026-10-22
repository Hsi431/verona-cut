# Wi-Fi identity diagnostics

Use the address shown on the cutter's screen:

```bash
verona-cut status --host "$VERONA_HOST" --details
```

This explicitly sends only `;RINFO;`, with a two-second total budget and a
4096-byte reply limit. It uses Verona's shared device lock, does not move the
head or change settings, and does not retry a failed query. Normal `status`
continues to send only `;RSVER;`.

## Observed replies

Read-only testing on SS12 / V22.0613 confirmed these requests:

| Request | Reply field(s) | Interpretation |
| --- | --- | --- |
| `;RSVER;` | `SVER` | Reported software version. |
| `;RHVER;` | `HVER` | Reported hardware version. |
| `;RMODE;` | `MODE` | Model code, `SS12` on the tested device. |
| `;RPGHEAD;` | `PGHEAD` | Program-header label; not a compatibility check. |
| `;RPID;` | `PID` | Opaque identifier; not published. |
| `;RCBM;` | `CBM` | Short opaque text; not a bitmap response. |
| `;RINFO;` | `PGHEAD`, `MDATE`, `NUM`, `PID`, `SVER`, `HVER`, `BSVER`, `MODE`, `MID` | Identity fields followed by `OK=RINFO;`. |

Replies can arrive in multiple TCP chunks. `NUM` contained non-text bytes;
the reader handles omitted identifiers as opaque bytes. The CLI returns
only model, software/hardware versions, the uninterpreted `BSVER` value,
date code and program-header label. It lists omitted field names without
their values. Neither `BSVER` nor `MDATE` has a verified component/date
meaning; do not label them camera firmware or manufacture date.

## Camera and job-state limits

These replies do not establish idle state, loaded material, registration
success, cutting progress or completion. There is no verified frame-export,
camera-threshold or registration-failure diagnostic API in Verona. An opaque
`CBM` reply must not be interpreted as image data. A query timeout does not
prove that another firmware lacks all diagnostic support.

## Firmware updates

The [official update guide](https://cdn.leonardodesignstudio.com/pdfs/siser-cutter-update-firmware.pdf)
supports both Wi-Fi and USB and links a public build folder. This confirms a
Wi-Fi update path, not that a particular BIN fits a particular board or fixes
registration. The current device's `SS12`, `PGSR_SSA` and version strings alone
do not establish an update's compatibility. A matching build, documented
recovery procedure and retained calibration/settings are still required
before choosing an update. Verona does not expose a firmware-update command.
