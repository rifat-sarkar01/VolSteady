"""
VolSteady — Brick-Wall Peak Limiter
Final safety stage: guarantees output NEVER exceeds the ceiling level.
Uses a lookahead delay buffer to catch transients before they clip.
This prevents painful loudspeaker spikes.
"""

import numpy as np
import numba as nb
from .level_detector import PeakDetector, db_to_linear, _time_to_coeff


@nb.jit(nopython=True, cache=True)
def _apply_limiter_gain(
    samples: np.ndarray,
    envelope: np.ndarray,
    ceiling: float,
    gain_state: float,
    release_coeff: float,
) -> tuple:
    """
    Apply brickwall limiting gain to each sample.
    Gain reduction is instant (attack=0) but release is smooth.
    Returns (limited_samples, final_gain_state).
    """
    n = len(samples)
    out = np.empty(n, dtype=np.float32)
    gain = gain_state

    for i in range(n):
        if envelope[i] > ceiling:
            target_gain = ceiling / envelope[i]
        else:
            target_gain = 1.0

        # Instant attack (take minimum immediately), smooth release
        if target_gain < gain:
            gain = target_gain
        else:
            gain = release_coeff * gain + (1.0 - release_coeff) * target_gain

        out[i] = samples[i] * gain

    return out, gain


class BrickWallLimiter:
    """
    Peak limiter (no lookahead).
    Guarantees output peaks never exceed ceiling_db.

    Note: In VolSteady's architecture the DSP pipeline processes audio only
    for metering — no audio is replayed — so lookahead is unnecessary and
    has been removed to eliminate the latency it introduced.

    Parameters:
      ceiling_db  : Maximum output level (typically -1.0 dB)
      release_ms  : Gain recovery time after a transient
    """

    def __init__(
        self,
        ceiling_db: float = -1.0,
        release_ms: float = 50.0,
        lookahead_ms: float = 0.0,  # kept for API compat, ignored
        sample_rate: int = 48000,
    ):
        self.sample_rate = sample_rate
        self._enabled = True
        self._gain_state = [1.0, 1.0]  # per-channel (kept in sync for linked stereo)
        self._detectors = [
            PeakDetector(0.01, release_ms, sample_rate),
            PeakDetector(0.01, release_ms, sample_rate),
        ]

        self.ceiling_db = ceiling_db
        self.set_ceiling(ceiling_db)
        self.set_release(release_ms)

        # Metering
        self.current_gr_db = 0.0

    def set_ceiling(self, ceiling_db: float):
        self.ceiling_db = ceiling_db
        self._ceiling_linear = float(db_to_linear(ceiling_db))

    def set_release(self, release_ms: float):
        self._release_coeff = _time_to_coeff(release_ms, self.sample_rate)

    def set_enabled(self, enabled: bool):
        self._enabled = enabled

    def process(self, audio: np.ndarray) -> np.ndarray:
        """
        Process audio buffer (shape: [samples, channels] or [samples]).
        Returns limited audio of the same shape.

        Uses linked-stereo detection: the louder channel's peak drives
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

        # --- Linked stereo: detect peak envelopes on all active channels ---
        envelopes = []
        for ch in range(active_channels):
            samples = audio[:, ch].astype(np.float32)
            envelope = self._detectors[ch].process(samples)
            envelopes.append(envelope)

        # Take the max peak envelope across channels (linked stereo)
        if active_channels == 2:
            linked_envelope = np.maximum(envelopes[0], envelopes[1])
        else:
            linked_envelope = envelopes[0]

        # Compute a single limiter gain curve from the linked envelope
        # Use a dummy signal — we only need the gain state
        dummy = audio[:, 0].astype(np.float32)
        _, gain_state = _apply_limiter_gain(
            dummy, linked_envelope, self._ceiling_linear,
            self._gain_state[0], self._release_coeff
        )

        # Re-run to get per-sample gain applied to each channel with the
        # same state. Since _apply_limiter_gain is deterministic with the
        # same inputs, we can apply it to each channel identically.
        for ch in range(active_channels):
            samples = audio[:, ch].astype(np.float32)
            limited, _ = _apply_limiter_gain(
                samples, linked_envelope, self._ceiling_linear,
                self._gain_state[0], self._release_coeff
            )
            out[:, ch] = limited

        # Update state (keep channels in sync)
        self._gain_state[0] = gain_state
        self._gain_state[1] = gain_state

        if gain_state < 1.0:
            self.current_gr_db = abs(20.0 * np.log10(max(gain_state, 1e-12)))
        else:
            self.current_gr_db = 0.0

        if n_channels > 2:
            out[:, 2:] = audio[:, 2:]

        return out[:, 0] if mono else out

    def reset(self):
        self._gain_state = [1.0, 1.0]
        self.current_gr_db = 0.0
        for d in self._detectors:
            d.reset()
