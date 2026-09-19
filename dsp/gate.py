"""
VolSteady — DSP Noise Gate
Suppresses audio below a threshold to eliminate background hiss/hum.
Uses hold time to prevent choppy artifacts during brief pauses.
"""

import numpy as np
import numba as nb
from .level_detector import RMSDetector, db_to_linear, _time_to_coeff


@nb.jit(nopython=True, cache=True)
def _apply_gate(
    samples: np.ndarray,
    envelope: np.ndarray,
    threshold_linear: float,
    gain_state: float,
    hold_samples_remaining: int,
    hold_samples_total: int,
    open_coeff: float,
    close_coeff: float,
) -> tuple:
    """
    Apply noise gate gain to samples using pre-computed envelope.
    Returns (gated_samples, final_gain_state, hold_samples_remaining).
    """
    n = len(samples)
    out = np.empty(n, dtype=np.float32)
    gain = gain_state
    hold_remain = hold_samples_remaining

    for i in range(n):
        if envelope[i] >= threshold_linear:
            # Signal above threshold: open the gate
            hold_remain = hold_samples_total
            target = 1.0
        elif hold_remain > 0:
            # Hold: keep gate open for a moment after signal drops
            hold_remain -= 1
            target = 1.0
        else:
            # Below threshold and past hold: close the gate
            target = 0.0

        if target > gain:
            gain = open_coeff * gain + (1.0 - open_coeff) * target
        else:
            gain = close_coeff * gain + (1.0 - close_coeff) * target

        out[i] = samples[i] * gain

    return out, gain, hold_remain


@nb.jit(nopython=True, cache=True)
def _compute_gate_gain(
    envelope: np.ndarray,
    threshold_linear: float,
    gain_state: float,
    hold_samples_remaining: int,
    hold_samples_total: int,
    open_coeff: float,
    close_coeff: float,
) -> tuple:
    """
    Compute per-sample gate gain envelope (without multiplying into audio).
    Returns (gain_envelope, final_gain_state, hold_samples_remaining).
    Used for linked-stereo gating where the same gain applies to all channels.
    """
    n = len(envelope)
    gain_env = np.empty(n, dtype=np.float32)
    gain = gain_state
    hold_remain = hold_samples_remaining

    for i in range(n):
        if envelope[i] >= threshold_linear:
            hold_remain = hold_samples_total
            target = 1.0
        elif hold_remain > 0:
            hold_remain -= 1
            target = 1.0
        else:
            target = 0.0

        if target > gain:
            gain = open_coeff * gain + (1.0 - open_coeff) * target
        else:
            gain = close_coeff * gain + (1.0 - close_coeff) * target

        gain_env[i] = gain

    return gain_env, gain, hold_remain


class NoiseGate:
    """
    Noise gate with configurable threshold, attack, release, and hold.
    Processes stereo or mono float32 audio per-channel.
    """

    def __init__(
        self,
        threshold_db: float = -50.0,
        attack_ms: float = 0.5,
        release_ms: float = 50.0,
        hold_ms: float = 20.0,
        sample_rate: int = 48000,
    ):
        self.sample_rate = sample_rate
        self._enabled = True

        # State per channel (supports up to 2 channels)
        self._gain_state = [0.0, 0.0]
        self._hold_remain = [0, 0]
        self._detectors = [
            RMSDetector(attack_ms * 5, release_ms, sample_rate),
            RMSDetector(attack_ms * 5, release_ms, sample_rate),
        ]

        self.set_threshold(threshold_db)
        self.set_times(attack_ms, release_ms, hold_ms)

    def set_threshold(self, threshold_db: float):
        self._threshold_linear = float(db_to_linear(threshold_db))

    def set_times(self, attack_ms: float, release_ms: float, hold_ms: float):
        self._open_coeff = _time_to_coeff(attack_ms, self.sample_rate)
        self._close_coeff = _time_to_coeff(release_ms, self.sample_rate)
        self._hold_samples = int((hold_ms / 1000.0) * self.sample_rate)

    def set_enabled(self, enabled: bool):
        self._enabled = enabled

    def process(self, audio: np.ndarray) -> np.ndarray:
        """
        Process audio buffer (shape: [samples, channels] or [samples]).
        Returns processed audio of the same shape.

        Uses linked-stereo detection: the louder channel's envelope drives
        the gate for both channels, preserving the stereo image.
        """
        if not self._enabled:
            return audio

        mono = audio.ndim == 1
        if mono:
            audio = audio[:, np.newaxis]

        n_channels = audio.shape[1]
        active_channels = min(n_channels, 2)
        out = np.empty_like(audio)

        # --- Linked stereo: detect envelopes on all active channels ---
        envelopes = []
        for ch in range(active_channels):
            samples = audio[:, ch].astype(np.float32)
            envelope = self._detectors[ch].process(samples)
            envelopes.append(envelope)

        # Take the max envelope across channels (linked stereo sidechain)
        if active_channels == 2:
            linked_envelope = np.maximum(envelopes[0], envelopes[1])
        else:
            linked_envelope = envelopes[0]

        # Compute a single gate gain envelope from the linked sidechain
        gate_gain, gain, hold = _compute_gate_gain(
            linked_envelope,
            self._threshold_linear,
            self._gain_state[0],
            self._hold_remain[0],
            self._hold_samples,
            self._open_coeff,
            self._close_coeff,
        )
        self._gain_state[0] = gain
        self._gain_state[1] = gain
        self._hold_remain[0] = hold
        self._hold_remain[1] = hold

        # Apply the same gate gain to all active channels
        for ch in range(active_channels):
            out[:, ch] = audio[:, ch].astype(np.float32) * gate_gain

        if n_channels > 2:
            out[:, 2:] = audio[:, 2:]

        return out[:, 0] if mono else out

    def reset(self):
        self._gain_state = [0.0, 0.0]
        self._hold_remain = [0, 0]
        for d in self._detectors:
            d.reset()
