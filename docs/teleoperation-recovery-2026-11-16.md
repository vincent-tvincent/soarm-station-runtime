# Teleoperation recovery and status corrections — November 16, 2026

## Diagnosed causes and resulting behavior

- The native leader still opened a `live_sample` DIRECT stream, inheriting its
  250-ms input expiry even after browser lease removal. Leader, command-line,
  keyboard and gamepad teleoperation now select `persistent_target`.
- Persistent DIRECT streams no longer inherit profile duration/liveness/tracking
  expiration or terminate after a control-loop gap. Stale follower feedback
  pauses dispatch while preserving the run. An explicit caller-supplied duration,
  Stop, actual hardware faults and control-connection loss retain their effects.
- A reference outside one joint's measured tracking band previously blocked
  every actuator. Recovery now stays within that joint's velocity bound and
  permits inward movement without freezing other joints or snapping reference.
- Home left a browser DIRECT stream open while Leader mode remained selected,
  disabling Follow. Unlock can now retire that exact panel-owned stream, confirm
  hold, obtain permission and open the leader in ALIGNING. No motion is dispatched
  until raw leader angles match the held follower. There is no alignment timer.
- Two teardown races could falsely reject handoff: browser WSS EOF before scoped
  Hold, and leader EOF before the ROS status cache updated. The browser now stops
  local production but retains its exact socket through scoped confirmation;
  leader cleanup checks authoritative native hold completion. Stale scopes cannot
  retire replacement browser operations or hold replacement native owners.
- The Status panel read legacy fault fields instead of `motion_issue`. It now
  shows compact per-motor tracking/fault badges and details, using the same
  per-arm/session latch rules as FK. Stale or missing data does not clear faults.
- Jacobian rank is still computed from measured joints. The header shows the
  actual measured timestamp; the tooltip includes minimum singular value and
  rank tolerance. Full geometric rank often remains 5/5 while the SO-101 moves.
  No rank threshold or fabricated motion score was introduced.
- IK speed defaults to profile 3 in both UI and API. Rates and physical limits
  themselves are unchanged.

## Operator workflow

1. In Control → Leader arm, select the configured host and leader.
2. Press **Unlock / follow**. Move the leader to the held follower pose while
   watching the measured/leader overlays. Body pickup tolerance is 0.12 rad per
   joint; jaw tolerance is 10 percentage points when Gripper follow is enabled.
3. Once aligned, fresh samples control the follower. **Freeze / Esc** holds it.
4. After Freeze or Home, use Unlock and align again. Waiting does not expire.

Delayed packets remain rejected; they are not queued for later movement.
Alignment compares the raw calibrated pose, so clipping an out-of-range leader
value cannot falsely unlock the arm. The gripper is excluded from pickup when
Gripper follow is disabled. Reconnection never automatically restarts following.

## Validation

Browser regression tests cover Home/leader handoff, cancellation during handoff,
scoped socket retirement, rejection of replacement owners, status latch behavior
and measured timestamp/rank presentation. Local mock screenshots verify compact
layout. The physical configuration template loads, resolves the bundled model
and exposes Home/Fold without opening devices.

The full isolated ROS mock run passed through the real bridge, gateway,
ROS transport and native MockArm: 1,161 sampled poses and 424 command frames,
with zero browser heartbeats. The first 6.05-second ALIGNING phase produced
546 samples and zero commands/motor writes. A 0.95-second sampling pause kept
the same operation and permission; a deliberately delayed sample was discarded
without replay. Explicit Stop confirmed native Hold. A second run waited
2.08 seconds against the newly held pose, then followed and stopped correctly.
Both samplers closed; the test launch and its tracing session were cleaned up.

The initial full mock run exposed the native-EOF/ROS-cache stop-confirmation
race; the passing run includes its fix and focused regressions. These checks
do not claim physical leader-following or obstruction recovery was exercised
by the agent. Deployment is verified separately below.


## Final checks and deployment

- 348 JavaScript tests passed. The Linux regression suite plus its corrected
  native-cleanup fixture covered 168 passing tests and 72 subtests, including
  real socket-owner EOF cleanup, scoped Hold routes, controller continuity,
  leader input, motor issues and FK/IK calculations.
- Runtime source revision `a6d863b` was pushed and deployed from Git. The ROS
  package built successfully and the user service restarted while held/idle.
- Native boot changed from `086002afd8244503bdf6cf872c8ef508` to
  `97dbe68a421d41369dcaef6466cdae07`. Configuration and calibration hashes were
  unchanged. All six torques remained ON; measured joint, goal and gripper
  deltas were zero. The new worker issued zero motor goal writes.
- Feedback measured 99.79 Hz immediately after restart. Read-only HTTP checks
  confirmed advancing joint timestamps, profile 3, live 5/5 Jacobian rank,
  six motor issue records, three cameras and exact served-asset/source matches.
- The existing Mac input bridge restarted with the new code and re-advertised
  its configured leader while remaining idle. No physical following, Home,
  Fold or learned policy was executed by the agent.

Refresh the dashboard to load the new controls. In Leader mode use
**Unlock / follow**, align to the held pose, and use **Freeze** to pause.
