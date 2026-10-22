# Architecture

Verona uses a standard Python `src` layout. Editing, job construction and
hardware access are separate so a preview never needs a device connection.

| Module | Responsibility |
| --- | --- |
| `commands.py` | Validate tools, force, speed, coordinates and registration bounds; build PLT bytes. |
| `transport.py` | Explicit TCP/USB operations, timeouts, partial writes and progress; no automatic job replay. |
| `device_lock.py` | Nonblocking per-user lock shared by GUI, CLI and MCP send operations. |
| `core.py`, `lds.py`, `document_import.py` | Bounded SVG, LDS and printable artwork import. |
| `lds_export.py`, `native_images.py` | Bounded native CDOC/ZIP output and lossless QOI image encoding. |
| `ui.py`, `node_editor.py`, `editor_widgets.py` | Desktop editing, tabs, undo, preview and explicit print/send actions. |
| `layers.py`, `layer_panel.py` | Shared visibility/output/locking rules and the object layer panel. |
| `printing.py`, `artwork.py` | Physical-size print/PDF output and registration marks. |
| `cli.py` | Status query, validated dry-run export and explicit one-shot sending. |
| `editor_bridge.py`, `editor_client.py`, `mcp_server.py` | Optional same-user editor automation with revision checks and prepared jobs. |

The encoder is shared across all interfaces. Document loading never sends
hardware commands. The CLI does not accept raw PLT input.

New `.jdesign` files use `format: "verona"`, version 1 and millimetres. The
previous `juliet-design-linux` format identifier remains readable.

LDS saves contain native `CDOC` geometry/images, a PNG `PREVIEW`, and a
`VERONA.json` editing document. The editing document uses the same JSON/geometry
validation as `.jdesign` and is bound to the CDOC bytes by a SHA-256 digest.
If another application changes CDOC, its stale Verona editing document is
ignored and the native importer is used. The extra member preserves original
pixels, names, node controls, groups, hidden/locked state and output settings.
Native output contains visible print/cut content; printable vector styles and
bleed are rendered at 300 DPI. Shared images use a single QOI definition.
Image chunks follow the [QOI 1.0 specification](https://qoiformat.org/qoi-specification.pdf).
Official Leonardo compatibility has not been verified.

Saving builds and validates the archive before writing. The UI writes to a
temporary sibling file and atomically replaces the destination. Its first
overwrite of an existing LDS also creates a byte-for-byte `.lds.bak` backup,
which later saves retain. Automation continues to save `.jdesign`/`.json` only.

Items may store boolean `visible`, `locked`, `print` and `cut` flags. Missing
flags keep the original behavior: visible, unlocked, printable artwork and
enabled cutting. A layer is one document object; groups retain their existing
selection behavior. Hidden items never reach print or cut output. Locking
protects editing in both the GUI and the local automation interface.

Settings use Qt's per-user `Verona/Workspace` store. Device locks and editor
sockets use XDG state/runtime directories. No local settings, private designs
or device address are needed in the source tree.
