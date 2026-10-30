# Experimental firmware update

Verona contains an experimental firmware transfer path for Juliet cutters. It has not been validated on a real device and does not establish that a firmware image is compatible with a particular cutter.

Open the dialog from **Help → Firmware update (experimental)** or **Send connection → Firmware update (experimental)**. Select a local BIN to check its container format and display candidate version strings. Choose Wi-Fi or an explicitly selected USB node, read the cutter identity, and independently confirm that the exact image is compatible before starting a single update attempt. USB node listing uses local system metadata only. The file check, opening the dialog, and changing connection settings do not contact the cutter. Identity reads and firmware transfer run in the background.

The transfer sends no image body unless the cutter replies `PGREADY`. It does not reconnect or replay a transfer after an error. A `PGOK` reply only indicates that the transfer protocol marker was received; it does not prove installation, compatibility, or normal operation. Check the cutter panel and touchscreen, verify camera operation where applicable, and explicitly read the firmware version after transfer.

A firmware recovery procedure has not been verified. Verona does not bundle, download, or automatically flash firmware. The MCP API does not expose firmware update operations.

USB uses the existing Linux `usblp` driver and requires read/write permission on
the selected `/dev/usb/lpN` node. Verona checks its sysfs vendor/product identity
and printer interface, and refuses pending input before sending a request. It
does not detach a driver, reset the USB device, or change permissions. Files
larger than 2 MiB are outside this experimental utility's supported input limit.

GUI update attempts save a local record before starting the device operation,
then record the result, failure stage and known byte counts. If the initial
record cannot be saved, the update does not start. Use **View last update record**
to read and copy the saved result after closing and reopening the dialog; this
does not contact the cutter. Records live under
`$XDG_STATE_HOME/verona/firmware-history/` (normally
`~/.local/state/verona/firmware-history/`). They omit network addresses, full
file/device paths and device serial numbers. A record still marked `started`
has no confirmed outcome; its unknown byte counts must not be treated as zero.

History contains only attempts recorded by this utility. A missing record does
not establish the result of an earlier attempt.

## Command line

Inspect a local file offline:

```sh
verona-firmware inspect ./firmware.bin
```

`status`, `verify`, and `update` require exactly one endpoint: `--host` or `--usb-node`.

```sh
verona-firmware status --host 192.0.2.1
verona-firmware status --usb-node /dev/usb/lp0
verona-firmware verify --host 192.0.2.1 --target-version V26.0605
```

An update is an explicit, single transfer attempt and requires the operator to supply the reviewed image hash and the exact identity observed for the target cutter:

```sh
verona-firmware update ./firmware.bin --host 192.0.2.1 \
  --sha256 "<reviewed-sha256>" --expected-mode "<MODE>" \
  --expected-hver "<HVER>" --expected-sver "<CURRENT-SVER>" \
  --target-version "<TARGET-VERSION>" \
  --confirm-compatible --machine-ready
```

The CLI reads the version once after a protocol completion reply; if it cannot verify the target version, it reports that result without repeating the update. Neither a successful transfer nor version readback verifies physical operation or camera behavior.
