"""Tests for the YAML emotion loader: schema validation, safe envelope,
unit conversion, and continuous-emotion oscillator math."""

from __future__ import annotations

import math
import textwrap
from pathlib import Path

import pytest

from reachy_mini_home_assistant.motion.emotion_loader import (
    ContinuousEmotion,
    EmotionConfigError,
    OneShotEmotion,
    load_emotion_file,
    load_emotions,
    sample_continuous,
)


def write_yaml(tmp_path: Path, name: str, content: str) -> Path:
    p = tmp_path / name
    p.write_text(textwrap.dedent(content), encoding="utf-8")
    return p


# -----------------------------------------------------------------------------
# Happy path — both emotion types parse cleanly
# -----------------------------------------------------------------------------


def test_load_one_shot_minimal(tmp_path):
    f = write_yaml(
        tmp_path,
        "wave.yaml",
        """\
    name: wave
    description: Test wave
    type: one_shot
    phases:
      - name: lift
        duration_s: 0.5
        easing: cartoon
        end:
          z_mm: 5
          pitch_deg: 10
          antenna_left_deg: 30
          antenna_right_deg: -30
      - name: home
        duration_s: 0.5
        easing: min_jerk
        end: {}
    """,
    )
    em = load_emotion_file(f)
    assert isinstance(em, OneShotEmotion)
    assert em.name == "wave"
    assert len(em.phases) == 2
    assert em.phases[0].duration_s == 0.5
    # mm → m
    assert em.phases[0].end.z == pytest.approx(0.005)
    # deg → rad
    assert em.phases[0].end.pitch == pytest.approx(math.radians(10))
    assert em.phases[0].end.antenna_left == pytest.approx(math.radians(30))


def test_load_continuous_minimal(tmp_path):
    f = write_yaml(
        tmp_path,
        "hum.yaml",
        """\
    name: hum
    description: Test hum
    type: continuous
    loops: true
    static_offset:
      z_mm: 3
    oscillators:
      - field: z_mm
        amplitude: 2
        frequency_hz: 0.25
    """,
    )
    em = load_emotion_file(f)
    assert isinstance(em, ContinuousEmotion)
    assert em.name == "hum"
    assert em.static_offset.z == pytest.approx(0.003)
    assert len(em.oscillators) == 1
    assert em.oscillators[0].field == "z"
    assert em.oscillators[0].amplitude == pytest.approx(0.002)


# -----------------------------------------------------------------------------
# Schema errors — must raise EmotionConfigError with a clear message
# -----------------------------------------------------------------------------


def test_missing_name_raises(tmp_path):
    f = write_yaml(
        tmp_path,
        "x.yaml",
        """\
    description: no name
    type: one_shot
    phases:
      - duration_s: 0.5
        end: {}
    """,
    )
    with pytest.raises(EmotionConfigError, match="name"):
        load_emotion_file(f)


def test_unknown_type_raises(tmp_path):
    f = write_yaml(
        tmp_path,
        "x.yaml",
        """\
    name: x
    type: dance_off
    """,
    )
    with pytest.raises(EmotionConfigError, match="one_shot|continuous"):
        load_emotion_file(f)


def test_unknown_easing_raises(tmp_path):
    f = write_yaml(
        tmp_path,
        "x.yaml",
        """\
    name: x
    type: one_shot
    phases:
      - duration_s: 0.5
        easing: bounce_of_death
        end: {}
    """,
    )
    with pytest.raises(EmotionConfigError, match="easing"):
        load_emotion_file(f)


def test_negative_duration_raises(tmp_path):
    f = write_yaml(
        tmp_path,
        "x.yaml",
        """\
    name: x
    type: one_shot
    phases:
      - duration_s: -0.5
        end: {}
    """,
    )
    with pytest.raises(EmotionConfigError, match="duration"):
        load_emotion_file(f)


def test_unknown_pose_field_raises(tmp_path):
    f = write_yaml(
        tmp_path,
        "x.yaml",
        """\
    name: x
    type: one_shot
    phases:
      - duration_s: 0.5
        end:
          eye_color: red
    """,
    )
    with pytest.raises(EmotionConfigError, match="unknown pose field"):
        load_emotion_file(f)


def test_unknown_oscillator_field_raises(tmp_path):
    f = write_yaml(
        tmp_path,
        "x.yaml",
        """\
    name: x
    type: continuous
    oscillators:
      - field: tail_wag_deg
        amplitude: 5
        frequency_hz: 1.0
    """,
    )
    with pytest.raises(EmotionConfigError, match="field"):
        load_emotion_file(f)


# -----------------------------------------------------------------------------
# Safe envelope — out-of-range values refuse to load
# -----------------------------------------------------------------------------


def test_antenna_over_envelope_rejected(tmp_path):
    f = write_yaml(
        tmp_path,
        "danger.yaml",
        """\
    name: danger
    type: one_shot
    phases:
      - duration_s: 0.5
        end:
          antenna_left_deg: 175       # well over the ±90° cap
    """,
    )
    with pytest.raises(EmotionConfigError, match="safe envelope"):
        load_emotion_file(f)


def test_pitch_over_envelope_rejected(tmp_path):
    f = write_yaml(
        tmp_path,
        "danger.yaml",
        """\
    name: danger
    type: one_shot
    phases:
      - duration_s: 0.5
        end:
          pitch_deg: 90               # over the ±25° cap
    """,
    )
    with pytest.raises(EmotionConfigError, match="safe envelope"):
        load_emotion_file(f)


def test_translation_over_envelope_rejected(tmp_path):
    f = write_yaml(
        tmp_path,
        "danger.yaml",
        """\
    name: danger
    type: one_shot
    phases:
      - duration_s: 0.5
        end:
          z_mm: 50                    # over the ±10 mm cap
    """,
    )
    with pytest.raises(EmotionConfigError, match="safe envelope"):
        load_emotion_file(f)


def test_envelope_boundary_value_accepted(tmp_path):
    # Exactly at the cap should pass.
    f = write_yaml(
        tmp_path,
        "boundary.yaml",
        """\
    name: boundary
    type: one_shot
    phases:
      - duration_s: 0.5
        end:
          antenna_left_deg: 90
          pitch_deg: 25
    """,
    )
    em = load_emotion_file(f)
    assert isinstance(em, OneShotEmotion)


# -----------------------------------------------------------------------------
# load_emotions: directory aggregation + duplicate detection
# -----------------------------------------------------------------------------


def test_load_emotions_collects_all_yaml_files(tmp_path):
    write_yaml(
        tmp_path,
        "a.yaml",
        """\
    name: a
    type: one_shot
    phases:
      - duration_s: 0.1
        end: {}
    """,
    )
    write_yaml(
        tmp_path,
        "b.yaml",
        """\
    name: b
    type: continuous
    oscillators:
      - field: z_mm
        amplitude: 1
        frequency_hz: 0.5
    """,
    )
    result = load_emotions(tmp_path)
    assert set(result.keys()) == {"a", "b"}


def test_duplicate_emotion_name_raises(tmp_path):
    write_yaml(
        tmp_path,
        "first.yaml",
        """\
    name: same
    type: one_shot
    phases:
      - duration_s: 0.1
        end: {}
    """,
    )
    write_yaml(
        tmp_path,
        "second.yaml",
        """\
    name: same
    type: continuous
    oscillators:
      - field: z_mm
        amplitude: 1
        frequency_hz: 0.5
    """,
    )
    with pytest.raises(EmotionConfigError, match="duplicate"):
        load_emotions(tmp_path)


def test_missing_directory_returns_empty(tmp_path):
    nowhere = tmp_path / "does_not_exist"
    assert load_emotions(nowhere) == {}


# -----------------------------------------------------------------------------
# Continuous emotion sampling — oscillator math
# -----------------------------------------------------------------------------


def test_continuous_sampling_at_t_zero(tmp_path):
    """At t=0, sin(0)=0, so the value should equal static_offset."""
    f = write_yaml(
        tmp_path,
        "hum.yaml",
        """\
    name: hum
    type: continuous
    static_offset:
      z_mm: 5
    oscillators:
      - field: z_mm
        amplitude: 2
        frequency_hz: 0.25
    """,
    )
    em = load_emotion_file(f)
    pose = sample_continuous(em, 0.0)
    assert pose.z == pytest.approx(0.005)


def test_continuous_sampling_at_quarter_period(tmp_path):
    """At t = 1/(4·frequency), sin(π/2) = 1, so the value should equal
    static_offset + amplitude."""
    f = write_yaml(
        tmp_path,
        "hum.yaml",
        """\
    name: hum
    type: continuous
    static_offset:
      z_mm: 5
    oscillators:
      - field: z_mm
        amplitude: 2
        frequency_hz: 0.25
    """,
    )
    em = load_emotion_file(f)
    t = 1.0 / (4.0 * 0.25)  # = 1.0 s for f=0.25 Hz
    pose = sample_continuous(em, t)
    assert pose.z == pytest.approx(0.005 + 0.002)


# -----------------------------------------------------------------------------
# Oscillator worst-case envelope check (continuous emotions)
# -----------------------------------------------------------------------------


def test_oscillator_amplitude_pushes_over_envelope_rejected(tmp_path):
    """static_offset alone is in-envelope, but static + amplitude
    exceeds the limit → loader must reject."""
    f = write_yaml(
        tmp_path,
        "danger.yaml",
        """\
    name: danger
    type: continuous
    static_offset:
      pitch_deg: 20
    oscillators:
      - field: pitch_deg
        amplitude: 20
        frequency_hz: 0.5
    """,
    )
    # 20 deg + 20 deg = 40 deg > MAX_PITCH (25 deg)
    with pytest.raises(EmotionConfigError, match=r"oscillator worst-case"):
        load_emotion_file(f)


def test_oscillator_multiple_amplitudes_sum_on_same_axis(tmp_path):
    """Two oscillators on the same axis must have their amplitudes
    summed for the worst-case check (triangle inequality)."""
    f = write_yaml(
        tmp_path,
        "danger.yaml",
        """\
    name: danger
    type: continuous
    oscillators:
      - field: antenna_left_deg
        amplitude: 50
        frequency_hz: 0.5
      - field: antenna_left_deg
        amplitude: 50
        frequency_hz: 1.0
    """,
    )
    # 0 + (50 + 50) = 100 deg > MAX_ANTENNA (90 deg)
    with pytest.raises(EmotionConfigError, match=r"oscillator worst-case"):
        load_emotion_file(f)


def test_oscillator_negative_amplitude_uses_absolute_value(tmp_path):
    """Negative amplitudes still count toward the worst-case excursion."""
    f = write_yaml(
        tmp_path,
        "danger.yaml",
        """\
    name: danger
    type: continuous
    static_offset:
      yaw_deg: 25
    oscillators:
      - field: yaw_deg
        amplitude: -10
        frequency_hz: 0.5
    """,
    )
    # |25| + |-10| = 35 deg > MAX_YAW (30 deg)
    with pytest.raises(EmotionConfigError, match=r"oscillator worst-case"):
        load_emotion_file(f)


def test_oscillator_within_envelope_accepted(tmp_path):
    """static + Σ|amplitude| at the boundary is allowed (envelope check
    is `>`, not `>=`)."""
    f = write_yaml(
        tmp_path,
        "ok.yaml",
        """\
    name: ok
    type: continuous
    static_offset:
      pitch_deg: 15
    oscillators:
      - field: pitch_deg
        amplitude: 10
        frequency_hz: 0.5
    """,
    )
    em = load_emotion_file(f)
    assert isinstance(em, ContinuousEmotion)


def test_shipped_continuous_emotions_pass_worst_case(tmp_path):
    """Every continuous emotion shipped today (idle, listening, thinking,
    speaking) must continue to load — regression guard so the new check
    never accidentally rejects them."""
    shipped_dir = Path("reachy_mini_home_assistant/emotions")
    for name in ("idle", "listening", "thinking", "speaking"):
        em = load_emotion_file(shipped_dir / f"{name}.yaml")
        assert isinstance(em, ContinuousEmotion), name


def test_continuous_phase_offset_pi_is_antiphase(tmp_path):
    """phase_offset_rad: π should mean sin(π) = 0 at t=0, and the
    oscillator should be exactly anti-phase with one at phase 0."""
    f = write_yaml(
        tmp_path,
        "anti.yaml",
        """\
    name: anti
    type: continuous
    oscillators:
      - field: antenna_left_deg
        amplitude: 30
        frequency_hz: 1.0
      - field: antenna_right_deg
        amplitude: 30
        frequency_hz: 1.0
        phase_offset_rad: 3.14159265
    """,
    )
    em = load_emotion_file(f)
    # At quarter-period (t = 0.25 s, f = 1 Hz): sin(π/2) = +1, sin(π/2 + π) = -1
    pose = sample_continuous(em, 0.25)
    assert pose.antenna_left == pytest.approx(math.radians(30))
    assert pose.antenna_right == pytest.approx(-math.radians(30))
