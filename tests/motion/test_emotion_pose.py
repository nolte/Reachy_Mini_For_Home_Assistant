"""Tests for the EmotionPose dataclass + lerp + to_set_target helpers."""

from __future__ import annotations

import math

import numpy as np
import pytest

from reachy_mini_home_assistant.motion.emotion_pose import (
    MAX_ANTENNA_RAD,
    MAX_BODY_YAW_RAD,
    MAX_PITCH_RAD,
    MAX_ROLL_RAD,
    MAX_TRANSLATION_M,
    MAX_YAW_RAD,
    NEUTRAL,
    EmotionPose,
    Phase,
    clamp_to_envelope,
    from_present,
    lerp_pose,
    to_set_target,
)


# -----------------------------------------------------------------------------
# Lerp
# -----------------------------------------------------------------------------


def test_lerp_at_s_zero_returns_a():
    a = EmotionPose(pitch=0.5, antenna_left=0.3)
    b = EmotionPose(pitch=1.0, antenna_left=-0.3)
    result = lerp_pose(a, b, 0.0)
    assert result == a


def test_lerp_at_s_one_returns_b():
    a = EmotionPose(pitch=0.5, antenna_left=0.3)
    b = EmotionPose(pitch=1.0, antenna_left=-0.3)
    result = lerp_pose(a, b, 1.0)
    assert result == b


def test_lerp_midpoint_is_componentwise_average():
    a = EmotionPose(x=0.0, pitch=0.0, antenna_left=0.0)
    b = EmotionPose(x=0.010, pitch=0.4, antenna_left=1.5)
    mid = lerp_pose(a, b, 0.5)
    assert mid.x == pytest.approx(0.005)
    assert mid.pitch == pytest.approx(0.2)
    assert mid.antenna_left == pytest.approx(0.75)


def test_lerp_s_clamped_below_zero():
    a = EmotionPose(pitch=0.5)
    b = EmotionPose(pitch=1.0)
    assert lerp_pose(a, b, -0.5) == a


def test_lerp_s_clamped_above_one():
    a = EmotionPose(pitch=0.5)
    b = EmotionPose(pitch=1.0)
    assert lerp_pose(a, b, 1.5) == b


# -----------------------------------------------------------------------------
# to_set_target — head matrix must be a valid 4x4
# -----------------------------------------------------------------------------


def test_to_set_target_returns_4x4_head_matrix():
    pose = EmotionPose(pitch=0.2, antenna_left=0.5, antenna_right=-0.5)
    head, ant, by = to_set_target(pose)
    assert head.shape == (4, 4)
    assert ant.shape == (2,)
    assert isinstance(by, float)


def test_to_set_target_neutral_is_identity_translation():
    head, ant, by = to_set_target(NEUTRAL)
    # Translation column zero
    assert head[0, 3] == pytest.approx(0.0)
    assert head[1, 3] == pytest.approx(0.0)
    assert head[2, 3] == pytest.approx(0.0)
    # Antennas zero
    assert ant[0] == pytest.approx(0.0)
    assert ant[1] == pytest.approx(0.0)
    assert by == pytest.approx(0.0)


def test_to_set_target_antenna_order_is_right_then_left():
    pose = EmotionPose(antenna_right=0.3, antenna_left=-0.4)
    head, ant, by = to_set_target(pose)
    # SDK convention: [right, left]
    assert ant[0] == pytest.approx(0.3)
    assert ant[1] == pytest.approx(-0.4)


def test_to_set_target_rotation_is_orthonormal():
    """The 4x4 rotation block must remain orthonormal — the matrix
    interpolation bug from today's session is exactly what we're
    preventing here."""
    pose = EmotionPose(roll=0.3, pitch=-0.2, yaw=0.15)
    head, _, _ = to_set_target(pose)
    R = head[:3, :3]
    # R · Rᵀ ≈ I
    product = R @ R.T
    assert np.allclose(product, np.eye(3), atol=1e-9)


# -----------------------------------------------------------------------------
# from_present round-trip
# -----------------------------------------------------------------------------


def test_from_present_roundtrip_preserves_translation():
    original = EmotionPose(
        x=0.005, y=-0.003, z=0.002, roll=0.1, pitch=-0.2, yaw=0.05, antenna_right=0.4, antenna_left=-0.4, body_yaw=0.1
    )
    head, ant, by = to_set_target(original)
    recovered = from_present(head, ant, body_yaw=by)
    assert recovered.x == pytest.approx(original.x, abs=1e-9)
    assert recovered.y == pytest.approx(original.y, abs=1e-9)
    assert recovered.z == pytest.approx(original.z, abs=1e-9)
    assert recovered.antenna_right == pytest.approx(original.antenna_right, abs=1e-9)
    assert recovered.antenna_left == pytest.approx(original.antenna_left, abs=1e-9)
    assert recovered.body_yaw == pytest.approx(original.body_yaw, abs=1e-9)


# -----------------------------------------------------------------------------
# Phase dataclass — frozen, comparable
# -----------------------------------------------------------------------------


def test_phase_is_frozen():
    p = Phase(end=NEUTRAL, duration_s=1.0)
    with pytest.raises(Exception):
        p.duration_s = 2.0  # type: ignore[misc]


# -----------------------------------------------------------------------------
# clamp_to_envelope — defense-in-depth runtime guard
# -----------------------------------------------------------------------------


def test_clamp_to_envelope_passes_through_safe_pose():
    safe = EmotionPose(
        x=0.003,
        y=-0.002,
        z=0.005,
        roll=math.radians(5),
        pitch=math.radians(10),
        yaw=math.radians(8),
        antenna_right=math.radians(30),
        antenna_left=math.radians(-20),
        body_yaw=math.radians(5),
    )
    clamped, did = clamp_to_envelope(safe)
    assert did is False
    # Identity short-circuit: same object back
    assert clamped is safe


def test_clamp_to_envelope_clamps_each_axis_independently():
    bad = EmotionPose(
        x=0.050,  # 50 mm  → +10 mm
        y=-0.020,  # -20 mm → -10 mm
        z=0.0,  # OK
        roll=math.radians(45),  # +45° → +20°
        pitch=math.radians(-40),  # -40° → -25°
        yaw=math.radians(60),  # +60° → +30°
        antenna_right=math.radians(120),  # +120° → +90°
        antenna_left=math.radians(-95),  # -95° → -90°
        body_yaw=math.radians(35),  # +35° → +20°
    )
    clamped, did = clamp_to_envelope(bad)
    assert did is True
    assert clamped.x == pytest.approx(MAX_TRANSLATION_M)
    assert clamped.y == pytest.approx(-MAX_TRANSLATION_M)
    assert clamped.z == pytest.approx(0.0)
    assert clamped.roll == pytest.approx(MAX_ROLL_RAD)
    assert clamped.pitch == pytest.approx(-MAX_PITCH_RAD)
    assert clamped.yaw == pytest.approx(MAX_YAW_RAD)
    assert clamped.antenna_right == pytest.approx(MAX_ANTENNA_RAD)
    assert clamped.antenna_left == pytest.approx(-MAX_ANTENNA_RAD)
    assert clamped.body_yaw == pytest.approx(MAX_BODY_YAW_RAD)


def test_clamp_to_envelope_only_offending_axis_changes():
    """A single out-of-range axis must not perturb the others."""
    pose = EmotionPose(
        x=0.002,
        pitch=math.radians(50),  # only this is over
        antenna_left=math.radians(20),
    )
    clamped, did = clamp_to_envelope(pose)
    assert did is True
    assert clamped.x == pytest.approx(0.002)
    assert clamped.pitch == pytest.approx(MAX_PITCH_RAD)
    assert clamped.antenna_left == pytest.approx(math.radians(20))


def test_clamp_to_envelope_boundary_value_not_clamped():
    """Exactly at the limit is allowed (matches loader's > semantics)."""
    boundary = EmotionPose(
        pitch=MAX_PITCH_RAD,
        antenna_right=MAX_ANTENNA_RAD,
    )
    _, did = clamp_to_envelope(boundary)
    assert did is False
