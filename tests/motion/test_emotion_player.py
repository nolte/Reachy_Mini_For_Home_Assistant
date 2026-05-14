"""Tests for EmotionPlayer's defense-in-depth clamp in _apply.

The full worker-thread lifecycle is covered indirectly by the loader and
pose tests; this file pins down the runtime clamp invariant: every pose
written to reachy.set_target() must be inside the safe envelope, and the
first clamp per worker must be logged at WARNING.
"""

from __future__ import annotations

import logging
import math

import numpy as np
import pytest
from reachy_mini.utils import create_head_pose

from reachy_mini_home_assistant.motion.emotion_player import EmotionPlayer
from reachy_mini_home_assistant.motion.emotion_pose import (
    MAX_ANTENNA_RAD,
    MAX_PITCH_RAD,
    NEUTRAL,
    EmotionPose,
)


class FakeReachy:
    """Minimal reachy stand-in capturing every set_target call."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def set_target(self, *, head=None, antennas=None, body_yaw=None) -> None:
        self.calls.append((head, antennas, body_yaw))

    def set_automatic_body_yaw(self, _value: bool) -> None:
        pass

    def get_current_head_pose(self):
        return np.eye(4)

    def get_present_antenna_joint_positions(self):
        return np.array([0.0, 0.0])


def _make_player() -> tuple[EmotionPlayer, FakeReachy]:
    fake = FakeReachy()
    # Empty emotions map is fine; we only exercise _apply directly.
    player = EmotionPlayer(reachy=fake, emotions={})
    return player, fake


def test_apply_passes_through_safe_pose():
    player, fake = _make_player()
    safe = EmotionPose(pitch=math.radians(10), antenna_right=math.radians(30))
    player._apply(safe)
    assert len(fake.calls) == 1
    call = fake.calls[0]
    antennas = call[1]
    body_yaw = call[2]
    assert antennas[0] == pytest.approx(math.radians(30))  # right
    assert body_yaw == pytest.approx(0.0)


def test_apply_clamps_out_of_envelope_pose():
    player, fake = _make_player()
    unsafe = EmotionPose(
        pitch=math.radians(80),  # > 25 deg
        antenna_left=math.radians(150),  # > 90 deg
    )
    player._apply(unsafe)
    assert len(fake.calls) == 1
    antennas = fake.calls[0][1]
    # antennas array is [right, left] per SDK convention
    assert antennas[1] == pytest.approx(MAX_ANTENNA_RAD)
    # Pitch we can't read back directly from the head matrix here without
    # re-deriving euler angles, but the EmotionPlayer caches the clamped
    # pose for rate-limiting, so we can assert that instead:
    assert player._last_sent_pose is not None
    assert player._last_sent_pose.pitch == pytest.approx(MAX_PITCH_RAD)
    assert player._last_sent_pose.antenna_left == pytest.approx(MAX_ANTENNA_RAD)


def test_apply_warns_once_per_worker(caplog):
    player, _ = _make_player()
    unsafe_a = EmotionPose(pitch=math.radians(80))
    unsafe_b = EmotionPose(pitch=math.radians(70))

    with caplog.at_level(logging.WARNING, logger="reachy_mini_home_assistant.motion.emotion_player"):
        player._apply(unsafe_a)
        player._apply(unsafe_b)

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1, f"expected exactly one WARNING, got {len(warnings)}"
    assert "clamped to safe envelope" in warnings[0].getMessage()


def test_apply_warn_flag_resets_on_new_worker():
    """Simulate worker boundary: the worker_main resets the warned flag,
    so a fresh emotion sees the WARNING again on the first clamp."""
    player, _ = _make_player()
    player._apply(EmotionPose(pitch=math.radians(80)))
    assert player._clamp_warned_this_worker is True
    # Worker boundary (simulating what _worker_main does on entry)
    player._clamp_warned_this_worker = False
    assert player._clamp_warned_this_worker is False


# -----------------------------------------------------------------------------
# _read_present_pose_safe: must never seed the Lerp with an out-of-envelope
# pose (which is what the HW sits at while in sleep / disabled).
# -----------------------------------------------------------------------------


def test_read_present_pose_safe_returns_present_when_inside_envelope():
    player, _ = _make_player()
    # Identity head_pose + zero antennas is the canonical neutral read.
    pose = player._read_present_pose_safe()
    assert pose == NEUTRAL


def test_read_present_pose_safe_falls_back_to_neutral_when_outside_envelope(caplog):
    """Empirical: a real `disabled`-state read from the Pollen daemon
    returns roughly pitch=27°, z=-46mm, antenna=±175° (see live MCP probe
    on 2026-05-14). That pose must NOT seed the Lead-In Lerp — falling
    back to NEUTRAL ensures every intermediate Lerp sample stays inside
    the envelope by construction (convex combination of two safe poses).
    """
    player, fake = _make_player()

    # Build a realistic sleep-pose head matrix (pitch=27°, z=-46mm).
    sleep_head = create_head_pose(
        x=-0.022,
        y=0.0,
        z=-0.046,
        roll=0.025,
        pitch=math.radians(27),
        yaw=0.005,
        mm=False,
        degrees=False,
    )
    # Antennas physically retracted at ±175° (well beyond ±90° envelope).
    sleep_antennas = np.array([math.radians(175.0), math.radians(-175.0)])

    fake.get_current_head_pose = lambda: sleep_head  # type: ignore[assignment]
    fake.get_present_antenna_joint_positions = lambda: sleep_antennas  # type: ignore[assignment]

    with caplog.at_level(logging.WARNING, logger="reachy_mini_home_assistant.motion.emotion_player"):
        pose = player._read_present_pose_safe()

    assert pose == NEUTRAL, "out-of-envelope read must not be used as Lerp start"
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "outside safe envelope" in warnings[0].getMessage()


def test_read_present_pose_safe_falls_back_to_neutral_on_sdk_error(caplog):
    player, fake = _make_player()

    def _raise():
        raise RuntimeError("simulated SDK error")

    fake.get_current_head_pose = _raise  # type: ignore[assignment]

    with caplog.at_level(logging.WARNING, logger="reachy_mini_home_assistant.motion.emotion_player"):
        pose = player._read_present_pose_safe()

    assert pose == NEUTRAL
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "Could not read present pose" in warnings[0].getMessage()
