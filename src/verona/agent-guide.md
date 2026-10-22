# Verona agent operating guide

The layer panel lists document objects from front to back. Optional boolean
`visible`, `locked`, `print` and `cut` fields are reported by editor state.
Hidden objects are excluded from PDF/print and cut jobs. Locked objects still
output but editing tools reject changes until the user unlocks them. Respect
these settings when preparing a job. Print-only images retain their source
pixels, and a sheet with registration disabled prints without marks.

Use the user's language. Explain the visible result of an edit, its scope and
any measured limitation. All design coordinates and distances are millimetres.
The editor has a 300 × 300 mm workspace; the paper and Print & Cut frame can be
smaller. Existing Qt editor functionality remains available to the user.

## Start with the actual document

1. Call verona_list_editors. Choose the instance matching the user's file; ask
   when multiple instances cannot be distinguished from the request.
2. Read verona_editor_state. It includes unsaved artwork, active tab, selection,
   object indices, cut flags, groups, bounds, print bounds, bleed, paper,
   registration, manual cut adjustment, settings and history counts.
3. Use verona_editor_image to inspect the whole artwork. This is a rendered
   document image, independent of desktop window occlusion or canvas scrolling.
   For exact geometry, request include_geometry=true on explicit object indices.
4. Object indices belong to the returned revision. Do not carry indices across
   edits without checking the new state. Never overwrite a revision conflict.

## Know which geometry does what

- A bitmap, its image mask and its cutting contours are separate data.
- cut=false means the object is excluded from the cutting job. Background images
  can print without cutting. Registration guides must stay excluded.
- Native LDS Logo subtype 101 is the image mask; subtype 102 is its cut curve.
  A saved cut curve is valid regardless of source color. Source colors are
  retained, not a reliable inference of the desired printed bleed color.
- Native import retains ordinary nested group_path; page-mark containers stay
  separate from editing groups. Mirrored Logo-group records are represented by
  their existing child geometry with its original cut flag, not extra duplicate
  cuts. Independent unrepresented Logo-group curves, multiple/hidden layers,
  unknown objects and nonidentity group transforms reject pending matching
  samples.
- Use each object's group_path and live bounds for grouping. native_groups
  metadata retains import evidence and does not track subsequent editing.
- Print & Cut requires a usable registration frame. Cuts must fit both the
  original frame and the frame after manual cut adjustment. Positive adjustment
  X moves cuts right; positive Y moves cuts down. The effective frame returned
  by state accounts for both requirements.
- A changed paper or registration mark layout requires printing a matching new
  sheet. The PDF must be printed at 100% actual size without fit-to-page.
- Printed artwork or bleed near registration marks can interfere with sensing.
  Inspect preview and leave the marks clear. A cuttable contour does not prove
  that its printed artwork leaves adequate mark clearance.

## Preview, inspect, apply

Editing tools return detached plans. They do not change the live document.
Read the returned object bounds and errors, then view the plan with
verona_editor_image(editor_id, plan_id). Apply that exact plan using
verona_apply_edit with its revision. One application creates one undo step.
The last four plans are retained. Applied, expired or stale plans cannot be
replayed. If the user edits, changes selection or changes tabs, read the new
state and review a new plan. A drag in progress or machine transmission blocks
agent modifications.

The user's request can authorize reversible edits. Do not ask for repeated
approval when the requested operation and necessary parameters are already
clear. Ask for a missing material choice rather than inventing it.

### Fill the cutting area

verona_fill_area repeats the explicit selected pattern in a rectangular grid.
It replaces those selected originals, preserves other objects and avoids their
cutting footprints. Groups expand together. By default the pattern keeps its
orientation, size, masks and image transforms; copied groups receive fresh IDs.

Specify gap_mm, margin_mm and max_copies. Bounds include print bleed so copies
do not overlap merely because their cut lines fit. Read copies, grid_candidates,
limit_reached and layout_method. Optional allow_rotate=true compares whole-pattern
0/90-degree layouts and chooses the greater actual copy count, retaining 0 on
ties; rotation_deg reports that additional rotation. Physical sizes, masks and
group relationships remain. Enable rotation only when the user's intended
orientation permits it. This is rectangular packing, not mixed-orientation or
optimal nesting of arbitrary contours. It can leave usable gaps. Limits are
1000 copies, 2000 output objects and 2 million cutting points.

If the user says "fill this sheet", use the current selection when unambiguous.
Otherwise identify the intended pattern from the image and object list, or ask.
Do not silently shrink the pattern to increase quantity. Size changes can be
previewed separately with verona_preview_edit.

### Pack different units and contours

verona_pack_objects treats each outer group or standalone selected object as a
unit, ordered by ascending source index. copies_per_unit gives one positive
count per unit (defaults to one each). fill=false places all requested copies
or rejects the plan; a bounded search failure does not prove physical impossibility.
fill=true first places those minimum counts, then adds copies while space and
limits permit; inspect the reported per-unit counts, which need not keep a ratio.
Unselected objects are retained; active cutting footprints are obstacles and
non-cut backgrounds are not. Physical sizes, masks, image affine and within-group
positions are retained; copies get fresh group IDs.

allow_rotate permits quarter turns. shape_aware uses actual cut/print/bleed
footprints, conservatively fills enclosed holes and gives open lines a thin
collision footprint; it can interlock concave shapes but does not nest in holes.
Default rectangular footprints are faster. Printed artwork also avoids the
actual L marks with gap_mm clearance. This prevents geometric overlap, not a
guarantee of camera acquisition. margin_mm and cut adjustment are included.

The corner/vertex bottom-left heuristic checks at most 20000 candidates with a
12-second search budget and capped coordinate sets. It is not optimal nesting.
Read limit_reached, candidate_checks, units and placements (including rotations).
Preprocessing and rendering are additional time. Capacity is 1000 unit copies,
2000 objects and 2 million cut points. For many identical regular copies the
grid fill tool is preferable. Never silently shrink artwork to fit requested counts.

GUI Edit → Automatic layout offers copies per unit, gap/margin, rotation,
contour footprints and filling. Preview enables Apply; changing settings requires
another preview. Applying reuses the exact cached layout and creates one undo.
Cancel preserves work. GUI sets one count for all units; MCP supports individual
counts. Existing modal-dialog protection prevents concurrent agent mutations.

### Order whole cutting jobs

verona_optimize_paths previews the saved whole-job enabled flag. Apply is one
undo, preserves selection and never reorders artwork/print stacking. The policy
is recalculated after later edits. Closed containing paths follow their inner
cuts; stable and nearest eligible candidates are compared by estimated pen-up
distance. When already valid, document order is retained if no longer. Inside
first can increase distance; inspect travel_increased_for_inside_first and the
before/after mm. Individual contour points, starts, directions and closure stay.

Limits: 4000 contours, 100000 containment relations and a 10-second containment
search. Disable restores document contour order without this search. Estimates
include logical start/return, exclude camera acquisition and physical timing.
No blade compensation, start rotation or reversal is performed. Touching or
intersecting paths are not treated as contained holes.

verona_editor_image(view="toolpath", plan_id=...) renders actual encoded U/D
moves including settings, order and registration return, in machine cross/feed
axes. It requires valid cut geometry. The default artwork view still works for
out-of-bounds designs. Neither view queries or moves equipment.

### Edit anchors and cubic handles

verona_edit_path replaces one explicit contour on one object. nodes contain
point:[x,y] and optional in/out:[x,y] or null, all absolute document mm. Adjacent
out/in controls form a cubic; absent controls use their anchor; no controls
means a line. closed=true implies the last-to-first segment, without a duplicate
closing anchor. At least2open/3closed nodes, at most10000; controls within±10000mm.
Read saved node_paths_mm with include_geometry. If an entry is absent, existing
cut_geometry_mm is a polyline (drop the repeated closing point). Unknown original
handles are not recovered. Exact no-op retains the original object.

Controls persist with save, transform, duplication and explicit cut transfer.
Adaptive subdivision bounds each cubic chord by0.025mm; it is not a measured
cutter tolerance. At most200000points per cubic path and2million per agent edit.
Image bytes/mask/affine remain; contour-based printed bleed follows edited cuts,
so review/reprint when needed. Mask retracing replaces edited cut controls.
GUI Edit → Edit nodes and curves offers direct anchor/handle drag, wheel zoom,
fit, exact mm fields, midpoint insertion, deletion and closure. Inserting a
cubic midpoint splits its controls without changing its shape. Arrow keys move
anchors0.1mm (Shift1mm). Cancel preserves work; Apply creates one undo.

### Add printed bleed

verona_add_bleed adds an external solid-color printed layer, 0–5 mm wide, to
explicit image objects. It retains the original bitmap, image mask and cutting
contours. The added ring extends outside the cutting contour and leaves its
interior, including transparent artwork, untouched. Excluding an object from
cutting does not change its printed bleed. Background images use their saved
fallback outline. The width stays in physical millimetres when artwork is resized.
Optional inner_mm (0–1) adds a selected-color band inside the cut contour, drawn
after the bitmap. It visibly covers this narrow edge region while leaving source
pixels, masks and cuts unchanged; cut-contour holes stay clear. Inner and outer
widths are independent. Width 0 with inner_mm 0 removes the added layer; it does
not reconstruct an image with baked-in bleed from another program.

GUI Edit → Set printed bleed offers outer/inner widths, color and an explicit
preview before application. Inner color is our defined edge-band operation;
exact parity with the original app's Small Inner Bleed is not established.
Bleed changes only the explicit image indices, even inside groups; the GUI uses
the images in the current expanded selection. Vector guides are unchanged.

Choose #RRGGBB explicitly, or "source" for each object's saved source color.
Inspect the preview: native source colors can differ from visible edge colors.
If source color is unavailable, specify a color. This is solid-color bleed,
unless mode="edge" is chosen. Edge mode extends nearest visible source colors
into the outer ring, retaining original bitmap/mask/cuts. It rasterizes only the
extension at up to 300 dpi with a maximum 1200-pixel edge; the original image
still prints at its saved resolution. This is color extrapolation, not texture
synthesis. Highly detailed edges need visual inspection. Fully transparent or
nonintersecting source/cut masks cannot provide colors and are rejected.
Color controls solid outer bleed and any inner band; for edge mode with inner_mm
0, use any valid color (e.g. #ffffff) without asking the user for an unused choice.
GUI Set printed bleed offers both modes and disables unused color selection.
"All" means the applicable image objects, not vector guides. Keep marks clear.

### Other editing tools

verona_preview_edit supports:

| Action | Parameters |
|---|---|
| transform | dx_mm, dy_mm, uniform scale, rotation_deg, mirror_x, mirror_y |
| duplicate | dx_mm, dy_mm (defaults 10, 10) |
| delete | {} |
| set_cut | enabled boolean |
| group | {} |
| ungroup | {} |
| align | mode: left/hcenter/right/top/vcenter/bottom/distribute_h/distribute_v |

Alignment treats each outer group as one unit and preserves its internal
positions; distribution requires at least three independent units.

### Create artwork and configure the document

verona_add_shape previews a rectangle or ellipse using top-left X/Y and width /
height in mm. verona_add_text previews text outlines using a font em size in mm,
not an exact visible glyph height. Inspect the returned resolved_font and image
for fallback fonts or missing characters. Created objects are cut outlines;
they do not become printed artwork merely by being created.

verona_configure_document previews saved cut settings (custom, force, speed,
tool, passes) and registration settings (enabled, paper_width, paper_height, margins,
mark_stroke, mark_short_length, offset). Margins are [left,top,right,bottom] in
mm and include half the mark stroke beyond the recorded printer hard margins.
The current workspace supports paper dimensions up to 300 mm per axis.
Artwork keeps its position and size. Review bounds errors against the proposed
frame; out-of-bounds jobs remain blocked from sending. requires_reprint means
paper or printed marks changed: export and print a matching sheet before cutting.
Changing only cut adjustment does not require moving or reprinting artwork.
These plans also use exact-revision apply and one-step undo; configuring does
not contact a printer or cutter. Existing selection is preserved.

verona_list_printers reads installed queues and driver-reported hard margins for
the requested paper size; it does not submit output or change the document.
Select an explicit returned name using registration.printer_name. If margins
are omitted, configuration fits marks to that queue's reported hard margins;
explicit margins must meet those minimums. A rejected paper size or unavailable
queue is an error. Selecting a queue does not make PDF export send a print job.

### Import artwork

verona_import_artwork previews appending a plain SVG or supported raster file,
up to 64 MiB. Existing work, file association and metadata are retained. SVG
keeps its physical units. x_mm/y_mm position the cut bounding-box top-left;
omitted axes center in the effective cutting frame. There is no automatic fit.

Raster tracing uses the same algorithm as GUI import. tracing can specify mode
(print_cut/cut_only/background), mask_mode (alpha/background), width_mm and
height_mm (one axis retains aspect ratio), background (#RRGGBB), tolerance
(integer 0–255), inverted/keep_holes/clip (booleans), smoothing (integer 0–100),
minimum_area_mm2 (0–1000), offset_mm (-5–5), bleed_mm (0–5), bleed_color (#RRGGBB).
Inspect the image: background removal can select the wrong regions. Original
files stay unchanged. Print & Cut creates A4 marks when missing and enables
registration; requires_reprint reports a changed registration layout. Import
dialog bleed is baked into the embedded image, unlike the separate reversible
verona_add_bleed layer. Neither changes the source file. An empty tracing result
is rejected before changing live work. Images are limited to 16 million pixels.

Both import and saved-mask editing support mask_mode="colors" with colors as
1–16 #RRGGBB foreground colors. Their RGB tolerance uses the largest per-channel
difference; the union of matching visible pixels is retained. Transparent pixels
are excluded before optional mask inversion. Background mode instead excludes
the selected background color. GUI Selected colors provides add/clear controls,
and the palette is saved with tracing parameters. An empty foreground palette
or empty tracing result cannot be applied.

verona_editor_select changes selection, not artwork. An empty list clears it.
verona_editor_history undoes the latest editor operation, including a manual
user edit, or redoes it when redo=true. Always review the current revision.

### Attach separate cut paths to an image

verona_link_cut_paths takes an explicit image_index and separate vector cut_index.
It replaces the image's old cut contours, enables its cut flag and transfers
the selected contour_indices from that vector. Omit contour_indices to transfer
all contours. A partly used source retains its other contours; an empty source
is removed. Returned image_index_after accounts for changed object indices.
The image pixels, image_clip and image_transform are preserved. Its new cuts
then move, scale, mirror, rotate, duplicate and save with the same image object.
Any contour-based bleed follows the newly attached cuts; inspect that preview.

No automatic pairing or repositioning is performed. Read exact source geometry
and inspect the plan, especially when the source aggregates paths for many
images. Only the explicit pair is changed, even inside existing groups. The GUI
Edit menu offers attachment for one selected image plus one selected vector;
that action transfers the vector's entire contour list. Use MCP contour_indices
for a subset. Apply and undo retain the usual revision and single-step rules.

### Reedit a saved image mask

verona_retrace_image previews one explicit image, including a member of a group.
It keeps original bitmap bytes, image affine, cut flag, group and printed bleed.
parameters can set mask_mode saved/alpha/background, background #RRGGBB,
tolerance integer 0–255, inverted/keep_holes/clip booleans, smoothing integer
0–100, minimum_area_mm2 0–1000 and offset_mm -5–5. Offsets and area filtering use
document mm even when an image is rotated, mirrored or sheared. An empty result
is rejected. Retracing replaces old cut contours, including manually attached
cuts, so inspect exact geometry and the preview before applying.
Images imported with tracing bleed_mm > 0 carry the bleed ring in their pixels
(raster_bleed_mm). They store the pre-bleed mask and accept only saved mode, so
retracing never moves the cut out onto the bleed.
The saved import pixel scale keeps that printed ring aligned through affine
transforms. Older records without this scale retain their existing print clip
when the saved mask is edited; only their cuts are retraced.

Saved mode reuses the stored working mask, or reconstructs the existing image
clip. When neither exists it uses the image rectangle; it does not guess a mask
from independent cut curves. The working mask is at most 1200 pixels per axis;
repeated retracing can approximate old subpixel vector masks at that resolution.
Parameters and the working mask survive save/reopen, but unavailable original
operation history is not reconstructed. Baked image bleed stays in the pixels.

GUI Edit → Edit image mask opens the same tracing controls for one selected
image. Position, affine, dimensions, mode and baked bleed are retained; ordinary
transform tools handle resizing. Opening and accepting without changes preserves
the exact original item and does not add undo history. Changing tracing creates
one undo step. Cancel preserves the document. New raster imports also save their
tracing parameters; old documents remain editable from their saved masks.

### Paint include/remove mask strokes

GUI saved-mask editing offers Paint mask, Include/Remove and brush diameter in
mm. Drag on the preview to alter the working mask; source bitmap pixels stay
unchanged. Including an originally transparent pixel does not invent artwork.
Switching to alpha/background/selected-colors mode regenerates the mask and
discards the dialog's manual brush changes. Cancel discards the whole edit;
applying produces one editor undo step. Stored masks retain the painted result,
not the individual stroke history.

verona_retrace_image also accepts brush_strokes: 1–100 objects each containing
mode (include/remove), width_mm (0.1–20) and points_mm ([[x,y],...], maximum 1000
points total). Coordinates and diameter are document mm; inverse image affine
maps them to the bounded working mask, including rotation/mirror/scale/shear.
Strokes apply after generating the requested mask; the result uses saved mode
so reopening/retracing does not discard the paint. Inspect the preview, especially
with contour offsets that can close small cut holes while the print mask retains
them. Working-mask resolution bounds brush precision; it is not vector node editing.

Agent modifications are blocked while a modal GUI dialog is open, as well as
during manual canvas gestures and sending. Read-only inspection remains available.

### Import PDF/SVG artwork and print vectors

verona_import_artwork defaults SVG to vector cut paths. For wider SVG appearance,
explicit representation=raster renders supported text/simpleCSS/gradients/use/
embeddedPNG-JPEG-WebP/masks. SVG CSS selectors are simple type/class/id lists;
clipPath, filters, animation, external references and unknown styles reject.
PDF renders a single 1-based page (default1) including static annotations. It
creates raster artwork, not extracted/editable PDF paths. dpi75–600, default300;
source64MiB,16million pixels, physical page up to300×300mm. Size is preserved
unless tracing.width_mm/height_mm explicitly changes it. Review system-font
substitution in SVG. Existing printed marks in a source page stay in its pixels;
do not add duplicate marks or assume those marks become registration geometry.
Use background mode plus explicitly attached cuts when appropriate.

GUI File → Import PDF/SVG artwork uses the same renderer/tracing, selects a PDF
page and reuses the exact preview image when applying. It preserves existing
work/settings/marks, with oneundo. Cancel leaves work unchanged.

verona_set_vector_print takes explicit vector indices (no group expansion),
fill/stroke #RRGGBB or null and width_mm0.01–5. Both null removes printing.
Cut flag and cut points are independent. Odd-even fill, round caps/joins;
stroke width stays physicalmm under transformations. Canvas/preview/PDF and
packing printed footprints include ink. Reprint after changes. Cut transfer
requires a cut-only source; duplicate/remove style first for printed vectors.
GUI Edit → Set vector printing offers the same style. No printer submission.

### Open, save and export

verona_editor_open opens a .jdesign/.json/.lds in a new tab, preserving existing
unsaved work. JSON design files use the same 256 MiB read limit in GUI/open/prepare-job;
agent-opened LDS inputs remain limited to 64 MiB. Invalid input does
not replace the current document.
LDS parsing rejects malformed records and limits all object-stack depth to 256,
records to 100,000, structural objects to 10,000, vector/image objects to 2,000,
and total expanded points to 2,000,000 including clips, guides and closure.
Errors name the exceeded limit; simplify or split the source and retry. No
precision reduction is applied. Decoded images use a 512 MiB / 32-entry cache,
canvas edge layers a 128 MiB budget; eviction preserves source artwork and
rebuilds on demand. These cache budgets are not a total process RSS limit.
JSON is checked before parsing for 256-level nesting and excessive structure;
all saved-design objects and geometry are checked before copying/decoding
(maximum 2,000 items and 2,000,000 points including clips and noncut items).
Plain vector SVG inputs are limited to 64 MiB, 256 levels, 100,000 XML elements
and 2,000,000 expanded points across paths; source controls are also bounded.
Curve sampling precision is unchanged; oversize input rejects explicitly.

verona_editor_save saves the live document to an explicit .jdesign/.json path.
It does not write LDS. verona_editor_export_pdf exports current artwork, bleed
and registration marks without submitting a printer job. Both reject existing
output paths unless overwrite=true is explicitly requested. Obtain authorization
for the intended output path; file writes cannot be undone through editor undo.

## Machine operations are separate

The existing verona_prepare_job reads a saved jdesign and snapshots its PLT.
Check the exact tool, force, speed, passes, contours, bounds, registration, SHA-256 and
source before using verona_send_job. Material-specific force/speed are machine
command values, not grams or mm/s. Do not invent settings for an unknown material.
passes is an integer from 1 through 10, defaults to the saved value (or 1),
and can be overridden during preparation. Each contour repeats before the next;
registration runs once. Returned contour/point counts describe source geometry.

Only send with current user authorization and the intended paper, mat and tool
loaded. machine_ready is an operator assertion, not a sensor or interlock.
Every job is consumed before transport. No automatic replay, reconnect or
replacement preparation after a failure. Socket delivery does not establish
physical completion. The GUI, CLI and MCP share a user-level device
lock; other vendor programs do not participate.

No camera frame, machine abort, completion sensor or firmware-update MCP tool
is available. Firmware CLI compatibility remains unverified on the real machine.
Do not use guessed commands or test a firmware header to discover compatibility.

## Current integration scope

The running editor exposes local same-user Unix sockets and works without the
optional MCP SDK. A trusted local MCP host uses the optional stdio server.
Startup does not query or move the cutter. Restart an older running editor to
load this bridge; preserve its unsaved work first.

The tools currently cover live inspection, selection, transforms, copying,
groups, cut flags, deletion, grid fill, mixed/contour packing, printed bleed and undo/redo.
Opening designs, saving jdesign, PDF export, alignment/distribution, new
shapes/text, SVG/raster import, printer profiles and paper/mark/cut settings are
also available, with saved-mask reediting, color palettes, brushes and inner/edge bleed.
Optional path ordering and node/cubic editing are available; physical PDF/SVG artwork import and explicit vector printing are also available.
Report unsupported operations honestly;
do not approximate them through arbitrary Python, raw machine commands or hidden
UI clicks.
