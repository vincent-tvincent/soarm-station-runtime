# Manual controls visual QA — November 13, 2026

Source visual truth: `/Users/vincentpu/.codex/generated_images/01a1c4c2-6ee7-7ce3-96d6-1fdbe316dc6b/exec-9b1cdd9b-c86a-4440-adb7-06e93276b747.png` (1536×1024, three states of one panel).

Browser implementation screenshots:
- `/Users/vincentpu/Library/CloudStorage/OneDrive-个人/so-arm/output/dashboard-ui/manual-joints-preview.jpg`
- `/Users/vincentpu/Library/CloudStorage/OneDrive-个人/so-arm/output/dashboard-ui/manual-ik-preview.jpg`
- `/Users/vincentpu/Library/CloudStorage/OneDrive-个人/so-arm/output/dashboard-ui/manual-ik-fault-preview.jpg`

Viewport: 1600×1000 CSS pixels; full-page captures 1600×1262, 1 CSS pixel per image pixel. Also checked 1280×720 and390×844: document width equaled viewport width, with vertical scrolling available. Temporary viewport override reset. Source's three conceptual panel columns were compared separately to the corresponding implemented state; no full-sheet scaling comparison.

State: hardware-free mock, explicit arm1, Manual selected. Joint, keyboard IK, and absent-host Leader states inspected. Fault scenario explicitly labels itself simulated. Existing camera photographs are historical preview media; they are not new hardware evidence.

## Findings and comparison history

- Initial desktop pose view shrank to an unreadable icon under the existing fixed-height panel. Reworked the Manual panel into upper control area, shared actions and persistent lower FK; short desktop layouts scroll vertically and keep a defined minimum workspace height. FK projection fits its measured/target geometry.
- First layout made important IK controls disappear too far into the upper scroller. The desktop jog buttons now use two aligned groups of compact rectangles; all axes and speed selection are immediately visible. Wrist presets/partial Home remain accessible through the upper area's scroll on short screens.
- Disabled key labels were too faint. Their contrast now remains readable while the Start button clearly owns activation.
- The approved mock accidentally repeated Fold. Only one shared Fold remains. The old Fold target exceeds the Pi configuration, so the actual unavailable state is shown rather than fabricated readiness.
- User's later corrections applied: rounded compact range sliders, filled black measured joint spheres, per-motor red fault/stall, no decorative panel indices, retained telemetry queue.

## Required fidelity surfaces

- Fonts/typography: existing station Helvetica/Arial stack retained; compact uppercase headings and consistent tab/action labels. Keyboard hints are smaller without adding independent decorative shapes. No font substitution.
- Spacing/layout: aligned dropdown/control/action edges; four shared action columns; upper input and lower pose stay distinct. Desktop uses two compact jog groups to fit the actual side-panel width. No horizontal clipping at checked widths.
- Colors/tokens: white background matches page; black measured links/joints; gray dashed target/leader; red only on the specifically reported issue. Topic/camera styles unchanged. Initial/healthy joints black. DOM verification of synthetic elbow stall: elbow #d9231b, the other five measured joints #191919.
- Image/geometry fidelity: real configured-URDF projection, not generated decorative artwork or a fixed robot picture. The user explicitly requested the old code-native 3D/FK view; measured geometry supplies the diagram. Camera framing remains native aspect ratio.
- Copy/content: exact J/K, L/H, F/D, I/U, G/S, brackets, 1–5/Shift, Tab, R/T/E, N/Space/M/Esc, Leader O. Readouts are measured values, not editable absolute pose targets. Leader selectors show actual registry availability instead of the mock's example USB device. Telemetry wording distinguishes an unconfigured collector.

## Interaction evidence

Hardware-free browser checks: Joint→IK→Leader mode selection; one-click Start; held X+ jog changed measured TCP249→256mm; speed5 changed to Maximum; N requested the configured Home and the simulated FK followed; Stop revoked simulated control; Leader gripper-follow toggle changed state; no-host Follow correctly unavailable; viewport checks at1280/390px.

Current Manual page had no browser console errors. One historical error from the separate agent's8771 policy preview was identified by its URL and reported to its owner; it was not emitted by this build.

Focused tests cover cancellation races, continuous IK, unresolved/overlapping fault kinds, stale samples, and positive clear evidence. Physical motion/leader pickup remain separate acceptance; no actual movement was issued by this visual QA.

## Implementation checklist

- Approved modes and persistent measured FK implemented.
- Key labels, speed, Home, source selectors, and gripper follow implemented.
- Upper-region compact controls and short-screen scrolling verified.
- Black/red marker behavior verified visually and with state-transition tests.
- No actionable P0/P1/P2 visual findings remain. P3: a solid mesh rendering is outside the requested schematic viewer.

final result: passed
