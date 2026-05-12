"""Pose dataclass + interpolation helpers for the new EmotionPlayer.

Adopted from the reference app at
/home/nolte/repos/github/reachy-mini-app/reachy_mini_app/blocks.py
and adapted for this project's emotion pipeline refactor.

The key correctness property this module enforces is that we never
linearly interpolate 4x4 rotation matrices — every interpolation is
component-wise on scalars, and the head matrix is constructed from
scalars at the point of set_target via create_head_pose. See
docs/refactor-emotion-pipeline-design.md §5.1 for the full rationale.

All EmotionPose fields are in SDK-native units (metres, radians) so
that no conversion happens between the dataclass and set_target. The
YAML loader (motion/emotion_loader.py) is responsible for converting
operator-facing units (mm, degrees) to these.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Tuple

import numpy as np
from reachy_mini.utils import create_head_pose
from reachy_mini.utils.interpolation import InterpolationTechnique


@dataclass(frozen=True)
class EmotionPose:
    """Component-wise pose: head translation + euler + antennas + body yaw.

    All units SDK-native:
      x, y, z       in metres
      roll, pitch, yaw, antenna_*, body_yaw in radians

    The dataclass is frozen so each interpolation tick produces a new
    immutable value — easier to reason about under threading.
    """

    # Head translation (metres).
    x: float = 0.0
    y: float = 0.0
    z: float = 0.0
    # Head orientation (radians). XYZ intrinsic euler, matching
    # reachy_mini.utils.create_head_pose's convention.
    roll: float = 0.0
    pitch: float = 0.0
    yaw: float = 0.0
    # Antennas (radians). SDK set_target expects ordering [right, left].
    antenna_right: float = 0.0
    antenna_left: float = 0.0
    # Body yaw (radians).
    body_yaw: float = 0.0


NEUTRAL = EmotionPose()


def lerp_pose(a: EmotionPose, b: EmotionPose, s: float) -> EmotionPose:
    """Component-wise linear interpolation, s clamped to [0, 1].

    Critically NOT matrix-level interpolation — every field is a scalar.
    """
    s = max(0.0, min(1.0, s))

    def L(p: float, q: float) -> float:
        return p + (q - p) * s

    return EmotionPose(
        x=L(a.x, b.x),
        y=L(a.y, b.y),
        z=L(a.z, b.z),
        roll=L(a.roll, b.roll),
        pitch=L(a.pitch, b.pitch),
        yaw=L(a.yaw, b.yaw),
        antenna_right=L(a.antenna_right, b.antenna_right),
        antenna_left=L(a.antenna_left, b.antenna_left),
        body_yaw=L(a.body_yaw, b.body_yaw),
    )


@dataclass(frozen=True)
class Phase:
    """One phase of a one-shot emotion: target pose, duration, easing.

    Duration is in seconds (wall-clock for the phase to complete). The
    EmotionPlayer uses `reachy_mini.utils.interpolation.time_trajectory`
    to map linear progress to the eased curve.
    """

    end: EmotionPose
    duration_s: float
    easing: InterpolationTechnique = InterpolationTechnique.MIN_JERK


def to_set_target(pose: EmotionPose) -> Tuple[np.ndarray, np.ndarray, float]:
    """Build the (head_matrix, antennas_array, body_yaw) tuple ready for
    ReachyMini.set_target(head=..., antennas=..., body_yaw=...).

    The head matrix is constructed from scalars at this point — we never
    interpolate matrices. The antennas array is in [right, left] order
    per SDK convention.
    """
    head_matrix = create_head_pose(
        x=pose.x,
        y=pose.y,
        z=pose.z,
        roll=pose.roll,
        pitch=pose.pitch,
        yaw=pose.yaw,
        mm=False,
        degrees=False,
    )
    antennas = np.array([pose.antenna_right, pose.antenna_left], dtype=np.float64)
    return head_matrix, antennas, float(pose.body_yaw)


def from_present(
    head_matrix: np.ndarray,
    antennas: np.ndarray,
    body_yaw: float = 0.0,
) -> EmotionPose:
    """Convert a present-pose read from the SDK (4x4 head, [r,l] antennas,
    body_yaw) into an EmotionPose so we can lerp from it.

    Euler extraction follows the same convention create_head_pose builds:
    XYZ intrinsic. Gimbal-lock-safe via atan2 fallback near ±π/2.
    """
    R = head_matrix
    pitch = float(np.arcsin(np.clip(-R[2, 0], -1.0, 1.0)))
    if abs(np.cos(pitch)) > 1e-6:
        roll = float(np.arctan2(R[2, 1], R[2, 2]))
        yaw = float(np.arctan2(R[1, 0], R[0, 0]))
    else:
        # Gimbal-lock fallback
        roll = 0.0
        yaw = float(np.arctan2(-R[0, 1], R[1, 1]))

    return EmotionPose(
        x=float(R[0, 3]),
        y=float(R[1, 3]),
        z=float(R[2, 3]),
        roll=roll,
        pitch=pitch,
        yaw=yaw,
        antenna_right=float(antennas[0]),
        antenna_left=float(antennas[1]),
        body_yaw=float(body_yaw),
    )
