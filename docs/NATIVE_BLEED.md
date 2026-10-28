# Native LDS image bleed

The image record (shape type 1012) has an optional kind-110 payload. Earlier
Verona readers skipped it, so a subsequent save lost editable native bleed.
The first-overwrite `.lds.bak` preserves the original stream.

The 11-byte payload stores two little-endian floats (contour offset and bleed
radius in source-image coordinates), then three one-byte flags: enable bleed,
clip artwork, and inner bleed. The bleed color comes from kind 100's color field.

Evidence from the locally available x86-64 Leonardo binary:

- `TTestLogoShape.LoadFromStream` at `0x100ad1538` loads the 11 bytes into `+0xb0`.
- `TTestLogoShape.DrawTo` at `0x100acf36d` checks `+0xb8`, reads the radius from
  `+0xb4`, and draws a round stroke with twice the radius and the matrix scale.
- `0x100acf5d5` checks the inner flag at `+0xba`; the inner stroke is 3 source
  units wide. Its inward extent is therefore 1.5 units times the image scale.
- `TMatrixObj.GetScale` at `0x1008e4e10` uses `_SetFlags` (`0x1008e6000`), which
  measures the transformed diagonal unit vector `(1/√2, 1/√2)`. For an affine
  image transform `(a,b,c,d)`, the scale is `hypot(a+c,b+d)/√2`.

The importer converts these lengths into Verona's document millimetres. It
retains the original bitmap, image placement, mask, and cutting curves.
Unknown/invalid flag values and malformed records are rejected.

Solid bleed rendering clips a round stroke to the inside or outside of the
contour. This avoids expensive offset booleans for detailed native outlines.
Geometric bleed paths remain available for operations requiring actual areas.
PDF and canvas output share the same drawing implementation.

The binary evidence establishes field use and rendering calculations. It does
not establish pixel-identical output with every Leonardo version or printer.
