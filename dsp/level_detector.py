"""
VolSteady — DSP Level Detector
Envelope follower with separate attack/release ballistics.
Uses Numba JIT for per-sample processing at near-C speed.
"""

import numpy as np
import numba as nb


@nb.jit(nopython=True, cache=True)
def _compute_rms_envelope(
    samples: np.ndarray,
    prev_level: float,
    attack_coeff: float,
    release_coeff: float,
) -> tuple:
    """
    Per-sample RMS envelope follower with attack/release ballistics.
    Returns (envelope array, final level for state carryover).
    """
    n = len(samples)
    envelope = np.empty(n, dtype=np.float32)
    level = prev_level

    for i in range(n):
        # Squared amplitude (RMS power)
        power = samples[i] * samples[i]
        if power > level:
            level = attack_coeff * level + (1.0 - attack_coeff) * power
        else:
            level = release_coeff * level + (1.0 - release_coeff) * power
        envelope[i] = np.sqrt(max(level, 1e-12))

    return envelope, level


@nb.jit(nopython=True, cache=True)
def _compute_peak_envelope(
    samples: np.ndarray,
    prev_level: float,
    attack_coeff: float,
    release_coeff: float,
) -> tuple:
    """
    Per-sample peak envelope follower with attack/release ballistics.
    Returns (envelope array, final level for state carryover).
    """
    n = len(samples)
    envelope = np.empty(n, dtype=np.float32)
    level = prev_level

    for i in range(n):
        peak = abs(samples[i])
        if peak > level:
            level = attack_coeff * level + (1.0 - attack_coeff) * peak
        else:
            level = release_coeff * level + (1.0 - release_coeff) * peak
        envelope[i] = max(level, 1e-12)

    return envelope, level


def _time_to_coeff(time_ms: float, sample_rate: int) -> float:
    """Convert a time constant in milliseconds to a smoothing coefficient."""
    if time_ms <= 0:
        return 0.0
    tau_samples = (time_ms / 1000.0) * sample_rate
    return float(np.exp(-1.0 / tau_samples))


def linear_to_db(linear: np.ndarray) -> np.ndarray:
    """Convert linear amplitude to dB, clamped to avoid -inf."""
    return 20.0 * np.log10(np.maximum(linear, 1e-12))


def db_to_linear(db: float) -> float:
    """Convert dB to linear amplitude."""
    return 10.0 ** (db / 20.0)


class RMSDetector:
    """Stateful RMS envelope detector."""

    def __init__(self, attack_ms: float = 10.0, release_ms: float = 200.0, sample_rate: int = 48000):
        self.sample_rate = sample_rate
        self._level = 0.0  # squared, for power
        self.set_times(attack_ms, release_ms)

    def set_times(self, attack_ms: float, release_ms: float):
        self._attack_coeff = _time_to_coeff(attack_ms, self.sample_rate)
        self._release_coeff = _time_to_coeff(release_ms, self.sample_rate)

    def process(self, samples: np.ndarray) -> np.ndarray:
        """Process a chunk of mono float32 samples, return RMS envelope."""
        envelope, self._level = _compute_rms_envelope(
            samples.astype(np.float32),
            self._level,
            self._attack_coeff,
            self._release_coeff,
        )
        return envelope

    def reset(self):
        self._level = 0.0


class PeakDetector:
    """Stateful peak envelope detector."""

    def __init__(self, attack_ms: float = 0.1, release_ms: float = 50.0, sample_rate: int = 48000):
        self.sample_rate = sample_rate
        self._level = 0.0
        self.set_times(attack_ms, release_ms)

    def set_times(self, attack_ms: float, release_ms: float):
        self._attack_coeff = _time_to_coeff(attack_ms, self.sample_rate)
        self._release_coeff = _time_to_coeff(release_ms, self.sample_rate)

    def process(self, samples: np.ndarray) -> np.ndarray:
        """Process a chunk of mono float32 samples, return peak envelope."""
        envelope, self._level = _compute_peak_envelope(
            samples.astype(np.float32),
            self._level,
            self._attack_coeff,
            self._release_coeff,
        )
        return envelope

    def reset(self):
        self._level = 0.0
