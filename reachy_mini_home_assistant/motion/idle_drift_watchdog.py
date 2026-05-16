"""Idle-pose drift watchdog.

Periodically (1 Hz) compares the actual robot pose with the idle rest
target and triggers a corrective `goto` when drift exceeds threshold.

Background: external `POST /api/move/goto` calls install a persistent
target in the daemon that overrides the app's regular 50 Hz set_target
loop — verified empirically 2026-05-16. After such an external override
the robot stays in the foreign pose indefinitely. This watchdog detects
that drift and issues an own goto, which overwrites the foreign goto's
persistent target.

Design notes:
- Reads actual pose via direct daemon HTTP (no SDK dependency).
- Corrects via direct daemon HTTP `/api/move/goto` (the same mechanism
  that creates drift in the first place — symmetric, robust).
- Skips while emotion / drain / pause events are set, so it never
  conflicts with EmotionPlayer.
- Cooldown after each watchdog goto prevents re-triggering during the
  goto's own settle.
- Thresholds are deliberately generous so Stewart-IK compensation drift
  (pitch +20 → +16, z -55 → -48, x 0 → -11) is NOT classified as drift.
"""

import json
import logging
import math
import urllib.error
import urllib.request
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from .movement_manager import MovementManager

logger = logging.getLogger(__name__)


# Drift detection thresholds. Generous to avoid false positives from
# Stewart-IK compensation and servo quantization. Tuned 2026-05-16
# against the tucked idle pose where IK costs pitch ~3.5° and x ~11 mm.
_DRIFT_ANTENNA_RAD = math.radians(5.0)   # 5° per antenna
_DRIFT_PITCH_RAD = math.radians(15.0)    # 15° head pitch
_DRIFT_Z_M = 0.020                       # 20 mm head z

# Two-phase corrective restore. Empirically (2026-05-16) the Stewart-IK
# can map a wild source pose to a "twisted" intermediate solution when
# asked to go straight to the tucked idle (pitch +9.8° instead of the
# clean +17° we get after an App-Restart). Cure: first goto to a
# canonical NEUTRAL pose (head = identity, antennas = SDK-init at ±10°),
# then goto to the configured idle rest pose. The neutral intermediate
# resets the IK to a deterministic Joint-vector, so the final idle is
# always reached on the "correct" branch of the IK polytope.
_PHASE_A_NEUTRAL_DURATION_S = 1.0
_PHASE_A_NEUTRAL_HEAD = {"x": 0.0, "y": 0.0, "z": 0.0, "roll": 0.0, "pitch": 0.0, "yaw": 0.0}
# SDK INIT_ANTENNAS_JOINT_POSITIONS = [right=-0.1745, left=+0.1745].
# HTTP `antennas` order = [left, right].
_PHASE_A_NEUTRAL_ANTENNAS = [0.1745, -0.1745]

# Phase B is the actual idle restore.
_PHASE_B_IDLE_DURATION_S = 1.5

# Cooldown after Phase B completes, before the watchdog is allowed to
# trigger another two-phase restore. Prevents oscillation on slow
# external drift sources.
_COOLDOWN_S = 2.0

# Daemon endpoint. App runs on the same host as the daemon.
_DAEMON_HOST = "localhost"
_DAEMON_PORT = 8000

# HTTP timeout. The watchdog tick is called from the 50 Hz control loop,
# so each call must be quick. The daemon's read endpoints answer in
# ~30-80 ms in practice; 0.5 s is a comfortable cap.
_HTTP_TIMEOUT_S = 0.5


class IdleDriftWatchdog:
    """Detects pose drift from the idle rest target, restores via two-phase goto.

    State machine:
      "idle"             — watching for drift; on detect → "phase_a"
      "phase_a"          — Phase A neutral goto in flight; on completion → "phase_b"
      "phase_b"          — Phase B idle goto in flight; on completion → "cooldown"
      "cooldown"         — silence period after restore; on completion → "idle"
    """

    def __init__(self) -> None:
        self._state = "idle"
        self._phase_start_time = 0.0

    def tick(self, manager: "MovementManager", now: float) -> None:
        """Called by the control loop at ~1 Hz."""

        # Advance the state machine first — phase transitions are time-based
        # and must happen even when emotion/drain/pause is set (so we don't
        # get stuck mid-restore if EmotionPlayer kicks in).
        if self._state == "phase_a":
            if now - self._phase_start_time >= _PHASE_A_NEUTRAL_DURATION_S:
                # Phase A done; kick off Phase B.
                if self._send_idle_goto(manager):
                    logger.info("Drift watchdog Phase B: goto idle-rest")
                    self._state = "phase_b"
                    self._phase_start_time = now
                else:
                    # Phase B send failed; abort to cooldown (will retry later).
                    self._state = "cooldown"
                    self._phase_start_time = now
            return

        if self._state == "phase_b":
            if now - self._phase_start_time >= _PHASE_B_IDLE_DURATION_S:
                self._state = "cooldown"
                self._phase_start_time = now
            return

        if self._state == "cooldown":
            if now - self._phase_start_time >= _COOLDOWN_S:
                self._state = "idle"
            return

        # state == "idle" — actually scan for drift below.

        # Skip while emotion or drain or pause active — never conflict
        # with EmotionPlayer, shutdown, or operator pause.
        if manager._emotion_playing_event.is_set():
            return
        if manager._draining_event.is_set():
            return
        if manager._robot_paused_event.is_set():
            return

        # Read actual pose. Skip silently on read failure (daemon hiccup,
        # transient HTTP error). The watchdog will retry on the next tick.
        actual = self._read_actual_pose()
        if actual is None:
            return

        # Compare against the idle rest targets stored in MovementManager.
        target_ant_l = manager._idle_rest_antenna_left_rad
        target_ant_r = manager._idle_rest_antenna_right_rad
        target_pitch_rad = manager._idle_rest_head_pitch_rad
        target_z_m = manager._idle_rest_z_m

        # NB: the daemon HTTP API returns antennas as [left, right], so
        # actual["ant_servo_left"] corresponds to app-world
        # `_idle_rest_antenna_right_rad` (mirror — see memory
        # `pollen_daemon_antenna_http_order`).
        ant_l_drift = abs(actual["ant_servo_left"] - target_ant_r) > _DRIFT_ANTENNA_RAD
        ant_r_drift = abs(actual["ant_servo_right"] - target_ant_l) > _DRIFT_ANTENNA_RAD
        pitch_drift = abs(actual["pitch"] - target_pitch_rad) > _DRIFT_PITCH_RAD
        z_drift = abs(actual["z"] - target_z_m) > _DRIFT_Z_M

        if not (ant_l_drift or ant_r_drift or pitch_drift or z_drift):
            return  # within tolerance

        logger.warning(
            "Idle drift detected (servo_L=%.1f° vs %.1f°, servo_R=%.1f° vs %.1f°, "
            "pitch=%.1f° vs %.1f°, z=%.1f mm vs %.1f mm) — starting two-phase restore",
            math.degrees(actual["ant_servo_left"]), math.degrees(target_ant_r),
            math.degrees(actual["ant_servo_right"]), math.degrees(target_ant_l),
            math.degrees(actual["pitch"]), math.degrees(target_pitch_rad),
            actual["z"] * 1000.0, target_z_m * 1000.0,
        )

        # Kick off Phase A — goto to canonical NEUTRAL.
        if self._send_neutral_goto():
            logger.info("Drift watchdog Phase A: goto neutral")
            self._state = "phase_a"
            self._phase_start_time = now
        # If send fails, stay in "idle" and retry on next tick (no state change).

    def _read_actual_pose(self) -> Optional[dict]:
        """Read current pose via daemon HTTP. Returns None on failure."""
        try:
            ant = json.loads(
                urllib.request.urlopen(
                    f"http://{_DAEMON_HOST}:{_DAEMON_PORT}/api/state/present_antenna_joint_positions",
                    timeout=_HTTP_TIMEOUT_S,
                ).read()
            )
            head = json.loads(
                urllib.request.urlopen(
                    f"http://{_DAEMON_HOST}:{_DAEMON_PORT}/api/state/present_head_pose",
                    timeout=_HTTP_TIMEOUT_S,
                ).read()
            )
            return {
                "ant_servo_left": float(ant[0]),
                "ant_servo_right": float(ant[1]),
                "pitch": float(head["pitch"]),
                "z": float(head["z"]),
            }
        except (urllib.error.URLError, TimeoutError, ValueError, KeyError) as e:
            logger.debug("Watchdog actual-pose read failed: %s", e)
            return None

    def _send_neutral_goto(self) -> bool:
        """Phase A: send goto to canonical NEUTRAL pose (deterministic IK anchor)."""
        return self._send_goto_http(
            head_pose=_PHASE_A_NEUTRAL_HEAD,
            antennas=list(_PHASE_A_NEUTRAL_ANTENNAS),
            duration=_PHASE_A_NEUTRAL_DURATION_S,
        )

    def _send_idle_goto(self, manager: "MovementManager") -> bool:
        """Phase B: send corrective goto to idle rest pose."""
        return self._send_goto_http(
            head_pose={
                "x": manager._idle_rest_x_m,
                "y": manager._idle_rest_y_m,
                "z": manager._idle_rest_z_m,
                "roll": manager._idle_rest_head_roll_rad,
                "pitch": manager._idle_rest_head_pitch_rad,
                "yaw": manager._idle_rest_head_yaw_rad,
            },
            # HTTP API expects [left, right]; app-world _idle_rest_antenna_right_rad
            # drives mechanical Servo-left, and _idle_rest_antenna_left_rad drives
            # Servo-right — see memory `pollen_daemon_antenna_http_order`.
            antennas=[
                manager._idle_rest_antenna_right_rad,
                manager._idle_rest_antenna_left_rad,
            ],
            duration=_PHASE_B_IDLE_DURATION_S,
        )

    def _send_goto_http(self, *, head_pose: dict, antennas: list, duration: float) -> bool:
        """Send a `POST /api/move/goto` via daemon HTTP, non-blocking trajectory."""
        try:
            body = json.dumps({
                "head_pose": head_pose,
                "antennas": antennas,
                "body_yaw": 0.0,
                "duration": duration,
                "interpolation": "minjerk",
            }).encode("utf-8")
            req = urllib.request.Request(
                f"http://{_DAEMON_HOST}:{_DAEMON_PORT}/api/move/goto",
                data=body,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT_S).read()
            return True
        except (urllib.error.URLError, TimeoutError) as e:
            logger.warning("Watchdog goto send failed: %s", e)
            return False
