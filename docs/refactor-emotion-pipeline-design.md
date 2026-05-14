# Emotion pipeline refactor — design document

Status: proposed
Authors: nolte + assistant
Date: 2026-05-12
Companion: `docs/refactor-emotion-pipeline.md` (executive summary, motivation)

This document is the **implementation contract** for the refactor. It is intentionally precise: a future implementer should be able to read it and write the code without re-running the original investigation.

---

## 1. Goals and non-goals

### Goals

1. Reach stable, repeatable emotion playback over a sustained session (≥ 1 hour, ≥ 50 mixed-emotion triggers, no `Lost connection` warnings).
2. Adopt the reference-app movement pattern at `/home/nolte/repos/github/reachy-mini-app` for the emotion path: one worker thread per move, component-wise `_lerp_pose`, direct `reachy.set_target`, 50 Hz tick.
3. Keep all five App-Code-Bugs from `fix/emotion-pipeline-stability` fixed (HF preload, idle_rest_pose tunable, `set_automatic_body_yaw` discipline, `enable_motors()` at boot, encoder-based lead-in).
4. Preserve idle / sway / face-tracking / HA-driven head moves — those keep using `MovementManager`.
5. End-to-end traceability: every emotion has a `Queued → Started → Complete` log triple and observable encoder motion.

### Non-goals

1. Touching the Pollen SDK or daemon. We adapt to its contracts, not the other way around.
2. Replacing `RecordedMoves.evaluate(t)` as the source of animation pose data. It works — the bug was in how we consumed it.
3. A full kinematics rewrite. Reachability check stays as-is for the non-emotion path.
4. Replacing the file logger or threshold-tweak — those land in this refactor's preserved state.

---

## 2. Why a refactor instead of more patches

Five distinct bugs in one evening, each in a different layer of the same data path, is a structural signal. A summary of the current path lives in `docs/refactor-emotion-pipeline.md` §1. Two observations make a refactor cheaper than continued patching:

- **The reference app solves the same problem in ~30 LOC.** Reading `reference-app/blocks.py` shows there is a much simpler structure that already works on the same SDK version (1.7.1).
- **The current emotion path racing the MovementManager is the root concurrency hazard.** Every "silent IPC stall" we hit today was traceable to two sources of `set_target` calls competing for the same servos in the same control loop.

Pulling emotions out of the MovementManager eliminates the race surface, not just one bug instance at a time.

---

## 3. High-level architecture

```
                                    ┌───────────────────────────────────┐
HA service call                     │           reachy_mini SDK         │
  │                                 │                                   │
  ▼                                 │   ReachyMini instance (single)    │
ESPHome bridge                      │   ├── set_target(head, ant, by)   │
  │                                 │   ├── get_present_head_pose()     │
  ▼                                 │   ├── get_present_antenna_pos()   │
motion_bridge.queue_emotion_move(name)│   └── set_automatic_body_yaw()  │
  │                                 │                                   │
  ▼                                 └──────────────┬────────────────────┘
EmotionPlayer.play(name)  ◄─────────────── tells   │  set_target calls
  │                                    SDK what     │
  ├── pause MovementManager (event flag)            │
  ├── spawn worker thread:                          │
  │     │                                           │
  │     ├── read present pose                       │
  │     ├── lead-in interpolation (~0.5s)           │
  │     ├── 50 Hz tick loop:                        │
  │     │     pose = evaluate(t * speed)            │
  │     │     reachy.set_target(...)  ──────────────┤
  │     │     sleep(TICK_S)                         │
  │     ├── ease-out to idle_rest_pose (~0.5s)      │
  │     └── done                                    │
  └── resume MovementManager (clear event flag)     │
                                                    │
─────────────── MovementManager (other path) ───────┤
idle / sway / face tracking / HA-driven targets ────┘
(unchanged from today, only paused while emotion plays)
```

Two **completely separate** Python threads now own `reachy_mini.set_target`:

1. **MovementManager control loop** — currently exists, keeps its 100 Hz tick for idle/sway/face/target. When the EmotionPlayer signals "emotion in flight", the loop **skips the `issue_control_command` call entirely**. Reads continue (face tracking can still run).
2. **EmotionPlayer worker thread** — spawned per emotion, lives only for the duration of the playback. Owns `set_target` for that duration. Joins back when done.

Mutual exclusion is enforced by a single `threading.Event` on `MovementManager` (see §6).

---

## 4. Module layout

### New files

| File | Purpose | LOC estimate |
|---|---|---|
| `motion/emotion_pose.py` | `Pose` dataclass + scalar `_lerp_pose` helpers; port of `reference-app/blocks.py` lines 30-90 | ~60 |
| `motion/emotion_player.py` | `EmotionPlayer` class with `play()`, `cancel()`, internal worker | ~180 |

### Modified files

| File | Change |
|---|---|
| `motion/movement_manager.py` | Remove emotion bookkeeping (`_emotion_move`, `_emotion_move_lock`, `_emotion_start_time`, `_pre_emotion_*`, `_emotion_lead_in_duration_s`, `_emotion_playback_speed`, `_start_emotion_move`, `_update_emotion_move`). Add a single `_emotion_in_flight` `threading.Event` and a `pause_during_emotion()` / `resume_after_emotion()` pair. Repurpose the (currently dead) `_emotion_playing_event` for this. |
| `motion/control_runtime.py` | Remove `update_emotion_move`. In `run_control_loop`, replace the `if emotion_pose is not None / else compose_final_pose` branch with a single `if not manager._emotion_in_flight.is_set(): … compose_final_pose …`. |
| `motion/command_runtime.py` | Replace `start_emotion_move` dispatch with a call to the app-wide `EmotionPlayer.play(name)`. |
| `protocol/motion_bridge.py` | `queue_emotion_move` calls `EmotionPlayer.play(name)` instead of `MovementManager.queue_emotion_move`. |
| `main.py` | Wire the singleton `EmotionPlayer` in `run()`, hand it the `ReachyMini` instance and a reference to the `MovementManager`. |

### Deleted code

- `MovementManager.pause_for_emotion` / `resume_after_emotion` / `is_emotion_playing` (deprecated, dead-coded today, but their underlying event flag gets reused with the new owner).
- `MovementManager.queue_emotion_move` (the public entry point that funnels into `command_runtime.start_emotion_move`).
- `MovementManager._start_emotion_move` and all `_emotion_*` lead-in state on `MovementManager`.
- `command_runtime.start_emotion_move` and the `"emotion_move"` branch in its `handle_command`.

---

## 5. Class skeletons

### 5.1 `EmotionPose` (motion/emotion_pose.py)

Direct port from `reference-app/blocks.py`. Component-wise scalar dataclass; the head matrix is rebuilt on every tick via `create_head_pose`. **This is the key correctness property the previous attempt got wrong**: we never linearly interpolate rotation matrices — we interpolate scalars and build the matrix from scratch.

```python
from dataclasses import dataclass

@dataclass(frozen=True)
class EmotionPose:
    """Head + antennas + body yaw at one instant."""
    # head, in SDK units (m / rad)
    x: float = 0.0
    y: float = 0.0
    z: float = 0.0
    roll: float = 0.0
    pitch: float = 0.0
    yaw: float = 0.0
    # antennas, in rad — SDK ordering is [right, left]
    antenna_right: float = 0.0
    antenna_left: float = 0.0
    body_yaw: float = 0.0

def lerp_pose(a: EmotionPose, b: EmotionPose, s: float) -> EmotionPose:
    """Linear interpolation between two poses, s in [0, 1]."""
    def L(x, y): return x + (y - x) * s
    return EmotionPose(
        x=L(a.x, b.x), y=L(a.y, b.y), z=L(a.z, b.z),
        roll=L(a.roll, b.roll), pitch=L(a.pitch, b.pitch), yaw=L(a.yaw, b.yaw),
        antenna_right=L(a.antenna_right, b.antenna_right),
        antenna_left=L(a.antenna_left, b.antenna_left),
        body_yaw=L(a.body_yaw, b.body_yaw),
    )

def from_recorded_move_sample(head_matrix, antennas, body_yaw) -> EmotionPose:
    """Convert a `RecordedMoves.evaluate(t)` tuple into our `EmotionPose`.

    `RecordedMoves.evaluate(t)` returns:
      head_matrix : np.ndarray shape (4, 4)
      antennas    : np.ndarray shape (2,) — [right, left] in radians
      body_yaw    : float in radians

    We extract Euler from the 4x4 matrix for our scalar dataclass. The
    matrix → euler → matrix round-trip is a small lossy step but
    correctness-preserving (each axis stays orthonormal). The lerp_pose
    output is then converted back to a 4x4 matrix at apply() time.
    """
    # decomposition implementation in the file
    ...

def to_set_target(pose: EmotionPose):
    """Build the (head_matrix, antennas_array, body_yaw) tuple that
    `reachy.set_target(**kwargs)` accepts."""
    head = create_head_pose(
        x=pose.x, y=pose.y, z=pose.z,
        roll=pose.roll, pitch=pose.pitch, yaw=pose.yaw,
        mm=False, degrees=False,
    )
    antennas = np.array([pose.antenna_right, pose.antenna_left], dtype=np.float64)
    return head, antennas, float(pose.body_yaw)
```

### 5.2 `EmotionPlayer` (motion/emotion_player.py)

```python
class EmotionPlayer:
    """Owns emotion playback. Runs each move in a worker thread.

    Invariants:
    - At most one worker thread alive at any time.
    - The worker thread is the *only* writer to reachy.set_target() while
      it lives.
    - On entry: pauses MovementManager. On exit (success, cancel, exception):
      resumes MovementManager.
    """

    LEAD_IN_S = 0.5         # smooth pre-emotion → evaluate(0)
    EASE_OUT_S = 0.5        # smooth evaluate(end) → idle_rest_pose
    TICK_S = 0.02           # 50 Hz, same as reference app
    PLAYBACK_SPEED = 0.5    # half speed; operator-confirmed

    def __init__(self, reachy, movement_manager, idle_rest_pose_provider):
        self._reachy = reachy
        self._mm = movement_manager
        self._idle_rest = idle_rest_pose_provider  # callable -> EmotionPose
        # NOTE: an idle_rest_pose with antennas at exactly 0° triggers a
        # ±0.5° servo-deadband wobble on the real hardware — see
        # docs/emotion-catalog.md §1.1. Prefer non-zero antenna setpoints.
        self._lock = threading.Lock()
        self._worker: threading.Thread | None = None
        self._cancel_event = threading.Event()
        self._current_emotion_name: str | None = None

    # --- public API -------------------------------------------------------

    def play(self, emotion_name: str) -> bool:
        """Trigger a new emotion. Cancel-and-replace if one is in flight.

        Returns True if scheduled, False if emotion library is unavailable.
        """
        ...

    def cancel(self) -> None:
        """Signal the worker to exit at the next tick. Worker eases out
        to idle_rest_pose, then joins."""
        ...

    def is_playing(self) -> bool:
        with self._lock:
            return self._worker is not None and self._worker.is_alive()

    # --- internal: worker --------------------------------------------------

    def _worker_main(self, emotion_name: str) -> None:
        """Runs in its own thread. Owns set_target for the duration."""
        try:
            self._mm.pause_during_emotion()
            self._reachy.set_automatic_body_yaw(False)
            try:
                emotion = EmotionMove(emotion_name)
                self._run_animation(emotion)
            finally:
                self._reachy.set_automatic_body_yaw(True)
                self._mm.resume_after_emotion()
        except Exception as e:
            logger.exception("Emotion %s failed in worker: %s", emotion_name, e)

    def _run_animation(self, emotion) -> None:
        # phase 1: lead-in (real present pose → evaluate(0))
        start_pose = self._read_present_pose()
        first_anim_pose = from_recorded_move_sample(*emotion.evaluate(0.0))
        self._run_phase(start_pose, first_anim_pose, self.LEAD_IN_S)

        # phase 2: the animation itself, scaled by playback speed
        self._run_animation_body(emotion)

        # phase 3: ease-out to idle_rest_pose
        last_anim_pose = from_recorded_move_sample(
            *emotion.evaluate(emotion.duration - 1e-3)
        )
        rest_pose = self._idle_rest()
        self._run_phase(last_anim_pose, rest_pose, self.EASE_OUT_S)

    def _run_phase(self, start, end, duration_s) -> None:
        t0 = time.monotonic()
        while not self._cancel_event.is_set():
            elapsed = time.monotonic() - t0
            if elapsed >= duration_s:
                break
            s = elapsed / duration_s
            self._apply(lerp_pose(start, end, s))
            time.sleep(self.TICK_S)
        if not self._cancel_event.is_set():
            self._apply(end)

    def _run_animation_body(self, emotion) -> None:
        t0 = time.monotonic()
        anim_duration = emotion.duration
        scaled_duration = anim_duration / self.PLAYBACK_SPEED
        while not self._cancel_event.is_set():
            elapsed = time.monotonic() - t0
            if elapsed >= scaled_duration:
                break
            t = min(elapsed * self.PLAYBACK_SPEED, anim_duration - 1e-3)
            pose = from_recorded_move_sample(*emotion.evaluate(t))
            self._apply(pose)
            time.sleep(self.TICK_S)

    def _apply(self, pose: EmotionPose) -> None:
        head, antennas, body_yaw = to_set_target(pose)
        self._reachy.set_target(head=head, antennas=antennas, body_yaw=body_yaw)

    def _read_present_pose(self) -> EmotionPose:
        head_mat = self._reachy.get_current_head_pose()
        antennas = self._reachy.get_present_antenna_joint_positions()
        # body_yaw read isn't exposed by the SDK we use; default to 0.0,
        # the SDK will reconcile via automatic_body_yaw on exit.
        return from_recorded_move_sample(head_mat, antennas, 0.0)
```

---

## 6. Concurrency model

### 6.1 The pause/resume contract

`MovementManager` exposes:

```python
def pause_during_emotion(self) -> None:
    """Called by EmotionPlayer before its worker thread starts emitting
    set_target. Sets _emotion_in_flight. The control loop continues to
    run for state updates (face tracking, sway accumulators) but skips
    issue_control_command()."""
    self._emotion_in_flight.set()

def resume_after_emotion(self) -> None:
    """Called by EmotionPlayer after its worker exits (success or
    failure). Clears _emotion_in_flight and lets issue_control_command()
    resume."""
    self._emotion_in_flight.clear()
```

`run_control_loop` adapts:

```python
while not manager._stop_event.is_set():
    # ... existing pre-tick bookkeeping ...
    manager._poll_commands()
    if manager._robot_paused_event.is_set():
        ...
    if manager._emotion_in_flight.is_set():
        # state updates that don't touch hardware still run
        manager._update_face_tracking()
        manager._update_antenna_blend(dt)
        time.sleep(manager._target_period - (manager._now() - loop_start))
        continue
    # normal compose_final_pose + issue_control_command path
    ...
```

### 6.2 Thread inventory after refactor

| Thread | Owner | Lifetime | Touches `set_target` |
|---|---|---|---|
| asyncio main loop | `main.py` | app lifetime | no |
| `MovementManager` control loop | `MovementManager._control_loop` | app lifetime | yes — *only when emotion not in flight* |
| `EmotionPlayer` worker | `EmotionPlayer._worker_main` | one emotion playback | yes — *exclusive ownership during its life* |
| voice / ESPHome / camera | various | app lifetime | no |

### 6.3 Mutual exclusion

- The `EmotionPlayer._lock` guards the singleton worker reference. Cancel-and-replace acquires this lock to swap the worker.
- `_emotion_in_flight` is the cross-thread handshake with the MovementManager. Set before the worker emits its first `set_target`, cleared after the worker's last `set_target` returns.
- The MovementManager's control loop *reads* `_emotion_in_flight` with no lock; `threading.Event` provides the memory-fence guarantee.

### 6.4 Cancel-and-replace semantics

A new `play("X")` while emotion "Y" is running:

1. `play("X")` acquires `_lock`.
2. It sets `_cancel_event` and `worker.join(timeout=0.3)`. The worker is mid-tick — it observes the event at the top of its next loop iteration, runs its ease-out phase, and exits cleanly.
3. `_cancel_event.clear()`, set `_current_emotion_name="X"`, spawn a new worker with "X".

The ease-out of the cancelled emotion **does** play (so the robot doesn't snap). Total worst-case takeover latency: `EASE_OUT_S + LEAD_IN_S` (≈ 1.0 s), which is fine for a chat-style UX.

---

## 7. Data flow — one emotion's lifecycle

```
t = 0.000 s   HA fires service call (e.g. "play emotion: enthusiastic1")
              esphome bridge → motion_bridge.queue_emotion_move("enthusiastic1")
              EmotionPlayer.play("enthusiastic1")
                acquires _lock
                cancel-and-replace (no-op if none running)
                spawns worker thread
                returns immediately to caller

t = 0.001 s   worker thread starts
              _mm.pause_during_emotion()   → _emotion_in_flight.set()
              _reachy.set_automatic_body_yaw(False)
              loads EmotionMove("enthusiastic1")   ← cache hit since e69caf9

t = 0.002 s   lead-in phase begins
              read present pose from encoders
              evaluate(0.0) for animation start
              lerp 50 Hz for 0.5 s

t = 0.502 s   animation body begins
              for each 20-ms tick:
                t_anim = elapsed * 0.5  (scaled speed)
                pose = evaluate(t_anim)
                set_target(pose)

t = 5.962 s   animation body ends (2.73 s anim @ 0.5x = 5.46 s)
              ease-out phase: lerp from last anim pose to idle_rest_pose
              50 Hz for 0.5 s

t = 6.462 s   worker post-loop:
                _reachy.set_automatic_body_yaw(True)
                _mm.resume_after_emotion()    → _emotion_in_flight.clear()
              thread exits

t = 6.463 s   MovementManager's control loop observes the cleared event,
              resumes issuing compose_final_pose / set_target at 100 Hz
              for idle / sway / face-tracking / HA-target work
```

Total wall time per emotion (with current defaults): `LEAD_IN + duration/speed + EASE_OUT = 0.5 + 2.73/0.5 + 0.5 = 5.96 s` for enthusiastic1. Adjusting `PLAYBACK_SPEED` and the LEAD_IN / EASE_OUT constants lets us tune.

---

## 8. State machine

```
        ┌──────────────┐
        │     IDLE     │ ← initial; MovementManager runs normally
        └──────┬───────┘
               │ play(name)
               ▼
        ┌──────────────┐
        │ STARTING     │ pause MM, disable auto body_yaw,
        │              │   load emotion
        └──────┬───────┘
               │
               ▼
        ┌──────────────┐  cancel()  ┌───────────────┐
        │  LEAD_IN     ├────────────►│  EASING_OUT   │
        └──────┬───────┘             └───────┬───────┘
               │                             │
               │ lead-in complete            │
               ▼                             │
        ┌──────────────┐  cancel()           │
        │  ANIMATING   ├─────────────────────┤
        └──────┬───────┘                     │
               │                             │
               │ animation body complete     │
               ▼                             │
        ┌──────────────┐                     │
        │  EASING_OUT  │◄────────────────────┘
        └──────┬───────┘
               │
               │ ease-out complete
               ▼
        ┌──────────────┐
        │ COMPLETING   │ re-enable auto body_yaw, resume MM
        └──────┬───────┘
               │
               ▼
              IDLE
```

`cancel()` from any of the in-flight states (`LEAD_IN`, `ANIMATING`) routes the worker through `EASING_OUT` (not an instant stop) — so the robot never snaps.

---

## 9. Error handling

| Failure | Detection | Recovery |
|---|---|---|
| Emotion library not loaded | `EmotionMove(name)` raises | log warning, abort `_worker_main` early, ensure `resume_after_emotion()` runs in `finally` |
| `evaluate(t)` raises mid-animation | exception in `_run_animation_body` | log error, jump to ease-out using the last successfully-emitted pose, then resume MM |
| `reachy.set_target` raises | exception inside `_apply` | propagate; outer `_worker_main`'s `finally` clause always runs `resume_after_emotion()` and `set_automatic_body_yaw(True)`. No retry loop — that complexity belongs to the SDK |
| Worker thread doesn't join on cancel | `worker.join(timeout=0.3)` returns False | log a warning; spawn the new worker anyway (the stuck one is still `daemon=True` so process shutdown isn't blocked) |
| MovementManager stuck not noticing the event | not currently detectable | acceptable: the worker is the only `set_target` source while it lives, so a missed event by MM is harmless |

The key invariant the error model enforces: **`MovementManager` cannot be left paused after a worker exits**, because `resume_after_emotion()` is in a `finally` block.

---

## 10. Tunable parameters

All exposed as class constants on `EmotionPlayer` for v1; later moved to `Config.motion` once the design settles.

| Constant | Default | Effect |
|---|---|---|
| `LEAD_IN_S` | 0.5 | Time to smoothly enter the animation. Bigger = gentler, slower-feeling robot. |
| `EASE_OUT_S` | 0.5 | Time to smoothly return to rest after the animation. |
| `TICK_S` | 0.02 | Control rate for the worker (50 Hz, same as reference). |
| `PLAYBACK_SPEED` | 0.5 | Multiplier on `evaluate(t)` time. < 1.0 = slower animation. Operator-confirmed default. |

`idle_rest_pose` continues to live in `conversation_animations.json` as it does today — the JSON is the operator-facing tuning surface.

---

## 11. Testing strategy

### 11.1 Unit tests (`tests/motion/`)

| Test | What it verifies |
|---|---|
| `test_emotion_pose.py::test_lerp_endpoints` | `lerp_pose(a, b, 0) == a`, `lerp_pose(a, b, 1) == b` |
| `test_emotion_pose.py::test_lerp_midpoint` | Component-wise midpoint check on a hand-built pose pair |
| `test_emotion_pose.py::test_recorded_move_roundtrip` | `from_recorded_move_sample(*evaluate(t)) → to_set_target` produces a valid 4×4 head matrix (orthonormal rotation block) |
| `test_emotion_player.py::test_cancel_idempotent` | Two `cancel()` in a row don't deadlock |
| `test_emotion_player.py::test_resume_after_exception` | Mock `set_target` to raise; verify `resume_after_emotion()` still fires |

### 11.2 Integration tests (with `ReachyMini(spawn_daemon=True, use_sim=True)`)

| Test | What it verifies |
|---|---|
| `test_play_single_emotion` | A full emotion runs to `Complete`, hardware encoders move within expected envelope, MM is unpaused after |
| `test_cancel_and_replace` | Trigger A then trigger B 1 s later; verify A's worker eased out, B's worker started, no overlap |
| `test_back_to_back_emotions` | Eight emotions queued sequentially; every one reaches `Complete` |

### 11.3 Live-hardware test (manual, against real Reachy)

The script from today's session — the 10 Hz pose-tracker against `/api/state/present_*` endpoints — becomes a regression harness. Acceptance: 30 minutes of mixed-emotion triggers with **zero `Lost connection` log lines** and encoders staying within `[-π, +π]` rad for the whole session.

---

## 12. Migration plan (suggested order)

### Phase 1 — additive, no behavior change yet

1. Add `motion/emotion_pose.py` with the `EmotionPose` dataclass and helpers.
2. Add `motion/emotion_player.py` with the `EmotionPlayer` class — fully implemented, but not yet wired.
3. Unit tests for both modules.

### Phase 2 — wire in, behavior changes

4. Add the `_emotion_in_flight` event on `MovementManager` and the `pause_during_emotion` / `resume_after_emotion` methods.
5. Modify `control_runtime.run_control_loop` to skip `issue_control_command` while the event is set (keep face tracking + state updates).
6. Modify `motion_bridge.queue_emotion_move` to call `EmotionPlayer.play(name)`.
7. Instantiate `EmotionPlayer` as a singleton in `main.py:run()` and inject into `motion_bridge`.

### Phase 3 — clean up dead code

8. Delete `update_emotion_move` in `control_runtime.py` and the entire `if emotion_pose is not None` branch in `run_control_loop`.
9. Delete `_emotion_move`, `_emotion_move_lock`, `_emotion_start_time`, `_pre_emotion_*`, `_emotion_lead_in_duration_s`, `_emotion_playback_speed` on `MovementManager`.
10. Delete `MovementManager._start_emotion_move`, `MovementManager.queue_emotion_move`, `pause_for_emotion`, `resume_after_emotion`, `is_emotion_playing`.
11. Delete `command_runtime.start_emotion_move` and remove the `"emotion_move"` case from `handle_command`.

### Phase 4 — verify

12. Live-hardware test (§11.3).
13. Update `docs/refactor-emotion-pipeline.md` to mark the spec as "implemented in PR #N".

---

## 13. Acceptance criteria (PR-level)

- [ ] `motion/emotion_player.py` and `motion/emotion_pose.py` exist and are unit-tested.
- [ ] `MovementManager` no longer carries any `_emotion_*` state.
- [ ] `control_runtime.update_emotion_move` is gone.
- [ ] All five existing fixes from `fix/emotion-pipeline-stability` are preserved by behaviour (HF preload, body-yaw discipline, motor enable at start, idle_rest_pose tunable, encoder-based pose reads).
- [ ] Eight different emotions trigger sequentially in HA without a single `Lost connection` log line and with every one reaching a `Complete` log entry.
- [ ] Antennas remain within `[-π, +π]` rad throughout a 30-minute mixed-trigger session.
- [ ] Idle / sway / face tracking behaviour visually unchanged.
- [ ] `pause_during_emotion` / `resume_after_emotion` correctly handle a `set_target` exception inside the worker (the `finally` block must run; verified by unit test).

---

## 14. Open questions to settle before implementation

1. **`body_yaw` read on entry.** SDK 1.7.1 exposes `get_present_head_pose` and `get_present_antenna_joint_positions` but the body_yaw read surface is less clear. Need to confirm via `inspect.signature` whether `get_present_body_yaw` exists or whether we just trust `automatic_body_yaw(True)` to converge before the next emotion starts.
2. **Cancel-and-replace UX.** If HA fires triggers faster than `LEAD_IN + EASE_OUT` (≈ 1 s), do we want a queue (FIFO) or "always take the latest"? Current proposal: take-the-latest (cancel-and-replace). Operator preference TBC.
3. **Tunables in JSON vs class constants.** For v1 we keep `LEAD_IN_S` / `EASE_OUT_S` / `PLAYBACK_SPEED` as class constants. Operator may want a `conversation_animations.json` section for these later — explicitly out of scope for v1 to keep the diff small.
4. **`set_automatic_body_yaw(False)` while another consumer of body_yaw is active.** Today the MovementManager is paused during emotions, but if a future feature (e.g. ESPHome-driven head-look-at) wants body_yaw concurrent with an emotion, we need a richer arbitration model. Out of scope for v1.

---

## 15. References

- Reference implementation we're adopting from: `/home/nolte/repos/github/reachy-mini-app/reachy_mini_app/blocks.py` (the `_run_phase` + `apply` pattern).
- Companion overview: `docs/refactor-emotion-pipeline.md`.
- All five bug fixes from `fix/emotion-pipeline-stability` (PR #1): see the commit bodies for verification snippets.
- File logger added in commit `e69caf9` lives at `/tmp/reachy_mini_home_assistant.log` and is the primary diagnostic surface during the refactor.
