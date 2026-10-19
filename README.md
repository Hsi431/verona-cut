# Verona

**A native Linux home for Juliet.**

Verona is an independent, offline design and cutting application for **Siser Juliet**.
Import artwork, edit paths, arrange a sheet, print at actual size and send a cut
directly over local Wi-Fi—all from one Linux desktop application.

## Why Verona?

**Your cutter is on your desk. Your designs should stay there, too.**
No accounts, no subscriptions, no cloud uploads. Once installed, Verona works
without internet access; Wi-Fi cutting needs only a local connection to your
machine. Your files, your tools, your workflow.

**Edit and cut in one place.** This is a desktop editor with everyday drawing,
path editing, layout and Print & Cut tools, alongside direct cutter control.

**Keep the workflow focused.** Open artwork → edit and arrange → cut.
No storefront, online asset library or background cloud synchronization.

## Features

- SVG vectors, raster artwork and PDF page import; supported `.lds` documents open read-only.
- Shapes, text outlines, node and curve editing, masks, contour tracing and printed bleed.
- Move, resize, rotate, mirror, group, align, distribute and automatically arrange artwork.
- Multiple document tabs, undo/redo, light/dark themes, English and Traditional Chinese.
- Layers with selection, renaming, stacking order, visibility, locking and separate print/cut switches.
- Explicit **Print only / Cut only / Print and cut** image import; change output later without losing the image.
- Print & Cut registration marks, measured alignment correction, direct printing and PDF export.
- Local `.jdesign` files, PLT export, `.lds` thumbnails and desktop file associations.
- Wi-Fi version checks and bounded cut jobs; CLI dry runs without any device connection.
- Optional local MCP interface for reviewing and applying undoable editor changes.

**Status: alpha.** Juliet Wi-Fi cutting and Print & Cut have been used on real
hardware. USB-B delivery is experimental. Romeo has not been tested and is not
currently a supported target. The work area is limited to **300 × 300 mm**;
automatic drag-knife offset compensation is not implemented.

Hidden layers are excluded from printing and cutting. Locked layers still
output normally but cannot be moved or edited until unlocked. Layer changes
support undo/redo and are saved in the design. Print-only sheets can be
printed or exported as PDF without registration marks.

## Install and open

Requires Linux and Python 3.11 or newer. From the repository directory:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install .
verona
```

On Ubuntu/Mint X11, install the system dependencies if missing:

```bash
sudo apt install python3-venv libxcb-cursor0 libegl1 libopengl0
```

Dependencies are downloaded during installation; normal editing and cutting
do not use an online service.

Choose **Language** and **Appearance** from the menu. Enter the IPv4 address
shown on the cutter in the Wi-Fi panel and use **Check connection** before
sending a job. The address is remembered locally. You can also launch with:

```bash
verona --lang en --host "$VERONA_HOST" --open examples/pen-calibration.jdesign
```

`VERONA_HOST` optionally sets the cutter address; `--host` overrides it.
`--port` selects a different TCP port (default: 8080). There is no LAN scanning.
Connection checks query the version; they do not confirm that the machine is
idle or that material is loaded.

For optional `.lds` file associations and thumbnails, run `verona-desktop`.
This changes only the current user's desktop configuration. Restart an
already-running Nemo after the first installation to load the thumbnailer.

## CLI

```bash
# Enter your cutter's address from its screen; keep it out of shared files.
read -r -p 'Cutter IPv4 address: ' VERONA_HOST
export VERONA_HOST

# Read-only version query. No head movement or cut job.
verona-cut status --host "$VERONA_HOST"

# Validate and export a small pen job. No connection to any hardware.
verona-cut cut examples/pen-calibration.jdesign --dry-run --output preview.plt

# After checking the setup and loading a pen, explicitly send the job once.
verona-cut cut examples/pen-calibration.jdesign --host "$VERONA_HOST" --send
```

`cut` defaults to a dry run. It accepts `.jdesign` or plain path-based `.svg`
files, never arbitrary PLT commands. `--force 1..160`, `--speed 1..13` and
`--tool pen|blade` override job settings; these are machine setting values,
not material recommendations. Omitting force/speed preserves saved settings
or the machine's settings. Read the dry-run summary before sending.

All job paths are checked against the work area and, when enabled, the
registration frame. Failed transfers are never automatically replayed.
“Delivered” means bytes were accepted by the local transport, **not** that
the physical cut completed. See [the protocol reference](docs/PROTOCOL.md).

## Safety

For first tests, **remove the blade holder** and run an air cut or use a pen.
Secure the mat/material, clear the travel area and keep your hand near the
power button. Software bounds checks cannot detect an incorrect physical
origin, an obstruction or the wrong tool. Set force and speed for your material.
Verona does not provide a verified remote emergency-stop command.

## Development

```bash
python -m pip install -e '.[mcp]'
QT_QPA_PLATFORM=offscreen python -m unittest discover -s tests -v
python scripts/check-release.py
```

Tests use synthetic fixtures, mocks and local sockets; they do not contact
a cutter or submit real print jobs. The optional MCP service runs with
`verona-mcp`; its tools are described in the packaged
[operating guide](src/verona/agent-guide.md).

[Architecture](docs/ARCHITECTURE.md) · [Release workflow](docs/RELEASING.md)

Verona's source code is licensed under the [MIT License](LICENSE).
Dependencies retain their [own licenses](THIRD_PARTY_NOTICES.md).

## Disclaimer

Verona is an independent, unofficial project for interoperability on Linux.
It is not affiliated with, endorsed by or supported by Siser. Siser, Juliet,
Romeo and Leonardo Design Studio are trademarks of their respective owners,
used here only to identify compatibility.

Use at your own risk. The authors and contributors accept no responsibility
for hardware damage, voided warranties, ruined materials or personal injury.
Always test without a blade first and keep your hand near the power button.
