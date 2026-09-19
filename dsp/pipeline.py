"""
VolSteady — DSP Pipeline Orchestrator
Chains: Noise Gate → Compressor → Makeup Gain → Brick-Wall Limiter
Exposes a single process() method used by the audio engine callback.
"""

import numpy as np
from .gate import NoiseGate
from .compressor import DynamicCompressor
from .limiter import BrickWallLimiter
from .level_detector import linear_to_db


# ──────────────────────────────────────────────────────────────
# Preset profiles mapping "strength" (0–100) to DSP parameters
# ──────────────────────────────────────────────────────────────
def strength_to_params(strength: float) -> dict:
    """
    Map a 0–100 strength value to compressor parameters.
    strength=0   → bypass (ratio 1:1, threshold 0 dB — no compression)
    strength=50  → moderate (ratio 4.5:1, threshold -25 dB)
    strength=100 → aggressive (ratio 10:1, threshold -40 dB)
    """
    t = strength / 100.0
    return {
        "threshold_db": 0.0 - 40.0 * t,          # 0 →  0 dB,   100 → -40 dB
        "ratio": 1.0 + 9.0 * t,                   # 0 → 1:1,     100 → 10:1
        "attack_ms": 30.0 - 25.0 * t,             # 0 → 30 ms,   100 → 5 ms
        "release_ms": 400.0 - 200.0 * t,          # 0 → 400 ms,  100 → 200 ms
        "makeup_gain_db": 8.0 * t,                # 0 → 0 dB,    100 → +8 dB
    }


class DSPPipeline:
    """
    Full 4-stage dynamics processing pipeline.
    Thread-safe: parameter updates are atomic Python float assignments.
    Audio callback calls process() from a high-priority audio thread.
    """

    def __init__(self, sample_rate: int = 48000, channels: int = 2):
        self.sample_rate = sample_rate
        self.channels = channels
        self._enabled = True
        self._bypass = False

        self.gate = NoiseGate(
            threshold_db=-50.0,
            attack_ms=0.5,
            release_ms=50.0,
            hold_ms=20.0,
            sample_rate=sample_rate,
        )

        self.compressor = DynamicCompressor(
            threshold_db=-25.0,
            ratio=4.0,
            attack_ms=10.0,
            release_ms=200.0,
            knee_db=6.0,
            makeup_gain_db=0.0,
            sample_rate=sample_rate,
        )

        self.limiter = BrickWallLimiter(
            ceiling_db=-1.0,
            release_ms=50.0,
            lookahead_ms=5.0,
            sample_rate=sample_rate,
        )

        # Metering outputs (updated each block, read by GUI thread)
        self.input_level_db = -60.0
        self.output_level_db = -60.0
        self.gain_reduction_db = 0.0

        # Apply default strength
        self.set_strength(65.0)

    # ─── Parameter control ────────────────────────────────────

    def set_strength(self, strength: float):
        """Apply a preset strength (0–100) — updates compressor params."""
        params = strength_to_params(max(0.0, min(100.0, strength)))
        self.compressor.threshold_db = params["threshold_db"]
        self.compressor.ratio = params["ratio"]
        self.compressor.makeup_gain_db = params["makeup_gain_db"]
        self.compressor.set_times(params["attack_ms"], params["release_ms"])

    def set_advanced(self, **kwargs):
        """
        Set individual DSP parameters by name. Accepts:
          compressor_threshold_db, compressor_ratio, compressor_attack_ms,
          compressor_release_ms, compressor_knee_db, makeup_gain_db,
          gate_threshold_db, gate_attack_ms, gate_release_ms,
          limiter_ceiling_db, limiter_release_ms
        """
        c = self.compressor
        g = self.gate
        lim = self.limiter

        if "compressor_threshold_db" in kwargs:
            c.threshold_db = kwargs["compressor_threshold_db"]
        if "compressor_ratio" in kwargs:
            c.ratio = kwargs["compressor_ratio"]
        if "compressor_knee_db" in kwargs:
            c.knee_db = kwargs["compressor_knee_db"]
        if "makeup_gain_db" in kwargs:
            c.makeup_gain_db = kwargs["makeup_gain_db"]
        if "compressor_attack_ms" in kwargs or "compressor_release_ms" in kwargs:
            attack = kwargs.get("compressor_attack_ms", 10.0)
            release = kwargs.get("compressor_release_ms", 200.0)
            c.set_times(attack, release)

        if "gate_threshold_db" in kwargs:
            g.set_threshold(kwargs["gate_threshold_db"])
        if "gate_attack_ms" in kwargs or "gate_release_ms" in kwargs:
            a = kwargs.get("gate_attack_ms", 0.5)
            r = kwargs.get("gate_release_ms", 50.0)
            h = kwargs.get("gate_hold_ms", 20.0)
            g.set_times(a, r, h)

        if "limiter_ceiling_db" in kwargs:
            lim.set_ceiling(kwargs["limiter_ceiling_db"])
        if "limiter_release_ms" in kwargs:
            lim.set_release(kwargs["limiter_release_ms"])

    def set_bypass(self, bypass: bool):
        self._bypass = bypass

    def set_enabled(self, enabled: bool):
        self._enabled = enabled

    # ─── Core processing ──────────────────────────────────────

    def process(self, audio: np.ndarray) -> np.ndarray:
        """
        Process one buffer of audio.
        Input/output: float32 numpy array, shape [samples, channels] or [samples].
        Called from the audio callback thread — must be fast.
        """
        if not self._enabled or self._bypass or len(audio) == 0:
            return audio

        # Meter input level
        peak_in = float(np.max(np.abs(audio)))
        self.input_level_db = float(linear_to_db(np.array([peak_in]))[0])

        # Stage 1: Noise Gate
        audio = self.gate.process(audio)

        # Stage 2: Dynamic Range Compression
        audio = self.compressor.process(audio)

        # Stage 3: (Makeup gain is applied inside compressor)

        # Stage 4: Brick-wall Limiter
        audio = self.limiter.process(audio)

        # Meter output level and gain reduction
        peak_out = float(np.max(np.abs(audio)))
        self.output_level_db = float(linear_to_db(np.array([peak_out]))[0])
        self.gain_reduction_db = self.compressor.current_gr_db + self.limiter.current_gr_db

        return audio

    def set_sample_rate(self, sample_rate: int):
        """
        Update the sample rate for all DSP components, re-initializing
        their internal time constants (attack/release coefficients).
        Must be called after discovering the actual device sample rate.
        """
        if sample_rate == self.sample_rate:
            return

        self.sample_rate = sample_rate

        # Re-create the gate with new sample rate
        self.gate = NoiseGate(
            threshold_db=-50.0,
            attack_ms=0.5,
            release_ms=50.0,
            hold_ms=20.0,
            sample_rate=sample_rate,
        )

        # Re-create the compressor with new sample rate,
        # preserving current parameter settings
        old_comp = self.compressor
        self.compressor = DynamicCompressor(
            threshold_db=old_comp.threshold_db,
            ratio=old_comp.ratio,
            attack_ms=10.0,
            release_ms=200.0,
            knee_db=old_comp.knee_db,
            makeup_gain_db=old_comp.makeup_gain_db,
            sample_rate=sample_rate,
        )

        # Re-create the limiter with new sample rate
        self.limiter = BrickWallLimiter(
            ceiling_db=self.limiter.ceiling_db,
            release_ms=50.0,
            lookahead_ms=5.0,
            sample_rate=sample_rate,
        )

    def reset(self):
        self.gate.reset()
        self.compressor.reset()
        self.limiter.reset()
        self.input_level_db = -60.0
        self.output_level_db = -60.0
        self.gain_reduction_db = 0.0
