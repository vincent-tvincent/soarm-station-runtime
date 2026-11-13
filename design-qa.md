# Dashboard design QA — 2026-11-13

final result: passed

## Visual truth and intentional changes

The approved 16:10 visual targets were the Operations and ROS graph images
`exec-8f2841e4-fc4e-4158-a741-03f34f943bd8.png` and
`exec-ff1bdf49-2efc-442f-a211-3e29fe20d5de.png` in the author's Codex generated
images directory for chat `01a1c4c2-6ee7-7ce3-96d6-1fdbe316dc6b`.
Both reference images are 1586 × 992. Their art direction is preserved in the
comparison boards under `docs/dashboard-images/`.

Later explicit user revisions supersede those images: Status/Manual/Payload
tabs, per-motor T/V and conditional current, no aggregate voltage hero, native
image bounds, optimized equal-area camera packing, draggable bounded PiP,
supplementary dock above the primary image, neutral generic graph components,
selection inversion, contextual red publishers, and single-click stationary
highlighting with double-click isolation and Exit focus.

The mock and source frames are deliberately different data: original historical
reference vs current historical preview files. The camera images are not live
robot evidence. The UI marks the preview as hardware-free.

## Captures and normalization

Final browser screenshots (CSS and image viewport 1829 × 1750 in the user's
current review browser):

- `docs/dashboard-images/operations.jpg`: primary image/top dock and Status.
- `docs/dashboard-images/equal.jpg`: equal-area packing with mixed native ratios.
- `docs/dashboard-images/floating.jpg`: floating camera layout.
- `docs/dashboard-images/graph.jpg`: complete overview, selection highlighting.
- `docs/dashboard-images/generic-graph.jpg`: unrelated mobile-robot topology
  with a sampled topic and activity sparkline.

Earlier layout review also used a 1440 × 900 desktop viewport. Final captures
use the user's taller review viewport; they are responsive reflows, not claims
of pixel identity with the original 16:10 image. Camera geometry tests cover
16:9, 16:10, portrait, extreme ratios and up to 64 cameras independently.

Full-view comparisons: `operations-comparison.jpg`, `graph-comparison.jpg`.
Each source/implementation is resized to equal 800-pixel width with its own
aspect preserved; density and different viewport height are explicit rather
than silently cropped. Focused component evidence:
`components-comparison.jpg` compares the original node/topic vocabulary with
the implemented metadata/activity components. Original images and actual
browser captures were opened together before this review.

## Findings resolved and comparison history

1. Equal columns left large black filler. Replaced by native-ratio row/column
   packing maximizing common image area. Eight geometry tests prove bounds,
   no overlap, equal areas and exact small-set optimality. Browser image ratios
   matched native ratios (4:3 exactly; 16:9 within subpixel rounding).
2. Supplementary views were detached at the bottom. Moved into a full-width
   dock directly above the primary image. Browser bounds confirmed a 10-pixel
   gap and matching dock/main widths.
3. Floating views were fixed. Added pointer drag, keyboard movement and
   clamping to the primary image. Browser drag moved the overview window from
   x=1059 to x=149, then clamped at x=52/y=650.7; it stayed supplementary.
   A subsequent click promoted it correctly.
4. Graph selection was reflowing/hiding objects. The default is now the full
   overview. DOM geometry comparison proved all 18 example objects unchanged
   after one click; double-click reduced the neighborhood to four objects and
   Exit focus restored the original 18 positions and viewport.
5. Active-button hover lost contrast; fixed the selected hover colors.
6. Browser text selection appeared during graph double-click. Disabled text
   selection on the graph SVG while retaining selectable inspector text.
7. Node title weights drifted from the diagram vocabulary. Restored regular
   node labels, bold topic labels, strong node top rules, gray typed topics,
   square corners, smooth separate paths and no junction dots. Final browser
   DOM/capture verified the regular weight and no console errors.

## Required fidelity surfaces

- Typography: Helvetica/Arial throughout; shared title hierarchy and short
  underline; node/topic weight distinction verified against diagram style.
- Spacing: responsive camera/panel split; internal panel scrolling, stable
  persistent controls, bounded floating windows, aligned top camera dock.
- Colors: white/black base; red Operations and blue Graph accents; selected
  graph object inverted; related publishers red; warm/cool directional edges.
- Images: actual historical assets in preview, production media via existing
  authenticated camera sockets. No cover/crop transforms or stretched ratios.
- Copy/content: meaningful node/topic types, explicit unknown values and mock
  labeling. Source receipt FPS is not represented as source capture rate.

## Interaction and technical verification

Browser-checked authentication, all side tabs, arm selection, manual stream,
DIRECT/TRAJECTORY/MODEL payload routes in INLINE and STREAM, update/end/hold,
primary promotion, equal packing, floating drag/clamping, graph selection,
double-click focus/exit, activity sampling, unrelated graph fixture and console
errors. All control interactions used the isolated mock server. No physical
motion was requested. An early sparse-stream failure was traced to the mock
fixture's missing semantic ACK/window rotation and corrected in that fixture;
production freshness checks were retained.

33 JavaScript regression checks passed: 19 transport, 6 graph, 8 camera geometry.
JavaScript syntax and whitespace checks passed. Backend and physical deployment
validation are recorded separately; this report does not equate mock success
with physical task success.

No unresolved P0/P1/P2 visual findings. Very large ROS graphs require pan/zoom
or optional focused view; no graph relationships are silently dropped.
