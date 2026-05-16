"""EmotionPlayer — runs each emotion in its own worker thread.

Implements the Phase-1 contract from docs/refactor-emotion-pipeline-design.md:
adopt the reference-app pattern (one worker thread per move, component-wise
lerp, direct reachy.set_target, 50 Hz tick) and keep emotions out of the
MovementManager control loop entirely.

This module is intentionally self-contained — it does NOT yet wire into
the existing MovementManager. Phase 2 of the refactor adds the pause /
resume handshake. For Phase 1 it can be exercised in isolation with a
plain ReachyMini instance.

Invariants:
- At most one worker thread alive at any time (enforced by self._lock).
- The worker thread is the only writer to reachy.set_target() while alive.
- A new play(name) cancels the current emotion (cancel-and-replace) and
  spawns a new worker after the cancellation has eased out.
- Worker is daemon=True so process shutdown is never blocked.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from collections.abc import Callable

from reachy_mini.utils.interpolation import time_trajectory

from .emotion_loader import ContinuousEmotion, Emotion, OneShotEmotion, sample_continuous
from .emotion_pose import (
    NEUTRAL,
    EmotionPose,
    Phase,
    clamp_to_envelope,
    from_present,
    lerp_pose,
    to_set_target,
)

logger = logging.getLogger(__name__)


# -----------------------------------------------------------------------------
# Defaults — see docs/refactor-emotion-pipeline-design.md §10 for rationale.
# -----------------------------------------------------------------------------

PRE_NEUTRAL_S = 0.6  # deterministic IK anchor: any-pose → NEUTRAL before lead-in
LEAD_IN_S = 0.5  # smooth pre-emotion → first animation pose
EASE_OUT_S = 0.5  # smooth animation tail → rest pose
TICK_S = 0.02  # 50 Hz, same as reference app
PLAYBACK_SPEED = 0.5  # 0.5× = half speed; operator-confirmed default

# Rate-limit / change-detect: don't push set_target faster than the
# hardware can actually move. The Pollen WSClient pipeline silently
# buffers commands when the daemon queue saturates (see today's
# investigation in PR #4's review). Skipping no-op ticks reduces the
# load enough that the saturation pattern stops triggering.
SET_TARGET_DELTA_M = 0.0005  # 0.5 mm — below encoder noise
SET_TARGET_DELTA_RAD = math.radians(0.5)  # 0.5° — below encoder noise
SET_TARGET_HEARTBEAT_S = 0.2  # send at least once every 200 ms
# (5 Hz floor; keeps SDK side
# convinced the connection is live)


class EmotionPlayer:
    """Plays YAML-defined emotions via per-move worker threads.

    Public API:
      - play(name): cancel-and-replace; spawns a worker thread
      - cancel(): signal current worker to ease out and exit
      - is_playing(): True if a worker thread is alive
      - shutdown(): cancel + join (called at app shutdown)

    The constructor accepts an optional callable for the rest pose so that
    operator preference (currently tracked in idle_rest_pose JSON) can be
    re-evaluated on every ease-out without hardcoding.
    """

    def __init__(
        self,
        reachy,
        emotions: dict[str, Emotion],
        rest_pose_provider: Callable[[], EmotionPose] | None = None,
        on_pause: Callable[[], None] | None = None,
        on_resume: Callable[[], None] | None = None,
    ) -> None:
        """
        Args:
            reachy: a ReachyMini instance (the worker thread is the only
                writer to reachy.set_target() while it lives).
            emotions: name → Emotion map, typically from emotion_loader.load_emotions.
            rest_pose_provider: callable returning the EmotionPose to ease
                out to on completion. Defaults to NEUTRAL.
            on_pause / on_resume: optional hooks to coordinate with another
                motion source (the MovementManager in Phase 2 of the refactor).
                Called once at the start / end of every worker lifecycle.
        """
        self._reachy = reachy
        self._emotions = emotions
        self._rest_pose_provider = rest_pose_provider or (lambda: NEUTRAL)
        self._on_pause = on_pause
        self._on_resume = on_resume

        self._lock = threading.Lock()
        self._worker: threading.Thread | None = None
        self._cancel_event = threading.Event()
        self._current_emotion_name: str | None = None

        # Rate-limit state. Worker resets these on entry so each emotion
        # starts with a fresh delta accounting.
        self._last_sent_pose: EmotionPose | None = None
        self._last_sent_ts: float = 0.0
        self._skipped_ticks_in_a_row: int = 0
        # Defense-in-depth clamp state. We log the first clamp per worker
        # at WARNING (so it lands in production logs) and downgrade
        # subsequent clamps to DEBUG to avoid log flooding under a stuck
        # out-of-envelope condition.
        self._clamp_warned_this_worker: bool = False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def play(self, emotion_name: str) -> bool:
        """Trigger a new emotion. Cancel-and-replace if one is in flight.

        Returns True if scheduled, False if the emotion name is unknown.
        """
        if emotion_name not in self._emotions:
            logger.warning(
                "Unknown emotion %r — known: %s",
                emotion_name,
                sorted(self._emotions.keys()),
            )
            return False

        with self._lock:
            # Cancel-and-replace: if a worker is running, signal it to ease
            # out and wait briefly for it to leave.
            if self._worker is not None and self._worker.is_alive():
                self._cancel_event.set()
                self._worker.join(timeout=PRE_NEUTRAL_S + LEAD_IN_S + EASE_OUT_S + 0.5)
                if self._worker.is_alive():
                    logger.warning(
                        "Previous emotion %r did not join in time; spawning new worker anyway",
                        self._current_emotion_name,
                    )
            self._cancel_event.clear()
            self._current_emotion_name = emotion_name
            self._worker = threading.Thread(
                target=self._worker_main,
                args=(emotion_name,),
                name=f"EmotionPlayer-{emotion_name}",
                daemon=True,
            )
            self._worker.start()
        return True

    def cancel(self) -> None:
        """Signal the current worker to ease out and exit (no-op if idle)."""
        with self._lock:
            if self._worker is not None and self._worker.is_alive():
                self._cancel_event.set()

    def is_playing(self) -> bool:
        """True if a worker thread is currently alive."""
        with self._lock:
            return self._worker is not None and self._worker.is_alive()

    def shutdown(self, timeout: float = 5.0) -> None:
        """Cancel current emotion and wait for the worker to exit. App shutdown."""
        with self._lock:
            worker = self._worker
            if worker is None or not worker.is_alive():
                return
            self._cancel_event.set()
        worker.join(timeout=timeout)
        if worker.is_alive():
            logger.warning("EmotionPlayer shutdown timed out; worker still alive")

    # ------------------------------------------------------------------
    # Worker thread
    # ------------------------------------------------------------------

    def _worker_main(self, emotion_name: str) -> None:
        """Owns reachy.set_target for the duration of this emotion."""
        emotion = self._emotions[emotion_name]
        # Reset rate-limit state so the first tick of each new emotion
        # always sends (no stale last_sent_pose from a previous emotion
        # that might be within delta of the new lead-in start).
        self._last_sent_pose = None
        self._last_sent_ts = 0.0
        self._skipped_ticks_in_a_row = 0
        self._clamp_warned_this_worker = False
        logger.info("Started emotion %r", emotion_name)
        try:
            if self._on_pause is not None:
                self._on_pause()
            try:
                self._reachy.set_automatic_body_yaw(False)
            except Exception as e:
                logger.debug("Could not disable automatic_body_yaw: %s", e)

            if isinstance(emotion, OneShotEmotion):
                self._run_one_shot(emotion)
            elif isinstance(emotion, ContinuousEmotion):
                self._run_continuous(emotion)
            else:
                logger.error("Unknown emotion type for %r: %s", emotion_name, type(emotion))
        except Exception as e:
            logger.exception("Emotion %r failed in worker: %s", emotion_name, e)
        finally:
            # Always restore SDK state and notify the resume hook, even on
            # exception or cancellation — leaving auto-yaw disabled or the
            # MovementManager paused would break the rest of the app.
            try:
                self._reachy.set_automatic_body_yaw(True)
            except Exception as e:
                logger.debug("Could not re-enable automatic_body_yaw: %s", e)
            if self._on_resume is not None:
                try:
                    self._on_resume()
                except Exception as e:
                    logger.warning("on_resume hook raised: %s", e)
            logger.info("Emotion %r complete", emotion_name)

    # ------------------------------------------------------------------
    # One-shot playback (lead-in → phases → ease-out)
    # ------------------------------------------------------------------

    def _run_one_shot(self, emotion: OneShotEmotion) -> None:
        # Phase A: read the real current pose so the pre-neutral anchor
        # starts from where the hardware actually is.
        start_pose = self._read_present_pose_safe()
        first_phase_end = emotion.phases[0].end

        # Phase A.5: PRE-NEUTRAL ANCHOR. Without this, the Stewart-IK can
        # land on a "twisted" branch when an emotion launches from an
        # extreme idle pose (e.g. tucked Servo[-180°, -180°] head fully
        # down). Routing the emotion start through NEUTRAL = identity head
        # + zero antennas resets the IK to a deterministic Joint vector,
        # so the actual first phase is reached on the correct IK branch.
        # The brief sweep through antenna 0° (≤ 0.6 s during motion, never
        # stationary) is within the empirical tolerance for the deadband.
        if not self._cancel_event.is_set():
            self._run_lerp(start_pose, NEUTRAL, PRE_NEUTRAL_S, time_trajectory_for_easing=None)
            start_pose = NEUTRAL  # subsequent lead-in starts from NEUTRAL

        # Phase B: lead-in (from NEUTRAL to the first phase target).
        if not self._cancel_event.is_set():
            self._run_lerp(start_pose, first_phase_end, LEAD_IN_S, time_trajectory_for_easing=None)

        # Phase C: the declared phases in sequence. Each phase's start is
        # the previous phase's end (or first_phase_end for phase 0, which
        # we just reached via the lead-in).
        current = first_phase_end
        for phase in emotion.phases:
            if self._cancel_event.is_set():
                break
            self._run_phase(current, phase)
            current = phase.end

        # Phase D: ease out to the operator-defined rest pose.
        rest = self._rest_pose_provider()
        self._run_lerp(current, rest, EASE_OUT_S, time_trajectory_for_easing=None)

    def _run_phase(self, start: EmotionPose, phase: Phase) -> None:
        """Play one Phase: lerp from start → phase.end with phase.easing."""
        if phase.duration_s <= 0:
            self._apply(phase.end)
            return
        t0 = time.monotonic()
        while not self._cancel_event.is_set():
            elapsed = time.monotonic() - t0
            if elapsed >= phase.duration_s:
                break
            s = time_trajectory(min(1.0, elapsed / phase.duration_s), phase.easing)
            self._apply(lerp_pose(start, phase.end, s))
            time.sleep(TICK_S)
        if not self._cancel_event.is_set():
            self._apply(phase.end)

    def _run_lerp(
        self,
        start: EmotionPose,
        end: EmotionPose,
        duration_s: float,
        time_trajectory_for_easing=None,
    ) -> None:
        """Simple linear lerp from start → end over duration_s (used for
        lead-in and ease-out; no easing curve)."""
        if duration_s <= 0:
            self._apply(end)
            return
        t0 = time.monotonic()
        while not self._cancel_event.is_set():
            elapsed = time.monotonic() - t0
            if elapsed >= duration_s:
                break
            s = min(1.0, elapsed / duration_s)
            self._apply(lerp_pose(start, end, s))
            time.sleep(TICK_S)
        if not self._cancel_event.is_set():
            self._apply(end)

    # ------------------------------------------------------------------
    # Continuous playback (lead-in → oscillator loop → ease-out on cancel)
    # ------------------------------------------------------------------

    def _run_continuous(self, emotion: ContinuousEmotion) -> None:
        # Lead-in: real present pose → t=0 sample of the emotion.
        start_pose = self._read_present_pose_safe()
        first_sample = sample_continuous(emotion, 0.0)
        # Pre-neutral anchor (see _run_one_shot for rationale): route the
        # start through NEUTRAL so the Stewart-IK lands on a deterministic
        # branch before the oscillator loop begins.
        if not self._cancel_event.is_set():
            self._run_lerp(start_pose, NEUTRAL, PRE_NEUTRAL_S)
            start_pose = NEUTRAL
        if not self._cancel_event.is_set():
            self._run_lerp(start_pose, first_sample, LEAD_IN_S)

        # Oscillator loop until cancel.
        t0 = time.monotonic()
        while not self._cancel_event.is_set():
            t = (time.monotonic() - t0) * PLAYBACK_SPEED
            self._apply(sample_continuous(emotion, t))
            time.sleep(TICK_S)

        # Ease-out from wherever we are to the rest pose.
        last_pose = self._read_present_pose_safe()
        rest = self._rest_pose_provider()
        # cancel_event is set; clear it for the ease-out so we actually run
        # to completion instead of bailing immediately
        was_cancelled = self._cancel_event.is_set()
        if was_cancelled:
            self._cancel_event.clear()
        self._run_lerp(last_pose, rest, EASE_OUT_S)
        # Re-arm cancellation so the caller doesn't see a "still running"
        # state after we exit.
        if was_cancelled:
            self._cancel_event.set()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _apply(self, pose: EmotionPose) -> None:
        """Push one pose to the SDK, but rate-limit no-op updates.

        Skips the set_target call when the new pose is within the
        encoder-noise threshold of the last sent pose, unless the
        last send was more than SET_TARGET_HEARTBEAT_S ago.

        This is the workaround for the Pollen WSClient saturation
        bug: at 50 Hz blind sending, the daemon's command queue
        eventually saturates and silently drops follow-up commands.
        By only sending when the pose actually moved, we let the
        daemon catch up and avoid the silent stall.

        Defense-in-depth: every pose is run through clamp_to_envelope
        before set_target. The loader already rejects YAML that could
        leave the envelope, so a clamp here means either a malformed
        present-pose read or a future bug. We WARN once per worker so
        operators see it, then DEBUG-log subsequent clamps to avoid
        flooding when the condition is persistent.
        """
        clamped, did_clamp = clamp_to_envelope(pose)
        if did_clamp:
            if not self._clamp_warned_this_worker:
                logger.warning(
                    "EmotionPlayer: pose clamped to safe envelope "
                    "(this should not happen — loader is supposed to "
                    "catch out-of-envelope values); original=%r clamped=%r",
                    pose,
                    clamped,
                )
                self._clamp_warned_this_worker = True
            else:
                logger.debug(
                    "EmotionPlayer: pose clamped again; original=%r clamped=%r",
                    pose,
                    clamped,
                )

        if self._pose_unchanged(clamped):
            self._skipped_ticks_in_a_row += 1
            return

        if self._skipped_ticks_in_a_row > 0:
            logger.debug(
                "EmotionPlayer: skipped %d static ticks before next set_target",
                self._skipped_ticks_in_a_row,
            )
            self._skipped_ticks_in_a_row = 0

        head, antennas, body_yaw = to_set_target(clamped)
        self._reachy.set_target(head=head, antennas=antennas, body_yaw=body_yaw)
        self._last_sent_pose = clamped
        self._last_sent_ts = time.monotonic()

    def _pose_unchanged(self, pose: EmotionPose) -> bool:
        """True iff `pose` is within delta thresholds of the last sent pose
        AND the heartbeat-interval has not yet elapsed since the last send."""
        if self._last_sent_pose is None:
            return False
        if time.monotonic() - self._last_sent_ts >= SET_TARGET_HEARTBEAT_S:
            return False
        prev = self._last_sent_pose
        # Translation deltas (metres)
        if abs(pose.x - prev.x) > SET_TARGET_DELTA_M:
            return False
        if abs(pose.y - prev.y) > SET_TARGET_DELTA_M:
            return False
        if abs(pose.z - prev.z) > SET_TARGET_DELTA_M:
            return False
        # Angular deltas (radians)
        if abs(pose.roll - prev.roll) > SET_TARGET_DELTA_RAD:
            return False
        if abs(pose.pitch - prev.pitch) > SET_TARGET_DELTA_RAD:
            return False
        if abs(pose.yaw - prev.yaw) > SET_TARGET_DELTA_RAD:
            return False
        if abs(pose.antenna_left - prev.antenna_left) > SET_TARGET_DELTA_RAD:
            return False
        if abs(pose.antenna_right - prev.antenna_right) > SET_TARGET_DELTA_RAD:
            return False
        if abs(pose.body_yaw - prev.body_yaw) > SET_TARGET_DELTA_RAD:
            return False
        return True

    def _read_present_pose_safe(self) -> EmotionPose:
        """Read the real current pose from the SDK; falls back to NEUTRAL
        if the SDK call fails OR if the read value is outside the safe
        envelope (which means the HW is in sleep / disabled and is sitting
        at a pose like pitch≈27°, antenna≈±175°, z≈-46mm that we never want
        to use as the starting point of a Lerp).

        Using such a pose as the Lerp start would produce out-of-envelope
        intermediate samples for the entire Lead-In, which the runtime clamp
        in `_apply` would then quench into a hard mechanical jump. Falling
        back to NEUTRAL keeps every Lerp tick inside the envelope by
        construction.
        """
        try:
            head_mat = self._reachy.get_current_head_pose()
            antennas = self._reachy.get_present_antenna_joint_positions()
            present = from_present(head_mat, antennas, body_yaw=0.0)
        except Exception as e:
            logger.warning("Could not read present pose; falling back to NEUTRAL: %s", e)
            return NEUTRAL
        _, out_of_envelope = clamp_to_envelope(present)
        if out_of_envelope:
            logger.warning(
                "Present pose outside safe envelope (HW likely in sleep/disabled); "
                "falling back to NEUTRAL as Lerp start to avoid a clamped jump. raw=%r",
                present,
            )
            return NEUTRAL
        return present
