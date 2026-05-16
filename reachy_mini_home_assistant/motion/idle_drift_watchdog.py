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

# Cooldown between watchdog-issued gotos.
_COOLDOWN_S = 3.0

# Corrective-goto duration. 1.5 s is fast enough that a visible drift
# is gone within ~2 s, slow enough to avoid Dynamixel Overload at extreme
# magnitudes (see emotion_pipeline_5_bug_story memory, Bug 5).
_GOTO_DURATION_S = 1.5

# Daemon endpoint. App runs on the same host as the daemon.
_DAEMON_HOST = "localhost"
_DAEMON_PORT = 8000

# HTTP timeout. The watchdog tick is called from the 50 Hz control loop,
# so each call must be quick. The daemon's read endpoints answer in
# ~30-80 ms in practice; 0.5 s is a comfortable cap.
_HTTP_TIMEOUT_S = 0.5


class IdleDriftWatchdog:
    """Detects pose drift from the idle rest target, restores via goto."""

    def __init__(self) -> None:
        self._last_goto_time = 0.0

    def tick(self, manager: "MovementManager", now: float) -> None:
        """Called by the control loop at ~1 Hz.

        Returns immediately if any "non-idle" state is active or if the
        cooldown after a previous watchdog goto has not elapsed.
        """
        # Skip while emotion or drain or pause active — never conflict
        # with EmotionPlayer, shutdown, or operator pause.
        if manager._emotion_playing_event.is_set():
            return
        if manager._draining_event.is_set():
            return
        if manager._robot_paused_event.is_set():
            return

        # Cooldown gate.
        if now - self._last_goto_time < _COOLDOWN_S:
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
            "pitch=%.1f° vs %.1f°, z=%.1f mm vs %.1f mm) — restoring via goto",
            math.degrees(actual["ant_servo_left"]), math.degrees(target_ant_r),
            math.degrees(actual["ant_servo_right"]), math.degrees(target_ant_l),
            math.degrees(actual["pitch"]), math.degrees(target_pitch_rad),
            actual["z"] * 1000.0, target_z_m * 1000.0,
        )

        if self._send_idle_goto(manager):
            self._last_goto_time = now

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

    def _send_idle_goto(self, manager: "MovementManager") -> bool:
        """Send corrective goto to idle rest pose via daemon HTTP."""
        try:
            body = json.dumps({
                "head_pose": {
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
                "antennas": [
                    manager._idle_rest_antenna_right_rad,
                    manager._idle_rest_antenna_left_rad,
                ],
                "body_yaw": 0.0,
                "duration": _GOTO_DURATION_S,
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
