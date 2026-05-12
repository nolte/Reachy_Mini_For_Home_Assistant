# Emotion catalog — base set for the HA voice assistant

Status: proposed
Authors: nolte + assistant
Date: 2026-05-12
Companion to: `docs/refactor-emotion-pipeline.md` (motivation) and `docs/refactor-emotion-pipeline-design.md` (architecture).

This catalog defines the **first set of hand-authored emotion moves** that the new `EmotionPlayer` will play. They are intentionally hand-authored as `Phase` sequences (reference-app pattern) instead of consumed from Pollen's `RecordedMoves` HF dataset, for three reasons:

1. **Mechanical safety**. Every pose value below is constrained to the operationally-safe envelope we derived during today's triage — antennas at most ±90°, pitch ±25°, no setpoint requires a > 180° servo path. None of the Pollen `RecordedMoves` we tested today respect this envelope (rage1 commands ±160° antennas, disgusted1 ±157°, surprised1 ±119°).
2. **Predictable visual style**. The HA voice assistant has a specific visual vocabulary — listening, thinking, speaking, acknowledging — that needs more deliberate motion than the Pollen demo set, which leans on dance/idle moves.
3. **Authoring control**. A `Phase` table the operator can read and edit by hand is far more maintainable than a 30-MB HF-hosted recording.

The `RecordedMoves` library stays available as an opt-in extra. The hand-authored set below is what the App emits by default.

---

## 1. Pose units and conventions

| Field | Unit | Range used | Mechanical note |
|---|---|---|---|
| `x`, `y`, `z` | millimetres | ±10 mm | Translation of the head center |
| `pitch`, `yaw`, `roll` | degrees | pitch ±25°, yaw ±30°, roll ±20° | All well inside reachable workspace |
| `antenna_left`, `antenna_right` | degrees | ±90° max | π/2 = 1.57 rad, 90° clearance to the mechanical end stop at ±π. **This is the post-triage safe envelope.** |
| `body_yaw` | degrees | ±20° | Daemon's `automatic_body_yaw(False)` is engaged for the duration of the playback (see design doc §6) |

Antenna convention: positive value of *both* antennas means "both tilted in the same nominal direction". The operator's preferred rest pose for the app is `±60°` (antennas mildly tilted down without crowding the end stop — see commit history `b46283e`–`5cfe660` for the tuning exploration).

Easing types reuse `reachy_mini.utils.interpolation.InterpolationTechnique`:

- **MIN_JERK** — default; smooth start, smooth end. Used for most natural movements.
- **EASE_IN_OUT** — slow start, fast middle, slow end. Used for sweeping moves.
- **CARTOON** — overshoot + settle. Used for surprise / "snappy" moves.
- **LINEAR** — constant velocity. Used for short bursts.

---

## 2. The base emotion set

Ten emotions cover the HA voice-assistant lifecycle. They are grouped by trigger source.

### Conversational-state emotions (continuous, looping)

| ID | When triggered | Description |
|---|---|---|
| `idle` | App in idle, no HA activity | Slow breathing motion, low energy |
| `listening` | Wake word detected, waiting for utterance | Alert, slight head lift, antennas raised |
| `thinking` | TTS/STT in flight | Subtle side-to-side head tilt, slower |
| `speaking` | TTS playing back | Lively head movement synced to speech rhythm |

### Discrete reaction emotions (one-shot, ~2-4 s each)

| ID | When triggered | Description |
|---|---|---|
| `happy` | Voice command succeeded, positive event | Joyful head lift + small antenna flare |
| `sad` | Negative event, failure | Head drop, antennas lowered |
| `surprised` | Unexpected event (door bell, alarm) | Quick recoil + antennas snap up |
| `curious` | Ambiguous request | Head tilts to one side, brief hold |
| `acknowledge` | Command accepted / received | Single small nod |
| `error` | Command failed / unintelligible | Slow head shake (no) |

Each emotion is fully specified below as a sequence of `Phase` entries (target pose + duration + easing). Total durations are deliberately short (≤ 4 s for discrete; continuous moves loop).

---

## 3. Pose specifications

### 3.1 `idle` (continuous, looping)

Slow breathing. Loops indefinitely until cancelled. **Identical** to the reference app's `waiting-idle` block at BREATH_FREQ = 0.25 Hz (4 s per cycle).

```
sinusoidal generator (not phase-based; runs in its own oscillator loop)
  z      = 2 mm * sin(2π · 0.25 · t)
  pitch  = 1° * sin(2π · 0.25 · t)
  antL   = 2° * sin(2π · 0.25 · t)
  antR   = 2° * sin(2π · 0.25 · t)
  other  = 0
```

No phases; this is a generator. The `EmotionPlayer` recognises `idle` as a special-case continuous emotion and runs the oscillator until a different emotion is requested.

---

### 3.2 `listening` (continuous, looping)

Alert posture with a gentle 0.6 Hz oscillation. Lifts the head ~3°, antennas held at +20°/-20° base with a small sinusoidal wiggle.

```
sinusoidal generator
  z      = 3 mm  + 1 mm  * sin(2π · 0.6 · t)
  pitch  = -3°  + 1°    * sin(2π · 0.6 · t)
  antL   = +20° + 8°    * sin(2π · 0.6 · t)
  antR   = -20° - 8°    * sin(2π · 0.6 · t + π)
  other  = 0
```

Wiggle is opposite-phase between L and R for visual interest.

---

### 3.3 `thinking` (continuous, looping)

Head tilts side-to-side slowly. Period 4 s.

```
sinusoidal generator
  yaw    = 8° * sin(2π · 0.25 · t)
  roll   = 4° * sin(2π · 0.25 · t + π/2)
  z      = 3 mm
  antL   = +15°
  antR   = -15°
  other  = 0
```

Eye-like effect: yaw and roll out of phase produces a head-tilt-while-looking-around look.

---

### 3.4 `speaking` (continuous, looping)

Lively, bouncy. Higher frequency. Will be modulated by the actual speech amplitude in a later iteration — for now, pure oscillator.

```
sinusoidal generator
  z      = 4 mm  * sin(2π · 1.0 · t)
  pitch  = 2°    * sin(2π · 1.0 · t)
  yaw    = 3°    * sin(2π · 0.5 · t)   # slower than vertical for naturalness
  antL   = +30°  + 5° * sin(2π · 0.5 · t)
  antR   = -30°  - 5° * sin(2π · 0.5 · t + π)
  other  = 0
```

---

### 3.5 `happy` (one-shot, ~2.5 s)

Joyful lift, hold, gentle bob, return.

| Phase | Duration | x | y | z | roll | pitch | yaw | antL | antR | body_yaw | Easing |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 1: lift  | 0.30 s | 0 | 0 | +8  | 0 | +12 | 0 | +45 | -45 | 0 | CARTOON |
| 2: hold  | 0.40 s | 0 | 0 | +8  | 0 | +12 | 0 | +45 | -45 | 0 | LINEAR |
| 3: bob1  | 0.25 s | 0 | 0 | +3  | 0 | +8  | 0 | +35 | -35 | 0 | MIN_JERK |
| 4: bob2  | 0.25 s | 0 | 0 | +8  | 0 | +12 | 0 | +45 | -45 | 0 | MIN_JERK |
| 5: bob3  | 0.25 s | 0 | 0 | +3  | 0 | +8  | 0 | +35 | -35 | 0 | MIN_JERK |
| 6: home  | 0.50 s | 0 | 0 |  0  | 0 |  0  | 0 |  0  |  0  | 0 | MIN_JERK |

---

### 3.6 `sad` (one-shot, ~3 s)

Head drop, antennas down, slow return.

| Phase | Duration | x | y | z | roll | pitch | yaw | antL | antR | body_yaw | Easing |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 1: drop      | 0.60 s | 0 | 0 | -8 | 0 | -18 | 0 | -25 | +25 | 0 | EASE_IN_OUT |
| 2: hold      | 1.20 s | 0 | 0 | -8 | 0 | -18 | 0 | -25 | +25 | 0 | LINEAR |
| 3: slow-rise | 1.00 s | 0 | 0 |  0 | 0 |  0  | 0 |  0  |  0  | 0 | EASE_IN_OUT |

---

### 3.7 `surprised` (one-shot, ~1.5 s)

Quick recoil + antennas snap up + brief hold + return.

| Phase | Duration | x | y | z | roll | pitch | yaw | antL | antR | body_yaw | Easing |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 1: recoil  | 0.15 s | -3 | 0 | +5 | 0 | +20 | 0 | +75 | -75 | 0 | CARTOON |
| 2: hold    | 0.45 s | -3 | 0 | +5 | 0 | +20 | 0 | +75 | -75 | 0 | LINEAR |
| 3: settle  | 0.30 s |  0 | 0 | +2 | 0 | +5  | 0 | +30 | -30 | 0 | MIN_JERK |
| 4: home    | 0.50 s |  0 | 0 |  0 | 0 |  0  | 0 |  0  |  0  | 0 | MIN_JERK |

Antennas at ±75°: still 15° clear of the safe envelope's ±90° cap.

---

### 3.8 `curious` (one-shot, ~2.5 s)

Head tilts to one side, brief hold, returns.

| Phase | Duration | x | y | z | roll | pitch | yaw | antL | antR | body_yaw | Easing |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 1: tilt   | 0.45 s | 0 | 0 | +3 | +15 | +3 | -20 | +25 | -10 | 0 | EASE_IN_OUT |
| 2: hold   | 1.20 s | 0 | 0 | +3 | +15 | +3 | -20 | +25 | -10 | 0 | LINEAR |
| 3: home   | 0.85 s | 0 | 0 |  0 |  0  | 0  |  0  |  0  |  0  | 0 | MIN_JERK |

Asymmetric antenna values give a "perked one ear" effect.

---

### 3.9 `acknowledge` (one-shot, ~1 s)

Quick single nod. Useful for "command received".

| Phase | Duration | x | y | z | roll | pitch | yaw | antL | antR | body_yaw | Easing |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 1: down  | 0.25 s | 0 | 0 | -3 | 0 | -10 | 0 | 0 | 0 | 0 | MIN_JERK |
| 2: up    | 0.25 s | 0 | 0 | +2 | 0 |  +3 | 0 | 0 | 0 | 0 | MIN_JERK |
| 3: home  | 0.50 s | 0 | 0 |  0 | 0 |   0 | 0 | 0 | 0 | 0 | MIN_JERK |

---

### 3.10 `error` (one-shot, ~2 s)

Slow head shake "no". Yaw oscillates twice.

| Phase | Duration | x | y | z | roll | pitch | yaw | antL | antR | body_yaw | Easing |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 1: left   | 0.30 s | 0 | 0 | 0 | 0 | -3 | -18 | -10 | +10 | 0 | EASE_IN_OUT |
| 2: right  | 0.40 s | 0 | 0 | 0 | 0 | -3 | +18 | +10 | -10 | 0 | EASE_IN_OUT |
| 3: left   | 0.40 s | 0 | 0 | 0 | 0 | -3 | -18 | -10 | +10 | 0 | EASE_IN_OUT |
| 4: right  | 0.40 s | 0 | 0 | 0 | 0 | -3 | +18 | +10 | -10 | 0 | EASE_IN_OUT |
| 5: home   | 0.50 s | 0 | 0 | 0 | 0 |  0 |  0  |  0  |  0  | 0 | MIN_JERK |

Slight negative pitch through the shake gives a more disappointed tone.

---

## 4. HA-event → emotion mapping (proposed)

The current `conversation_animations.json` already maps HA events to emotions; the refactor will keep that mapping table as the operator-facing surface. The base set above is what those mappings can reference. Concrete proposed defaults:

| HA event class | Emotion |
|---|---|
| `wake_word_detected` | `listening` |
| `voice_query_started` | `thinking` (until TTS arrives) |
| `tts_playback_started` | `speaking` |
| `tts_playback_finished` | `idle` |
| `command_accepted` | `acknowledge` |
| `command_failed` / `unintelligible` | `error` |
| `binary_sensor.front_door`=on | `curious` |
| `binary_sensor.alarm`=on | `surprised` |
| `automation.morning_routine` | `happy` |
| `weather.severe_warning` | `sad` |

These are *defaults*. Operators tune them in `conversation_animations.json` for their installation.

---

## 5. YAML emotion definitions

The catalog is defined as **plain YAML files**, one per emotion, under `reachy_mini_home_assistant/emotions/`. Operators can add, edit, or remove emotions by touching only YAML — no Python knowledge required. The `EmotionPlayer` discovers the files at app boot, validates them against the schema below, and dispatches by name.

### 5.1 Directory layout

```
reachy_mini_home_assistant/
  emotions/
    happy.yaml
    sad.yaml
    surprised.yaml
    curious.yaml
    acknowledge.yaml
    error.yaml
    idle.yaml
    listening.yaml
    thinking.yaml
    speaking.yaml
    custom_my_greeting.yaml      ← operator-added, picked up automatically
```

### 5.2 Schema — one-shot emotion

```yaml
# emotions/happy.yaml
name: happy
description: Joyful head lift with antenna flare
type: one_shot
phases:
  - name: lift
    duration_s: 0.30
    easing: cartoon                 # min_jerk | ease_in_out | cartoon | linear
    end:                            # any field omitted = 0
      z_mm: 8
      pitch_deg: 12
      antenna_left_deg: 45
      antenna_right_deg: -45
  - name: hold
    duration_s: 0.40
    easing: linear
    end:
      z_mm: 8
      pitch_deg: 12
      antenna_left_deg: 45
      antenna_right_deg: -45
  - name: bob_down
    duration_s: 0.25
    easing: min_jerk
    end:
      z_mm: 3
      pitch_deg: 8
      antenna_left_deg: 35
      antenna_right_deg: -35
  - name: bob_up
    duration_s: 0.25
    easing: min_jerk
    end:
      z_mm: 8
      pitch_deg: 12
      antenna_left_deg: 45
      antenna_right_deg: -45
  - name: bob_down_2
    duration_s: 0.25
    easing: min_jerk
    end:
      z_mm: 3
      pitch_deg: 8
      antenna_left_deg: 35
      antenna_right_deg: -35
  - name: home
    duration_s: 0.50
    easing: min_jerk
    end: {}                         # all zeros = neutral
```

**Field reference** (all under `phases[].end`):

| Field | Type | Unit | Default |
|---|---|---|---|
| `x_mm`, `y_mm`, `z_mm` | float | mm | 0 |
| `roll_deg`, `pitch_deg`, `yaw_deg` | float | degrees | 0 |
| `antenna_left_deg`, `antenna_right_deg` | float | degrees | 0 |
| `body_yaw_deg` | float | degrees | 0 |

Every phase value is validated against the safe envelope from §1 at app boot; an out-of-envelope file refuses to load with a clear error message rather than risking the hardware.

### 5.3 Schema — continuous (oscillating) emotion

```yaml
# emotions/listening.yaml
name: listening
description: Alert posture with gentle wiggle
type: continuous
loops: true                          # plays until a different emotion is queued
static_offset:                       # bias on top of which oscillators run
  z_mm: 3
  pitch_deg: -3
  antenna_left_deg: 20
  antenna_right_deg: -20
oscillators:
  - field: z_mm                      # which pose field to drive
    amplitude: 1                     # peak deviation from static_offset
    frequency_hz: 0.6
    phase_offset_rad: 0.0
  - field: pitch_deg
    amplitude: 1
    frequency_hz: 0.6
  - field: antenna_left_deg
    amplitude: 8
    frequency_hz: 0.6
  - field: antenna_right_deg
    amplitude: 8
    frequency_hz: 0.6
    phase_offset_rad: 3.14159        # π = anti-phase with antenna_left
```

The instantaneous pose at time `t` is computed as:
```
pose[field] = static_offset[field] + amplitude * sin(2π · frequency_hz · t + phase_offset_rad)
```
for each oscillator entry; fields without an oscillator default to their `static_offset` value (or 0).

### 5.4 Schema — simple variant for `idle`

For very simple continuous emotions, `static_offset` can be omitted:

```yaml
# emotions/idle.yaml
name: idle
description: Slow breathing motion
type: continuous
loops: true
oscillators:
  - field: z_mm
    amplitude: 2
    frequency_hz: 0.25
  - field: pitch_deg
    amplitude: 1
    frequency_hz: 0.25
  - field: antenna_left_deg
    amplitude: 2
    frequency_hz: 0.25
  - field: antenna_right_deg
    amplitude: 2
    frequency_hz: 0.25
```

### 5.5 Loading + hot-reload

The `EmotionPlayer` reads `emotions/*.yaml` at app boot via `motion/emotion_loader.py`. Each file is parsed once, validated, and cached as a Python `Emotion` object (either `OneShotEmotion(phases=[...])` or `ContinuousEmotion(static_offset=..., oscillators=[...])`). Lookups are by `name` field, which must match across files (uniqueness checked at boot).

For development iteration, the loader can be re-run via a `POST /reload-emotions` admin endpoint without restarting the app — useful for tuning a new emotion file in place.

### 5.6 Adding a new emotion (operator workflow)

1. Drop a new YAML file in `reachy_mini_home_assistant/emotions/`, e.g. `morning_greeting.yaml`.
2. Reference the schema sections above for fields.
3. Either restart the app, or POST to `/reload-emotions` for hot-reload.
4. Map the new emotion's name to an HA event in `conversation_animations.json` (existing operator-facing surface).
5. Trigger the HA event and observe in `/tmp/reachy_mini_home_assistant.log`.

No Python code is touched. No app rebuild. No HF download.

---

## 6. Acceptance criteria for the catalog

- [ ] All ten emotions listed above exist as YAML files in `reachy_mini_home_assistant/emotions/`.
- [ ] Every pose value parsed from YAML is within the safe envelope from §1 (antennas ≤ 90°, pitch ≤ 25°, etc.). Out-of-envelope values refuse to load with a clear error.
- [ ] `motion/emotion_loader.py` parses, validates, and caches all emotion YAML files at app boot.
- [ ] One-shot emotions complete within their declared total duration (verified by `Started/Complete` log spread).
- [ ] Continuous emotions stay within their declared amplitude envelope (verified by max-pose-tracking during a 30 s loop each).
- [ ] HA event mappings (§4) live in `conversation_animations.json` and are operator-editable.
- [ ] `POST /reload-emotions` admin endpoint reloads YAML changes without app restart.
- [ ] Operator can drop a new YAML file into `emotions/` and play it via HA without writing Python.
- [ ] The `RecordedMoves` HF library remains accessible as an opt-in path; default play uses the YAML catalog.

---

## 7. Open questions

1. **Should `idle` keep oscillating or settle to the static `idle_rest_pose` after some inactivity timeout?** Reference app's `waiting-idle` is pure oscillator. Today's `idle_rest_pose` is a static target. The YAML schema could grow a `settle_after_s` field (oscillate for N seconds, then hold at `static_offset`).
2. **How fine-grained should the speech-driven `speaking` be?** Current proposal is a pure oscillator. A future schema extension could add a `modulated_by` field (e.g. `modulated_by: tts_audio_level`) — out of scope for v1.
3. **Pollen `RecordedMoves` integration policy.** Keep as opt-in via configuration flag, or drop entirely? Today's evidence suggests the recordings are too aggressive for the safe envelope; the YAML catalog gives us a clean self-hosted baseline.
4. **YAML library choice**: `PyYAML` is already a Reachy ecosystem dependency. We use `yaml.safe_load` to avoid arbitrary-code-execution risk. No new dependency needed.
5. **Validation strictness**: should out-of-envelope values be *rejected* (refuse to load) or *clamped* (load with a warning)? Proposal: reject. Operator mistakes are caught loudly at boot rather than producing surprising clamped motion.
6. **Schema versioning**: each YAML file could carry a `schema_version: 1` field, so we can evolve the format without breaking existing operator-authored files. Worth including from v1.

---

## 8. References

- Reference implementation pattern: `/home/nolte/repos/github/reachy-mini-app/reachy_mini_app/blocks.py` — `groove_bob`, `sway_side`, `proud`, `bow`, `waiting_idle` define exactly the same shape of data this catalog uses.
- Safe-envelope derivation: today's triage session (`fix/emotion-pipeline-stability` commits + `docs/refactor-emotion-pipeline.md` §1).
- HA event surface mapped here: existing `reachy_mini_home_assistant/animations/conversation_animations.json` schema.
