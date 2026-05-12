# Refactor spec: emotion pipeline → reference-app pattern

Status: draft (proposed)
Authors: nolte + assistant
Date: 2026-05-12
Trigger: end of a 4-hour triage session that found 5 distinct bugs in the
current emotion pipeline; operator concluded "the whole movement
implementation needs to be reworked" (verbatim).

## 1. Why this exists

Between commit `e69caf9` and commit `29a736b` we fixed five distinct
implementation bugs in the emotion path of `reachy_mini_home_assistant`:

| Commit | Bug |
|---|---|
| `e69caf9` | HF emotion library was loaded synchronously on the asyncio thread on first trigger, freezing the whole app. Plus 4 pre-conditions. |
| `ea32312` / refined | `idle_rest_pose` antennas pinned at ±3.05 rad (≈ ±175°) — only 0.09 rad off the mechanical end stop. |
| `4a6ebc1` | App pushed body_yaw setpoints every tick while SDK's `automatic_body_yaw` tracker was also driving the same servo. Silent IPC stall, especially on `enthusiastic1`. |
| `7271725` | App never called `enable_motors()` at start. After daemon restart, set_target calls reached the daemon but were ignored. |
| `a6c5ea3` + `e93913b` + `29a736b` | Emotion lead-in: matrix linear interpolation was mathematically invalid; pre-emotion pose was read from the last setpoint instead of the real encoder, producing 360° servo paths. |

Every single one of these is real. Every one is in our app, not in
Pollen's SDK. But the rate at which they fell out tells us the
**structure** of the emotion pipeline is too complex for what it actually
needs to do.

Concretely, this is what an emotion trigger has to traverse today:

```
HA service call
  → ESPHome bridge
  → motion_bridge.queue_emotion_move
  → MovementManager._enqueue_command("emotion_move", name)
  → command_runtime.start_emotion_move
    → EmotionMove(name)               # lazy-loads HF library
    → set_automatic_body_yaw(False)   # the fix from 4a6ebc1
    → capture pre-emotion pose         # the fix from 29a736b
    → _emotion_move = move

while in MovementManager control loop @ 100 Hz:
  → update_emotion_move
    → if in lead-in window:
        → interpolate antennas (scalar)
        → snap head_pose to evaluate(0)         # fix from e93913b
        → return composed pose
    → else if elapsed >= total_wall_time:
        → log Complete
        → set_automatic_body_yaw(True)
        → _emotion_move = None
        → return None
    → else:
        → evaluate(elapsed * speed)
        → clamp body_yaw
        → return composed pose
  → if not None: issue_control_command(head, antennas, body_yaw)
  → else: compose_final_pose with 5 sources (target / anim / sway / face_offsets / idle)
        → reachability filter
        → antenna_controller.get_blended_positions (with freeze logic)
        → issue_control_command
```

And `issue_control_command` itself has its own connection-loss /
retry-backoff state machine.

A bug can hide in any of these layers. We found five today; we
don't have confidence there isn't a sixth.

## 2. The reference-app comparison

`/home/nolte/repos/github/reachy-mini-app/reachy_mini_app/blocks.py`
solves the same problem in **one function**:

```python
def _run_phase(reachy, stop_event, start, phase):
    if phase.duration <= 0:
        apply(reachy, phase.end)
        return phase.end
    t0 = time.time()
    while not stop_event.is_set():
        elapsed = time.time() - t0
        if elapsed >= phase.duration:
            break
        s = time_trajectory(min(1.0, elapsed / phase.duration), phase.easing)
        apply(reachy, _lerp_pose(start, phase.end, s))
        time.sleep(TICK_S)
    apply(reachy, phase.end)
    return phase.end
```

Where `apply` is:

```python
def apply(reachy, pose):
    head = create_head_pose(x=pose.x, ..., mm=True, degrees=True)
    antennas_rad = np.deg2rad([pose.antenna_right, pose.antenna_left])
    reachy.set_target(head=head, antennas=antennas_rad,
                      body_yaw=float(np.deg2rad(pose.body_yaw)))
```

That's it. 50 Hz tick, component-wise lerp on a `Pose` dataclass,
construct the matrix from scalars each tick, direct `set_target`. No
composer, no filter, no freeze, no 100-Hz control thread fighting a
separate emotion path.

This works. Reproducibly. With the same SDK version (1.7.1).

## 3. Proposed refactor

**Pull emotions out of the `MovementManager` entirely.** Run each
emotion in its own worker thread that owns the `reachy_mini` instance
for the duration of the move, exactly like the reference app's blocks do.

### 3.1 New architecture (target)

```
HA service call
  → ESPHome bridge
  → motion_bridge.queue_emotion_move(name)
  → EmotionPlayer.play(name)
       │
       ├── pause MovementManager (it stops issuing set_target)
       ├── spawn worker thread:
       │     ├── read present pose from SDK
       │     ├── load emotion via RecordedMoves.evaluate
       │     ├── for tick in animation_duration / TICK_S:
       │     │     pose = component-wise lerp(start_pose, evaluate(tick * speed))
       │     │     reachy.set_target(head=pose.head, antennas=pose.antennas, body_yaw=pose.body_yaw)
       │     │     time.sleep(TICK_S)
       │     └── ease back to idle_rest_pose over 0.5 s
       └── resume MovementManager
```

The `MovementManager` keeps its existing job for idle / face tracking /
sway / HA-entity-driven target moves. **Emotions live entirely in the
worker thread layer above it.**

### 3.2 What the refactor deletes

- `start_emotion_move` in `command_runtime.py`
- `_start_emotion_move` in `movement_manager.py`
- `_emotion_move`, `_emotion_move_lock`, `_emotion_start_time` on `MovementManager`
- `_emotion_lead_in_duration_s`, `_emotion_playback_speed`,
  `_pre_emotion_head_pose`, `_pre_emotion_antennas`, `_pre_emotion_body_yaw`
- `update_emotion_move` in `control_runtime.py`
- The whole emotion branch in `run_control_loop`
- The deprecated `pause_for_emotion` / `resume_after_emotion` / `_emotion_playing_event` filter

### 3.3 What the refactor keeps

- `EmotionMove` / `RecordedMoves` integration in `motion/emotion_moves.py`
  (with the eager HF preload from commit `e69caf9` — that fix is
  orthogonal and still correct)
- `motion_bridge.queue_emotion_move` as the entry point (signature stays
  the same, only the implementation changes)
- `MovementManager` for idle / face tracking / sway / HA-driven targets
- `enable_motors()` at MovementManager start (`7271725`)
- `set_automatic_body_yaw(False/True)` around emotion playback (`4a6ebc1`)
- `idle_rest_pose` JSON-driven (operator can still tune the
  antenna angles at rest)

### 3.4 New module layout

```
reachy_mini_home_assistant/motion/
  emotion_player.py          # NEW — the worker thread + run loop
  emotion_pose.py            # NEW — Pose dataclass + lerp helpers (copy of reference's blocks.Pose)
  emotion_moves.py           # KEPT — RecordedMoves wrapper with eager preload
  movement_manager.py        # SHRUNK — emotion code removed, focus on idle/sway/face
  control_runtime.py         # SHRUNK — update_emotion_move + emotion branch removed
```

`emotion_player.py` is the only new file with significant logic, and it
is roughly a port of `reference-app/blocks.py` adapted to use
`RecordedMoves.evaluate(t)` for the target pose stream.

## 4. Open questions to resolve before implementation

- **Pause semantics**: how does the `MovementManager` cleanly stop
  issuing `set_target` while the worker owns the SDK? Option A:
  shared `threading.Event` (`_emotion_owns_robot`). Option B: thread-safe
  flag the control loop checks at the top of each tick. Probably
  option A — and we can resurrect / reuse the `_emotion_playing_event`
  flag that's currently dead code, but for its originally documented
  purpose.
- **Ease-out**: when the emotion's `evaluate()` ends, the recorded
  pose can be far from `idle_rest_pose`. The reference-app pattern is to
  add a final phase that interpolates back to neutral. We should do the
  same — say a 0.5 s linear lerp from `evaluate(duration)` to
  `idle_rest_pose`. (This is actually a *better* fix for the cross-emotion
  end-stop drift than today's lead-in alone.)
- **Antenna joint convention**: today's testing surfaced confusion about
  whether `0 rad` is "straight up" or "horizontal" or "neutral other".
  Before the refactor merges, we should write a one-paragraph appendix
  in the spec stating the convention as we now understand it (operator
  feedback after the refactor will confirm).
- **Speed multiplier default**: today we set `_emotion_playback_speed =
  0.5`. Operator confirmed that's the desired feel. The new
  `EmotionPlayer` should ship with the same default and expose it as a
  per-emotion or app-wide setting later if needed.
- **Concurrency invariants**: only one emotion at a time. Trigger during
  an active emotion should either queue (preferred for HA UX), reject, or
  cancel-and-replace. Operator preference: cancel-and-replace (matches
  the reference app's `_request` pattern).

## 5. Implementation steps (when we do this)

1. Add `emotion_pose.py` with `Pose` dataclass + `_lerp_pose` (copy
   reference-app verbatim, adapt unit conventions to whatever this
   project standardises on).
2. Add `emotion_player.py` with the `EmotionPlayer` class:
   `play(name)`, `cancel()`, worker thread function.
3. Wire `motion_bridge.queue_emotion_move` to `EmotionPlayer.play`
   instead of `MovementManager.queue_emotion_move`.
4. Add the pause/resume hook on `MovementManager` (resurrect
   `_emotion_playing_event` with new owner).
5. Delete the dead-code paths listed in §3.2.
6. Verify with a 30-minute live test session, running the eight
   most-used emotions back-to-back with the file logger (`/tmp/reachy_mini_home_assistant.log`)
   and the pose tracker we built today. Acceptance: every emotion
   reaches `Complete` in the log, hardware encoders stay within ±180°
   for the entire session, no `Lost connection` warnings.

## 6. Acceptance criteria for the refactor PR

- [ ] All five fixes from today's session preserved in behaviour
- [ ] No `update_emotion_move` in `control_runtime.py`
- [ ] No `_emotion_move` state on `MovementManager`
- [ ] `EmotionPlayer` worker thread is the only place emotions touch
  `reachy_mini.set_target`
- [ ] Eight different emotions trigger sequentially in HA without any
  `Lost connection` log lines and with every one reaching a `Complete`
  log entry
- [ ] Antennas remain within `[-π, +π]` rad for the entire test session
- [ ] Idle / sway / face-tracking behaviour unchanged
