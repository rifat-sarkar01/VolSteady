"""
VolSteady — Dynamic Range Compressor
Soft-knee feed-forward compressor with per-channel RMS detection.
This is the core of VolSteady: it tames loud passages and lifts quiet ones.
"""

import numpy as np
import numba as nb
from .level_detector import RMSDetector, linear_to_db, db_to_linear, _time_to_coeff


@nb.jit(nopython=True, cache=True)
def _compute_gain_reduction(
    envelope_db: np.ndarray,
    threshold_db: float,
    ratio: float,
    knee_db: float,
    gain_reduction_state: float,
    attack_coeff: float,
    release_coeff: float,
) -> tuple:
    """
    Compute per-sample gain reduction (in dB) using soft-knee curve,
    smoothed with attack/release ballistics.
    Returns (gain_reduction_db array, final_state).
    """
    n = len(envelope_db)
    gr = np.empty(n, dtype=np.float32)
    state = gain_reduction_state
    half_knee = knee_db * 0.5

    for i in range(n):
        level = envelope_db[i]
        overshoot = level - threshold_db

        # Soft-knee gain computation
        if overshoot < -half_knee:
            # Below knee: no compression
            target_gr = 0.0
        elif overshoot > half_knee:
            # Above knee: full compression
            target_gr = overshoot - overshoot / ratio
        else:
            # In knee region: smooth transition
            knee_factor = (overshoot + half_knee) / knee_db
            effective_ratio = 1.0 + (ratio - 1.0) * knee_factor * 0.5
            target_gr = overshoot - overshoot / effective_ratio

        # Apply ballistics: attack when increasing GR, release when decreasing
        if target_gr > state:
            state = attack_coeff * state + (1.0 - attack_coeff) * target_gr
        else:
            state = release_coeff * state + (1.0 - release_coeff) * target_gr

        gr[i] = state

    return gr, state


class DynamicCompressor:
    """
    Feed-forward dynamic range compressor with soft-knee.
    Processes stereo or mono float32 audio.

    Key parameters:
      threshold_db : Level (in dB) above which compression is applied
      ratio        : Compression ratio (e.g. 4.0 = 4:1)
      attack_ms    : Time for compressor to react to loud sounds
      release_ms   : Time for compressor to recover after loud sounds pass
      knee_db      : Width of the soft-knee transition zone
      makeup_gain_db: Output gain applied after compression
    """

    def __init__(
        self,
        threshold_db: float = -25.0,
        ratio: float = 4.0,
        attack_ms: float = 10.0,
        release_ms: float = 200.0,
        knee_db: float = 6.0,
        makeup_gain_db: float = 0.0,
        sample_rate: int = 48000,
    ):
        self.sample_rate = sample_rate
        self._enabled = True
        self._gr_state = [0.0, 0.0]  # per-channel gain reduction state
        self._detectors = [
            RMSDetector(attack_ms, release_ms, sample_rate),
            RMSDetector(attack_ms, release_ms, sample_rate),
        ]

        # Public parameters (thread-safe: Python float assignment is atomic)
        self.threshold_db = threshold_db
        self.ratio = ratio
        self.knee_db = knee_db
        self.makeup_gain_db = makeup_gain_db

        self.set_times(attack_ms, release_ms)

        # Current gain reduction for metering (updated each block)
        self.current_gr_db = 0.0

    def set_times(self, attack_ms: float, release_ms: float):
        self._attack_coeff = _time_to_coeff(attack_ms, self.sample_rate)
        self._release_coeff = _time_to_coeff(release_ms, self.sample_rate)

    def set_enabled(self, enabled: bool):
        self._enabled = enabled

    def process(self, audio: np.ndarray) -> np.ndarray:
        """
        Process audio buffer (shape: [samples, channels] or [samples]).
        Returns compressed audio of the same shape.

        Uses linked-stereo detection: the louder channel's envelope drives
        gain reduction for both channels, preserving the stereo image.
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
        envelopes_db = []
        for ch in range(active_channels):
            samples = audio[:, ch].astype(np.float32)
            envelope_linear = self._detectors[ch].process(samples)
            envelopes_db.append(linear_to_db(envelope_linear).astype(np.float32))

        # Take the max envelope across channels (linked stereo sidechain)
        if active_channels == 2:
            linked_envelope_db = np.maximum(envelopes_db[0], envelopes_db[1])
        else:
            linked_envelope_db = envelopes_db[0]

        # Compute a single gain-reduction curve from the linked envelope
        gr_db, self._gr_state[0] = _compute_gain_reduction(
            linked_envelope_db,
            float(self.threshold_db),
            float(self.ratio),
            float(self.knee_db),
            self._gr_state[0],
            self._attack_coeff,
            self._release_coeff,
        )
        # Keep both channel states in sync
        self._gr_state[1] = self._gr_state[0]

        # Apply the same gain reduction to all active channels
        gain_linear = np.power(10.0, (-gr_db + self.makeup_gain_db) / 20.0).astype(np.float32)
        for ch in range(active_channels):
            out[:, ch] = audio[:, ch].astype(np.float32) * gain_linear

        # Update meter value
        self.current_gr_db = float(np.mean(gr_db))

        if n_channels > 2:
            out[:, 2:] = audio[:, 2:]

        return out[:, 0] if mono else out

    def reset(self):
        self._gr_state = [0.0, 0.0]
        self.current_gr_db = 0.0
        for d in self._detectors:
            d.reset()
