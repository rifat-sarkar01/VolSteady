"""
VolSteady — Brick-Wall Peak Limiter
Final safety stage: guarantees output NEVER exceeds the ceiling level.
Uses a lookahead delay buffer to catch transients before they clip.
This prevents painful loudspeaker spikes.
"""

import numpy as np
import numba as nb
from collections import deque
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
    Lookahead peak limiter.
    Guarantees output peaks never exceed ceiling_db.

    Parameters:
      ceiling_db  : Maximum output level (typically -1.0 dB)
      release_ms  : Gain recovery time after a transient
      lookahead_ms: Delay buffer size to anticipate peaks
    """

    def __init__(
        self,
        ceiling_db: float = -1.0,
        release_ms: float = 50.0,
        lookahead_ms: float = 5.0,
        sample_rate: int = 48000,
    ):
        self.sample_rate = sample_rate
        self._enabled = True
        self._gain_state = [1.0, 1.0]  # per-channel
        self._detectors = [
            PeakDetector(0.01, release_ms, sample_rate),
            PeakDetector(0.01, release_ms, sample_rate),
        ]

        # Lookahead delay buffers (one per channel, max 2)
        lookahead_samples = int((lookahead_ms / 1000.0) * sample_rate)
        self._lookahead_samples = lookahead_samples
        self._delay_bufs = [
            deque(np.zeros(lookahead_samples, dtype=np.float32), maxlen=lookahead_samples),
            deque(np.zeros(lookahead_samples, dtype=np.float32), maxlen=lookahead_samples),
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

    def _process_channel(self, samples: np.ndarray, ch: int) -> np.ndarray:
        """Process a single channel with lookahead."""
        lookahead = self._lookahead_samples
        delay_buf = self._delay_bufs[ch]
        detector = self._detectors[ch]

        if lookahead == 0:
            envelope = detector.process(samples)
            out, self._gain_state[ch] = _apply_limiter_gain(
                samples, envelope, self._ceiling_linear,
                self._gain_state[ch], self._release_coeff
            )
            return out

        # Build delayed version: delay_buf holds the oldest samples
        delayed_samples = np.empty(len(samples), dtype=np.float32)
        for i, s in enumerate(samples):
            delayed_samples[i] = delay_buf[0]
            delay_buf.append(s)

        # Detect peaks on the CURRENT (un-delayed) signal
        envelope = detector.process(samples)

        # Apply gain computed from current to delayed output
        out, self._gain_state[ch] = _apply_limiter_gain(
            delayed_samples, envelope, self._ceiling_linear,
            self._gain_state[ch], self._release_coeff
        )
        return out

    def process(self, audio: np.ndarray) -> np.ndarray:
        """
        Process audio buffer (shape: [samples, channels] or [samples]).
        Returns limited audio of the same shape.
        """
        if not self._enabled:
            return audio

        mono = audio.ndim == 1
        if mono:
            audio = audio[:, np.newaxis]

        n_channels = audio.shape[1]
        out = np.empty_like(audio)
        total_gr = 0.0

        for ch in range(min(n_channels, 2)):
            samples = audio[:, ch].astype(np.float32)
            limited = self._process_channel(samples, ch)
            out[:, ch] = limited

            if self._gain_state[ch] < 1.0:
                total_gr += abs(20.0 * np.log10(max(self._gain_state[ch], 1e-12)))

        self.current_gr_db = total_gr / min(n_channels, 2)

        if n_channels > 2:
            out[:, 2:] = audio[:, 2:]

        return out[:, 0] if mono else out

    def reset(self):
        self._gain_state = [1.0, 1.0]
        self.current_gr_db = 0.0
        for d in self._detectors:
            d.reset()
        for buf in self._delay_bufs:
            buf.clear()
            buf.extend(np.zeros(self._lookahead_samples, dtype=np.float32))
