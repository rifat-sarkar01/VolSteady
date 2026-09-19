"""
VolSteady — Audio Engine (Volume Envelope Rider)

Architecture:
  1. Capture system audio via WASAPI loopback (read-only, for level monitoring)
  2. Analyse loudness using the DSP pipeline's level detector
  3. Compute a target volume scalar that tames loud passages and lifts quiet ones
  4. Apply that scalar to the Windows endpoint volume via pycaw

This design has NO feedback loop because we never play audio back — we only
observe levels and adjust the system volume knob in real time.
"""

import threading
import logging
import time
import numpy as np
import pyaudiowpatch as pyaudio
from ctypes import cast, POINTER
from comtypes import CLSCTX_ALL

try:
    from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume
    PYCAW_AVAILABLE = True
except ImportError:
    PYCAW_AVAILABLE = False

from dsp.pipeline import DSPPipeline
from dsp.level_detector import RMSDetector, linear_to_db

logger = logging.getLogger(__name__)

# ── Constants ────────────────────────────────────────────────
BUFFER_SIZE = 512        # samples per callback (~10 ms at 48 kHz)
FORMAT = pyaudio.paFloat32
CHANNELS = 2
SAMPLE_RATE = 48000

# Volume-rider parameters
VOLUME_UPDATE_INTERVAL_S = 0.010   # Update volume every ~10 ms (was 30 ms — perceptible lag)
TARGET_LOUDNESS_DB = -18.0         # Target RMS loudness in dBFS
MAX_GAIN_DB = 12.0                 # Maximum boost (dB)
MIN_GAIN_DB = -24.0                # Maximum cut (dB)
SMOOTHING_ATTACK = 0.05            # Fast response to loud signals (was 0.15 — too slow)
SMOOTHING_RELEASE = 0.92           # Smooth recovery after loud signals (was 0.985 — ~2 s lag)

# Tolerance for detecting user-initiated volume changes vs. our own writes
VOLUME_CHANGE_EPSILON = 0.015


class AudioEngine:
    """
    Volume-envelope-rider audio engine.

    Monitors system audio via WASAPI loopback (capture only) and adjusts the
    Windows endpoint master volume in real-time to stabilise loudness.
    No audio is re-played, so there is no feedback loop.

    Call start() to begin processing, stop() to halt cleanly.
    """

    def __init__(self, pipeline: DSPPipeline, device_name: str = "default"):
        self.pipeline = pipeline
        self.device_name = device_name

        self._pa = None
        self._capture_stream = None
        self._running = False
        self._lock = threading.Lock()

        # Volume control via pycaw
        self._original_volume = None
        self._volume_interface = None
        self._user_volume = 1.0       # Volume scalar the user had before we started
        self._last_written_volume = None  # Track what we last wrote to detect user changes

        # Envelope-rider state
        self._current_rms_db = -60.0   # Current measured RMS level
        self._smooth_gain = 1.0        # Smoothed gain multiplier applied to volume
        self._level_detector = RMSDetector(attack_ms=5.0, release_ms=150.0, sample_rate=SAMPLE_RATE)
        self._volume_thread = None
        self._level_lock = threading.Lock()

        # Device info
        self.sample_rate = SAMPLE_RATE
        self.channels = CHANNELS
        self.device_index = None
        self.output_device_index = None

    # ─── Public API ──────────────────────────────────────────

    def start(self):
        """Initialize audio capture and begin volume riding."""
        if self._running:
            return

        self._pa = pyaudio.PyAudio()
        self._find_devices()
        self._save_user_volume()
        self._open_capture_stream()
        self._running = True

        # Start the volume-adjustment thread
        self._volume_thread = threading.Thread(
            target=self._volume_rider_loop, daemon=True, name="VolSteady-rider"
        )
        self._volume_thread.start()

        logger.info(f"AudioEngine started — device: {self.device_name}, "
                    f"rate: {self.sample_rate}, channels: {self.channels}")

    def stop(self):
        """Stop processing and restore original system volume."""
        if not self._running:
            return

        self._running = False

        if self._capture_stream:
            try:
                self._capture_stream.stop_stream()
                self._capture_stream.close()
            except Exception:
                pass
            self._capture_stream = None

        if self._volume_thread and self._volume_thread.is_alive():
            self._volume_thread.join(timeout=2.0)

        if self._pa:
            self._pa.terminate()
            self._pa = None

        self._restore_volume()
        logger.info("AudioEngine stopped.")

    def get_available_output_devices(self) -> list:
        """Return list of (name, index) for WASAPI output devices."""
        devices = []
        if not self._pa:
            pa = pyaudio.PyAudio()
        else:
            pa = self._pa

        try:
            wasapi_info = pa.get_host_api_info_by_type(pyaudio.paWASAPI)
        except OSError:
            return devices

        for i in range(pa.get_device_count()):
            info = pa.get_device_info_by_index(i)
            if (info.get("hostApi") == wasapi_info["index"]
                    and info.get("maxOutputChannels", 0) > 0):
                devices.append((info["name"], i))

        if not self._pa:
            pa.terminate()

        return devices

    @property
    def current_rms_db(self) -> float:
        """Current measured RMS level (dBFS). Thread-safe read."""
        with self._level_lock:
            return self._current_rms_db

    @property
    def current_gain_scalar(self) -> float:
        """Current smoothed gain multiplier being applied. Thread-safe read."""
        with self._level_lock:
            return self._smooth_gain

    # ─── Device discovery ────────────────────────────────────

    def _find_devices(self):
        """Find the loopback capture device and the output device."""
        try:
            wasapi_info = self._pa.get_host_api_info_by_type(pyaudio.paWASAPI)
        except OSError:
            raise RuntimeError("WASAPI not available on this system.")

        # Find default output device
        default_output = self._pa.get_default_output_device_info()
        self.output_device_index = default_output["index"]

        # Find the WASAPI loopback counterpart for that output device
        loopback_index = None
        loopback_info = None
        for i in range(self._pa.get_device_count()):
            info = self._pa.get_device_info_by_index(i)
            if (info.get("hostApi") == wasapi_info["index"]
                    and info.get("isLoopbackDevice", False)
                    and info.get("name", "").startswith(default_output.get("name", "")[:20])):
                loopback_index = i
                loopback_info = info
                break

        # Fallback: use PyAudioWPatch helper
        if loopback_index is None:
            try:
                for loopback in self._pa.get_loopback_device_info_generator():
                    if loopback["index"] != self.output_device_index:
                        loopback_index = loopback["index"]
                        loopback_info = loopback
                        break
            except Exception:
                pass

        if loopback_index is None:
            raise RuntimeError(
                "Could not find WASAPI loopback device. "
                "Make sure audio is playing or a sound device is active."
            )

        self.device_index = loopback_index

        # Derive sample rate and channels from the loopback device.
        # WASAPI loopback requires its own native rate.
        self.sample_rate = int(loopback_info.get("defaultSampleRate", SAMPLE_RATE))
        self.channels = min(int(loopback_info.get("maxInputChannels", 2)), 2)

        logger.info(f"Loopback device index: {loopback_index}")
        logger.info(f"Output device index: {self.output_device_index}")
        logger.info(f"Sample rate: {self.sample_rate}, Channels: {self.channels}")

        # Update pipeline and level detector with correct sample rate (Bug #6 fix)
        self.pipeline.set_sample_rate(self.sample_rate)
        self.pipeline.channels = self.channels
        self._level_detector = RMSDetector(
            attack_ms=5.0, release_ms=150.0, sample_rate=self.sample_rate
        )

    # ─── Volume management ───────────────────────────────────

    def _save_user_volume(self):
        """Save the user's current Windows volume so we can restore it on exit."""
        if not PYCAW_AVAILABLE:
            logger.warning("pycaw not available — volume riding disabled. "
                           "Install pycaw for VolSteady to function.")
            return

        try:
            devices = AudioUtilities.GetSpeakers()
            # Access the raw IMMDevice COM interface via ._dev to call Activate().
            raw_device = devices._dev
            interface = raw_device.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
            self._volume_interface = cast(interface, POINTER(IAudioEndpointVolume))
            self._original_volume = self._volume_interface.GetMasterVolumeLevelScalar()
            self._user_volume = self._original_volume
            logger.info(f"Saved user volume: {self._original_volume:.2f}")
        except Exception as e:
            logger.warning(f"Could not read volume via pycaw: {e}")

    def _restore_volume(self):
        """Restore original Windows volume on exit."""
        if self._volume_interface and self._original_volume is not None:
            try:
                self._volume_interface.SetMasterVolumeLevelScalar(
                    self._original_volume, None
                )
                logger.info(f"Restored volume to {self._original_volume:.2f}")
            except Exception as e:
                logger.warning(f"Could not restore volume: {e}")
        self._volume_interface = None
        self._original_volume = None

    def _set_endpoint_volume(self, scalar: float):
        """Set the Windows endpoint volume (0.0–1.0)."""
        if self._volume_interface is None:
            return
        # Clamp to valid range
        scalar = max(0.0, min(1.0, scalar))
        try:
            self._volume_interface.SetMasterVolumeLevelScalar(scalar, None)
        except Exception as e:
            logger.debug(f"Volume set error: {e}")

    # ─── Capture stream ──────────────────────────────────────

    def _open_capture_stream(self):
        """Open loopback capture stream (read-only, for level monitoring)."""
        loopback_info = self._pa.get_device_info_by_index(self.device_index)
        loopback_channels = int(loopback_info.get("maxInputChannels", 2))

        self._capture_stream = self._pa.open(
            format=FORMAT,
            channels=min(loopback_channels, 2),
            rate=self.sample_rate,
            input=True,
            input_device_index=self.device_index,
            frames_per_buffer=BUFFER_SIZE,
            stream_callback=self._capture_callback,
        )
        self._capture_stream.start_stream()

    def _capture_callback(self, in_data, frame_count, time_info, status):
        """
        Called from PyAudio's internal audio thread.
        Analyses the audio level and stores it for the volume rider thread.
        Must be fast — no blocking, no I/O.
        """
        if not self._running:
            return (None, pyaudio.paComplete)

        try:
            # Decode incoming bytes to float32 numpy array
            audio = np.frombuffer(in_data, dtype=np.float32).copy()

            # Bug #3 fix: handle variable-length WASAPI loopback buffers
            # Don't assume len(audio) == BUFFER_SIZE * channels
            actual_samples = len(audio)
            if self.channels == 2 and actual_samples >= 2:
                # Ensure even number of samples for stereo reshape
                actual_samples = actual_samples - (actual_samples % 2)
                audio = audio[:actual_samples]
                # Convert to mono by averaging channels for level detection
                stereo = audio.reshape(-1, 2)
                mono = (stereo[:, 0] + stereo[:, 1]) * 0.5
            else:
                mono = audio

            # Level detection via RMS envelope
            envelope = self._level_detector.process(mono.astype(np.float32))
            rms_linear = float(np.mean(envelope)) if len(envelope) > 0 else 1e-12
            rms_db = float(linear_to_db(np.array([max(rms_linear, 1e-12)]))[0])

            # Also feed the DSP pipeline for metering (input/output level display)
            if self.channels == 2 and actual_samples >= 2:
                self.pipeline.process(stereo)
            else:
                self.pipeline.process(mono)

            with self._level_lock:
                self._current_rms_db = rms_db

        except Exception as e:
            logger.error(f"Capture callback error: {e}")

        return (None, pyaudio.paContinue)

    # ─── Volume rider ────────────────────────────────────────

    def _read_current_volume(self) -> float | None:
        """Read the current Windows endpoint volume. Returns None on failure."""
        if self._volume_interface is None:
            return None
        try:
            return float(self._volume_interface.GetMasterVolumeLevelScalar())
        except Exception:
            return None

    def _volume_rider_loop(self):
        """
        Runs in a daemon thread. Periodically reads the measured RMS level
        and adjusts the Windows endpoint volume to keep loudness near the
        target level.

        The algorithm:
          1. Detect if the user changed volume externally → adopt new baseline
          2. Compute error = TARGET_LOUDNESS_DB - current_rms_db
          3. Convert error to a gain scalar
          4. Smooth the gain with asymmetric attack/release
          5. Multiply by user's baseline volume to get final endpoint volume
          6. Write to Windows via pycaw
        """
        logger.info("Volume rider thread started.")

        while self._running:
            try:
                # ── Detect user volume changes ─────────────────────────
                # If the current system volume differs from what we last
                # wrote, the user must have changed it (keyboard, mixer).
                # Adopt their new volume as our baseline.
                current_sys_vol = self._read_current_volume()
                if (current_sys_vol is not None
                        and self._last_written_volume is not None):
                    delta = abs(current_sys_vol - self._last_written_volume)
                    if delta > VOLUME_CHANGE_EPSILON:
                        logger.debug(
                            f"User volume change detected: "
                            f"{self._last_written_volume:.3f} → {current_sys_vol:.3f}"
                        )
                        self._user_volume = current_sys_vol

                with self._level_lock:
                    rms_db = self._current_rms_db
                    prev_gain = self._smooth_gain

                # Don't adjust when signal is very quiet (below noise gate)
                if rms_db < -55.0:
                    # Signal is basically silence — hold current gain
                    time.sleep(VOLUME_UPDATE_INTERVAL_S)
                    continue

                # Compute desired gain correction in dB
                error_db = TARGET_LOUDNESS_DB - rms_db
                # Clamp to sane range
                desired_gain_db = max(MIN_GAIN_DB, min(MAX_GAIN_DB, error_db))
                desired_gain = 10.0 ** (desired_gain_db / 20.0)

                # Asymmetric smoothing: attack fast (loud → turn down quickly),
                # release slow (quiet → turn up gradually)
                if desired_gain < prev_gain:
                    # Getting louder → need to reduce volume quickly
                    alpha = SMOOTHING_ATTACK
                else:
                    # Getting quieter → increase volume slowly
                    alpha = SMOOTHING_RELEASE

                smooth_gain = alpha * prev_gain + (1.0 - alpha) * desired_gain

                # Compute final volume scalar for Windows endpoint
                final_volume = self._user_volume * smooth_gain
                final_volume = max(0.0, min(1.0, final_volume))

                # Apply to Windows and track what we wrote
                self._set_endpoint_volume(final_volume)
                self._last_written_volume = final_volume

                with self._level_lock:
                    self._smooth_gain = smooth_gain

                # Update pipeline metering for the GUI
                self.pipeline.gain_reduction_db = -desired_gain_db

            except Exception as e:
                logger.error(f"Volume rider error: {e}")

            time.sleep(VOLUME_UPDATE_INTERVAL_S)

        logger.info("Volume rider thread stopped.")
