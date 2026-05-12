"""YAML loader + validator for emotion definitions.

Reads every `*.yaml` file under reachy_mini_home_assistant/emotions/,
validates against the schema documented in docs/emotion-catalog.md §5,
converts operator units (mm, degrees) to SDK-native units (m, radians),
and returns a {name: Emotion} dictionary the EmotionPlayer dispatches over.

Validation is strict: out-of-envelope pose values refuse to load with a
clear error. Operator mistakes are caught loudly at boot rather than
producing surprising clamped motion at runtime.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Union

import yaml
from reachy_mini.utils.interpolation import InterpolationTechnique

from .emotion_pose import EmotionPose, Phase

logger = logging.getLogger(__name__)


# -----------------------------------------------------------------------------
# Safe envelope (catalog.md §1). Out-of-envelope values refuse to load.
# Values in SDK-native units (m, rad).
# -----------------------------------------------------------------------------

MAX_TRANSLATION_M = 0.010          # 10 mm
MAX_PITCH_RAD = math.radians(25)
MAX_YAW_RAD = math.radians(30)
MAX_ROLL_RAD = math.radians(20)
MAX_ANTENNA_RAD = math.radians(90)
MAX_BODY_YAW_RAD = math.radians(20)


# -----------------------------------------------------------------------------
# Easing name → InterpolationTechnique enum.
# -----------------------------------------------------------------------------

_EASING_MAP: Dict[str, InterpolationTechnique] = {
    "min_jerk": InterpolationTechnique.MIN_JERK,
    "ease_in_out": InterpolationTechnique.EASE_IN_OUT,
    "cartoon": InterpolationTechnique.CARTOON,
    "linear": InterpolationTechnique.LINEAR,
}


# -----------------------------------------------------------------------------
# Emotion datatypes.
# -----------------------------------------------------------------------------


@dataclass(frozen=True)
class Oscillator:
    """Single per-field sinusoidal oscillator for continuous emotions.

    Computes: amplitude * sin(2π · frequency_hz · t + phase_offset_rad)
    and adds it to the static_offset for the named EmotionPose field.

    `field` must be one of the EmotionPose attribute names. `amplitude`
    is in SDK-native units (m for translation, rad for angles); the
    YAML loader converts from operator units (mm, deg) before constructing.
    """

    field: str
    amplitude: float
    frequency_hz: float
    phase_offset_rad: float = 0.0


@dataclass(frozen=True)
class ContinuousEmotion:
    """An oscillator-driven looping emotion (idle, listening, etc.)."""

    name: str
    description: str
    static_offset: EmotionPose
    oscillators: List[Oscillator]
    loops: bool = True


@dataclass(frozen=True)
class OneShotEmotion:
    """A phase-list emotion (happy, sad, etc.). Plays once and completes."""

    name: str
    description: str
    phases: List[Phase]

    @property
    def total_duration_s(self) -> float:
        return sum(p.duration_s for p in self.phases)


Emotion = Union[OneShotEmotion, ContinuousEmotion]


# -----------------------------------------------------------------------------
# Errors.
# -----------------------------------------------------------------------------


class EmotionConfigError(ValueError):
    """Raised when a YAML emotion file violates the schema or envelope."""


# -----------------------------------------------------------------------------
# Unit conversion (operator-facing units → SDK-native).
# -----------------------------------------------------------------------------

# (yaml_field, dataclass_field, conversion_callable)
_POSE_FIELD_MAP: List[tuple] = [
    ("x_mm",                "x",              lambda v: v / 1000.0),
    ("y_mm",                "y",              lambda v: v / 1000.0),
    ("z_mm",                "z",              lambda v: v / 1000.0),
    ("roll_deg",            "roll",           math.radians),
    ("pitch_deg",           "pitch",          math.radians),
    ("yaw_deg",             "yaw",            math.radians),
    ("antenna_left_deg",    "antenna_left",   math.radians),
    ("antenna_right_deg",   "antenna_right",  math.radians),
    ("body_yaw_deg",        "body_yaw",       math.radians),
]


def _parse_pose(yaml_pose: dict, *, path: Path, where: str) -> EmotionPose:
    """Convert a YAML `end:` or `static_offset:` block into an EmotionPose."""
    if yaml_pose is None:
        yaml_pose = {}
    if not isinstance(yaml_pose, dict):
        raise EmotionConfigError(
            f"{path}:{where}: pose block must be a mapping, got {type(yaml_pose).__name__}"
        )

    kwargs: Dict[str, float] = {}
    unknown = set(yaml_pose.keys()) - {f[0] for f in _POSE_FIELD_MAP}
    if unknown:
        raise EmotionConfigError(
            f"{path}:{where}: unknown pose field(s): {sorted(unknown)}; "
            f"valid: {sorted(f[0] for f in _POSE_FIELD_MAP)}"
        )

    for yaml_key, py_key, conv in _POSE_FIELD_MAP:
        if yaml_key in yaml_pose:
            kwargs[py_key] = conv(float(yaml_pose[yaml_key]))

    pose = EmotionPose(**kwargs)
    _check_safe_envelope(pose, path=path, where=where)
    return pose


def _check_safe_envelope(pose: EmotionPose, *, path: Path, where: str) -> None:
    """Refuse poses outside the documented safe envelope (catalog.md §1)."""
    violations = []
    if abs(pose.x) > MAX_TRANSLATION_M:
        violations.append(f"x={pose.x*1000:.1f}mm > ±{MAX_TRANSLATION_M*1000:.0f}mm")
    if abs(pose.y) > MAX_TRANSLATION_M:
        violations.append(f"y={pose.y*1000:.1f}mm > ±{MAX_TRANSLATION_M*1000:.0f}mm")
    if abs(pose.z) > MAX_TRANSLATION_M:
        violations.append(f"z={pose.z*1000:.1f}mm > ±{MAX_TRANSLATION_M*1000:.0f}mm")
    if abs(pose.pitch) > MAX_PITCH_RAD:
        violations.append(f"pitch={math.degrees(pose.pitch):.1f}° > ±{math.degrees(MAX_PITCH_RAD):.0f}°")
    if abs(pose.yaw) > MAX_YAW_RAD:
        violations.append(f"yaw={math.degrees(pose.yaw):.1f}° > ±{math.degrees(MAX_YAW_RAD):.0f}°")
    if abs(pose.roll) > MAX_ROLL_RAD:
        violations.append(f"roll={math.degrees(pose.roll):.1f}° > ±{math.degrees(MAX_ROLL_RAD):.0f}°")
    if abs(pose.antenna_left) > MAX_ANTENNA_RAD:
        violations.append(f"antenna_left={math.degrees(pose.antenna_left):.1f}° > ±{math.degrees(MAX_ANTENNA_RAD):.0f}°")
    if abs(pose.antenna_right) > MAX_ANTENNA_RAD:
        violations.append(f"antenna_right={math.degrees(pose.antenna_right):.1f}° > ±{math.degrees(MAX_ANTENNA_RAD):.0f}°")
    if abs(pose.body_yaw) > MAX_BODY_YAW_RAD:
        violations.append(f"body_yaw={math.degrees(pose.body_yaw):.1f}° > ±{math.degrees(MAX_BODY_YAW_RAD):.0f}°")

    if violations:
        raise EmotionConfigError(
            f"{path}:{where}: pose value(s) outside safe envelope: " + "; ".join(violations)
        )


# -----------------------------------------------------------------------------
# One-shot and continuous parsers.
# -----------------------------------------------------------------------------


def _parse_one_shot(data: dict, path: Path) -> OneShotEmotion:
    phases_raw = data.get("phases")
    if not isinstance(phases_raw, list) or not phases_raw:
        raise EmotionConfigError(f"{path}: one_shot emotion must have a non-empty `phases:` list")

    phases: List[Phase] = []
    for i, phase_raw in enumerate(phases_raw):
        if not isinstance(phase_raw, dict):
            raise EmotionConfigError(f"{path}: phases[{i}] must be a mapping")

        duration_s = phase_raw.get("duration_s")
        if duration_s is None or not isinstance(duration_s, (int, float)) or duration_s <= 0:
            raise EmotionConfigError(
                f"{path}: phases[{i}].duration_s must be a positive number, got {duration_s!r}"
            )

        easing_name = phase_raw.get("easing", "min_jerk")
        if easing_name not in _EASING_MAP:
            raise EmotionConfigError(
                f"{path}: phases[{i}].easing must be one of {sorted(_EASING_MAP.keys())}, "
                f"got {easing_name!r}"
            )

        end_pose = _parse_pose(
            phase_raw.get("end", {}),
            path=path,
            where=f"phases[{i}].end",
        )

        phases.append(
            Phase(end=end_pose, duration_s=float(duration_s), easing=_EASING_MAP[easing_name])
        )

    return OneShotEmotion(
        name=data["name"],
        description=str(data.get("description", "")),
        phases=phases,
    )


def _parse_continuous(data: dict, path: Path) -> ContinuousEmotion:
    static_offset = _parse_pose(
        data.get("static_offset", {}),
        path=path,
        where="static_offset",
    )

    osc_raw = data.get("oscillators")
    if not isinstance(osc_raw, list):
        raise EmotionConfigError(f"{path}: continuous emotion must have an `oscillators:` list")

    oscillators: List[Oscillator] = []
    valid_fields = {f[1] for f in _POSE_FIELD_MAP}  # python attribute names
    valid_yaml_fields = {f[0] for f in _POSE_FIELD_MAP}  # yaml-style names

    # Map yaml field → python field and amplitude conversion
    yaml_to_py = {f[0]: (f[1], f[2]) for f in _POSE_FIELD_MAP}

    for i, osc in enumerate(osc_raw):
        if not isinstance(osc, dict):
            raise EmotionConfigError(f"{path}: oscillators[{i}] must be a mapping")

        field_name = osc.get("field")
        if field_name not in yaml_to_py:
            raise EmotionConfigError(
                f"{path}: oscillators[{i}].field must be one of {sorted(yaml_to_py.keys())}, "
                f"got {field_name!r}"
            )

        py_field, conv = yaml_to_py[field_name]

        amplitude_raw = osc.get("amplitude")
        if amplitude_raw is None or not isinstance(amplitude_raw, (int, float)):
            raise EmotionConfigError(
                f"{path}: oscillators[{i}].amplitude must be a number, got {amplitude_raw!r}"
            )

        frequency_hz = osc.get("frequency_hz")
        if frequency_hz is None or not isinstance(frequency_hz, (int, float)) or frequency_hz <= 0:
            raise EmotionConfigError(
                f"{path}: oscillators[{i}].frequency_hz must be a positive number"
            )

        phase_offset_rad = float(osc.get("phase_offset_rad", 0.0))

        oscillators.append(
            Oscillator(
                field=py_field,
                amplitude=conv(float(amplitude_raw)),
                frequency_hz=float(frequency_hz),
                phase_offset_rad=phase_offset_rad,
            )
        )

    return ContinuousEmotion(
        name=data["name"],
        description=str(data.get("description", "")),
        static_offset=static_offset,
        oscillators=oscillators,
        loops=bool(data.get("loops", True)),
    )


# -----------------------------------------------------------------------------
# Public API.
# -----------------------------------------------------------------------------


def load_emotion_file(path: Path) -> Emotion:
    """Parse a single YAML file into an Emotion. Raises EmotionConfigError."""
    try:
        with path.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except yaml.YAMLError as e:
        raise EmotionConfigError(f"{path}: YAML parse error: {e}") from e

    if not isinstance(data, dict):
        raise EmotionConfigError(f"{path}: top-level must be a mapping")

    name = data.get("name")
    if not isinstance(name, str) or not name:
        raise EmotionConfigError(f"{path}: `name:` is required and must be a non-empty string")

    emotion_type = data.get("type")
    if emotion_type == "one_shot":
        return _parse_one_shot(data, path)
    if emotion_type == "continuous":
        return _parse_continuous(data, path)
    raise EmotionConfigError(
        f"{path}: `type:` must be 'one_shot' or 'continuous', got {emotion_type!r}"
    )


def load_emotions(emotions_dir: Path) -> Dict[str, Emotion]:
    """Load every `*.yaml` file in `emotions_dir` and return a name→Emotion map.

    Duplicate names across files raise EmotionConfigError. A single file
    failing to parse raises immediately — no silent skips, so operator
    mistakes are caught at boot.
    """
    if not emotions_dir.exists():
        logger.warning("Emotions directory does not exist: %s", emotions_dir)
        return {}

    result: Dict[str, Emotion] = {}
    files = sorted(emotions_dir.glob("*.yaml"))
    for path in files:
        emotion = load_emotion_file(path)
        if emotion.name in result:
            raise EmotionConfigError(
                f"{path}: duplicate emotion name {emotion.name!r}; "
                f"previously defined in another file"
            )
        result[emotion.name] = emotion
        logger.info("Loaded emotion %r (%s) from %s", emotion.name, type(emotion).__name__, path.name)

    logger.info("Loaded %d emotion(s) from %s", len(result), emotions_dir)
    return result


def sample_continuous(
    emotion: ContinuousEmotion,
    t: float,
) -> EmotionPose:
    """Evaluate a continuous emotion at time `t` (seconds since play start)."""
    # Start from static_offset's component values.
    values: Dict[str, float] = {
        "x": emotion.static_offset.x,
        "y": emotion.static_offset.y,
        "z": emotion.static_offset.z,
        "roll": emotion.static_offset.roll,
        "pitch": emotion.static_offset.pitch,
        "yaw": emotion.static_offset.yaw,
        "antenna_right": emotion.static_offset.antenna_right,
        "antenna_left": emotion.static_offset.antenna_left,
        "body_yaw": emotion.static_offset.body_yaw,
    }
    two_pi = 2.0 * math.pi
    for osc in emotion.oscillators:
        if osc.field in values:
            values[osc.field] = values[osc.field] + osc.amplitude * math.sin(
                two_pi * osc.frequency_hz * t + osc.phase_offset_rad
            )
    return EmotionPose(**values)
