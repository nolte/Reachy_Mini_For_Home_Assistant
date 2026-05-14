"""Smoke-test the EmotionPlayer on a real Reachy Mini.

Runs three risk-emotions sequentially and asserts that the safety chain
holds during live playback:
  - `surprised`  — one-shot with the tightest pitch margin (20° vs 25° limit)
  - `listening`  — continuous with antenna excursion up to ±28°
  - `speaking`   — continuous with antenna excursion up to ±35°

The script wraps `EmotionPlayer._apply` so every set_target call is
captured with its EmotionPose. After each emotion the captured poses
are re-validated against `MAX_*` to catch any silent envelope drift
that the runtime clamp would otherwise mask (the clamp would still
fire and log WARNING, but this script makes the contract explicit).

Exit code 0 = PASS, 1 = FAIL. Designed to run on the Reachy Mini Pi
inside the Pollen daemon's Python environment.
"""

from __future__ import annotations

import json
import logging
import math
import os
import sys
import time
import urllib.request
from pathlib import Path

import numpy as np
from reachy_mini.utils import create_head_pose

from reachy_mini_home_assistant.motion.emotion_loader import load_emotions
from reachy_mini_home_assistant.motion.emotion_player import EmotionPlayer
from reachy_mini_home_assistant.motion.emotion_pose import (
    MAX_ANTENNA_RAD,
    MAX_BODY_YAW_RAD,
    MAX_PITCH_RAD,
    MAX_ROLL_RAD,
    MAX_TRANSLATION_M,
    MAX_YAW_RAD,
    EmotionPose,
)

# ---------------------------------------------------------------------------
# HTTP adapter that duck-types the ReachyMini methods EmotionPlayer relies on.
#
# The SDK's ReachyMini() constructor mandates a working WebRTC producer on
# localhost, which is unavailable when the daemon's backend hasn't been
# fully brought up. The daemon's HTTP API at :8000 exposes every motor
# read/write we actually need, so the smoke test talks to the daemon
# directly. This is test-only — production EmotionPlayer still uses the
# real SDK in the HA app.
# ---------------------------------------------------------------------------


class HttpReachyAdapter:
    """Minimal ReachyMini stand-in that talks to the Pollen daemon HTTP API."""

    def __init__(self, base_url: str = "http://127.0.0.1:8000") -> None:
        self._base = base_url.rstrip("/")

    def set_target(self, *, head=None, antennas=None, body_yaw=None) -> None:
        body: dict = {}
        if head is not None:
            arr = np.asarray(head, dtype=np.float64).reshape(-1)
            if arr.size != 16:
                raise ValueError(f"head must be a 4x4 matrix, got size {arr.size}")
            body["target_head_pose"] = {"m": [float(v) for v in arr]}
        if antennas is not None:
            body["target_antennas"] = [float(antennas[0]), float(antennas[1])]
        if body_yaw is not None:
            body["target_body_yaw"] = float(body_yaw)
        self._post("/api/move/set_target", body)

    def set_automatic_body_yaw(self, _enabled: bool) -> None:
        # Daemon has no HTTP toggle for this; EmotionPlayer toggles it as a
        # courtesy at worker start but the smoke test doesn't depend on it.
        return

    def get_current_head_pose(self) -> np.ndarray:
        data = self._get("/api/state/present_head_pose")
        if isinstance(data, dict) and "m" in data:
            return np.asarray(data["m"], dtype=np.float64).reshape(4, 4)
        return create_head_pose(
            x=data["x"],
            y=data["y"],
            z=data["z"],
            roll=data["roll"],
            pitch=data["pitch"],
            yaw=data["yaw"],
            mm=False,
            degrees=False,
        )

    def get_present_antenna_joint_positions(self) -> np.ndarray:
        arr = self._get("/api/state/present_antenna_joint_positions")
        return np.asarray(arr, dtype=np.float64)

    def _post(self, path: str, body: dict) -> dict:
        req = urllib.request.Request(
            self._base + path,
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=2.0) as r:
            raw = r.read()
            return json.loads(raw) if raw else {}

    def _get(self, path: str):
        with urllib.request.urlopen(self._base + path, timeout=2.0) as r:
            return json.loads(r.read())


RISK_EMOTIONS = [
    ("surprised", 4.0, False),  # one-shot, ~1.4s total, generous buffer
    ("listening", 6.0, True),  # continuous, cancel after 6s
    ("speaking", 6.0, True),  # continuous, cancel after 6s
]


def pose_in_envelope(pose: EmotionPose) -> tuple[bool, list[str]]:
    """Return (ok, list-of-violations). Empty list iff every axis is safe."""
    v: list[str] = []
    if abs(pose.x) > MAX_TRANSLATION_M:
        v.append(f"x={pose.x * 1000:.2f}mm > {MAX_TRANSLATION_M * 1000:.0f}mm")
    if abs(pose.y) > MAX_TRANSLATION_M:
        v.append(f"y={pose.y * 1000:.2f}mm > {MAX_TRANSLATION_M * 1000:.0f}mm")
    if abs(pose.z) > MAX_TRANSLATION_M:
        v.append(f"z={pose.z * 1000:.2f}mm > {MAX_TRANSLATION_M * 1000:.0f}mm")
    if abs(pose.pitch) > MAX_PITCH_RAD:
        v.append(f"pitch={math.degrees(pose.pitch):.1f}° > {math.degrees(MAX_PITCH_RAD):.0f}°")
    if abs(pose.yaw) > MAX_YAW_RAD:
        v.append(f"yaw={math.degrees(pose.yaw):.1f}° > {math.degrees(MAX_YAW_RAD):.0f}°")
    if abs(pose.roll) > MAX_ROLL_RAD:
        v.append(f"roll={math.degrees(pose.roll):.1f}° > {math.degrees(MAX_ROLL_RAD):.0f}°")
    if abs(pose.antenna_left) > MAX_ANTENNA_RAD:
        v.append(f"antL={math.degrees(pose.antenna_left):.1f}° > {math.degrees(MAX_ANTENNA_RAD):.0f}°")
    if abs(pose.antenna_right) > MAX_ANTENNA_RAD:
        v.append(f"antR={math.degrees(pose.antenna_right):.1f}° > {math.degrees(MAX_ANTENNA_RAD):.0f}°")
    if abs(pose.body_yaw) > MAX_BODY_YAW_RAD:
        v.append(f"body_yaw={math.degrees(pose.body_yaw):.1f}° > {math.degrees(MAX_BODY_YAW_RAD):.0f}°")
    return (not v, v)


class WarnCounter(logging.Handler):
    """Counts WARNING-level records from the EmotionPlayer logger."""

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        if record.levelno >= logging.WARNING:
            self.records.append(record)


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    warn_counter = WarnCounter()
    logging.getLogger("reachy_mini_home_assistant.motion.emotion_player").addHandler(warn_counter)

    here = Path(__file__).resolve().parent.parent
    emotions_dir = here / "reachy_mini_home_assistant" / "emotions"
    emotions = load_emotions(emotions_dir)
    print(f"Loaded {len(emotions)} emotions from {emotions_dir}")

    daemon_url = os.environ.get("REACHY_DAEMON_URL", "http://127.0.0.1:8000")
    print(f"Connecting to Reachy daemon at {daemon_url}...")
    reachy = HttpReachyAdapter(base_url=daemon_url)
    # Probe one read so we fail fast if the daemon is not actually serving.
    _ = reachy.get_current_head_pose()
    _ = reachy.get_present_antenna_joint_positions()
    print("Daemon HTTP API reachable; both state reads succeeded.")

    player = EmotionPlayer(reachy=reachy, emotions=emotions)

    # Hook into _apply to record every pose sent to set_target.
    captured: list[tuple[str, EmotionPose]] = []
    original_apply = player._apply
    current_emotion = {"name": ""}

    def recording_apply(pose: EmotionPose) -> None:
        captured.append((current_emotion["name"], pose))
        original_apply(pose)

    player._apply = recording_apply  # type: ignore[method-assign]

    overall_violations: list[str] = []

    try:
        for name, run_for_s, is_continuous in RISK_EMOTIONS:
            print(f"\n=== Playing {name!r} for {run_for_s}s ===")
            current_emotion["name"] = name
            before_count = len(captured)
            ok = player.play(name)
            if not ok:
                overall_violations.append(f"{name}: play() returned False (unknown emotion?)")
                continue
            time.sleep(run_for_s)
            if is_continuous:
                player.cancel()
                # Wait for ease-out (LEAD_IN_S + EASE_OUT_S = 1.0s + buffer)
                time.sleep(1.5)
            else:
                # One-shot: wait for natural completion if still alive
                while player.is_playing():
                    time.sleep(0.1)

            after_count = len(captured)
            n_calls = after_count - before_count
            phase_violations: list[str] = []
            worst_per_axis: dict[str, float] = {}
            for _, pose in captured[before_count:after_count]:
                ok_envelope, vs = pose_in_envelope(pose)
                if not ok_envelope:
                    phase_violations.extend(vs)
                worst_per_axis["pitch_deg"] = max(worst_per_axis.get("pitch_deg", 0.0), abs(math.degrees(pose.pitch)))
                worst_per_axis["yaw_deg"] = max(worst_per_axis.get("yaw_deg", 0.0), abs(math.degrees(pose.yaw)))
                worst_per_axis["roll_deg"] = max(worst_per_axis.get("roll_deg", 0.0), abs(math.degrees(pose.roll)))
                worst_per_axis["antL_deg"] = max(
                    worst_per_axis.get("antL_deg", 0.0), abs(math.degrees(pose.antenna_left))
                )
                worst_per_axis["antR_deg"] = max(
                    worst_per_axis.get("antR_deg", 0.0), abs(math.degrees(pose.antenna_right))
                )
                worst_per_axis["z_mm"] = max(worst_per_axis.get("z_mm", 0.0), abs(pose.z) * 1000)

            print(f"  {n_calls} set_target calls captured")
            print(f"  worst-case magnitudes: {worst_per_axis}")
            if phase_violations:
                # Deduplicate before reporting
                uniq = sorted(set(phase_violations))
                print(f"  ENVELOPE VIOLATIONS ({len(uniq)} unique):")
                for vline in uniq:
                    print(f"    - {vline}")
                overall_violations.extend(f"{name}: {v}" for v in uniq)
            else:
                print("  OK: every captured pose is inside the safe envelope")

            # Brief pause between emotions
            time.sleep(0.5)
    finally:
        print("\n=== Shutting down EmotionPlayer ===")
        player.shutdown(timeout=3.0)

    print("\n=== SUMMARY ===")
    print(f"Total set_target captures: {len(captured)}")
    print(f"WARN-level log records:    {len(warn_counter.records)}")
    for r in warn_counter.records:
        print(f"  WARN: {r.getMessage()}")

    if overall_violations:
        print(f"\nFAIL: {len(overall_violations)} envelope violation(s) observed")
        return 1
    if warn_counter.records:
        # Clamp warnings or sleep-state-fallback warnings are not fatal —
        # they mean the safety chain caught something. But surface them.
        print(
            "\nWARN: safety chain logged warnings — review above. "
            "Returning 0 because envelope was never breached at set_target."
        )
    print("\nPASS: all emotions stayed inside the safe envelope")
    return 0


if __name__ == "__main__":
    sys.exit(main())
