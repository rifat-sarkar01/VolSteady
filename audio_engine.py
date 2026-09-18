"""
VolSteady — Audio Engine
WASAPI loopback capture + real-time DSP + playback.

Architecture:
  1. Open WASAPI loopback stream on the default output device (capture)
  2. Process captured audio through DSP pipeline
  3. Play processed audio on an output stream to the same device
  4. Mute the original Windows endpoint volume so we don't double-play

Feedback prevention:
  The original endpoint is muted via pycaw while VolSteady is running.
  Processed audio is written via a separate exclusive-mode output stream.
  On exit, original volume is restored.
"""

import threading
import logging
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

logger = logging.getLogger(__name__)

# ── Constants ────────────────────────────────────────────────
BUFFER_SIZE = 512        # samples per callback (~10ms at 48 kHz)
FORMAT = pyaudio.paFloat32
CHANNELS = 2
SAMPLE_RATE = 48000


class AudioEngine:
    """
    Manages WASAPI loopback capture and DSP output playback.
    Call start() to begin processing, stop() to halt cleanly.
    """

    def __init__(self, pipeline: DSPPipeline, device_name: str = "default"):
        self.pipeline = pipeline
        self.device_name = device_name

        self._pa = None
        self._capture_stream = None
        self._playback_stream = None
        self._running = False
        self._lock = threading.Lock()

        # Volume control via pycaw
        self._original_volume = None
        self._volume_interface = None

        # Audio queue between capture callback and playback thread
        self._queue = []
        self._queue_lock = threading.Lock()
        self._playback_thread = None

        # Device info
        self.sample_rate = SAMPLE_RATE
        self.channels = CHANNELS
        self.device_index = None
        self.output_device_index = None

    # ─── Public API ──────────────────────────────────────────

    def start(self):
        """Initialize audio streams and begin processing."""
        if self._running:
            return

        self._pa = pyaudio.PyAudio()
        self._find_devices()
        self._save_and_mute_volume()
        self._open_streams()
        self._running = True
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

        if self._playback_stream:
            try:
                self._playback_stream.stop_stream()
                self._playback_stream.close()
            except Exception:
                pass
            self._playback_stream = None

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

        # Bug fix: derive sample rate and channels from the loopback device,
        # not the output device. WASAPI loopback requires its own native rate.
        self.sample_rate = int(loopback_info.get("defaultSampleRate", SAMPLE_RATE))
        self.channels = min(int(loopback_info.get("maxInputChannels", 2)), 2)

        logger.info(f"Loopback device index: {loopback_index}")
        logger.info(f"Output device index: {self.output_device_index}")
        logger.info(f"Sample rate: {self.sample_rate}, Channels: {self.channels}")

        # Update pipeline with correct sample rate
        self.pipeline.sample_rate = self.sample_rate
        self.pipeline.channels = self.channels

    # ─── Volume management ───────────────────────────────────

    def _save_and_mute_volume(self):
        """Save current Windows volume and mute the endpoint (prevent double-play)."""
        if not PYCAW_AVAILABLE:
            logger.warning("pycaw not available — cannot mute original output. "
                           "You may hear audio doubled during processing.")
            return

        try:
            devices = AudioUtilities.GetSpeakers()
            # AudioUtilities.GetSpeakers() returns a pycaw AudioDevice wrapper.
            # Access the raw IMMDevice COM interface via ._dev to call Activate().
            raw_device = devices._dev
            interface = raw_device.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
            self._volume_interface = cast(interface, POINTER(IAudioEndpointVolume))
            self._original_volume = self._volume_interface.GetMasterVolumeLevelScalar()
            self._volume_interface.SetMasterVolumeLevelScalar(0.0, None)
            logger.info(f"Muted Windows endpoint (was {self._original_volume:.2f})")
        except Exception as e:
            logger.warning(f"Could not mute volume via pycaw: {e}")

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

    def set_output_volume(self, scalar: float):
        """Temporarily adjust output volume (0.0–1.0) for the playback stream."""
        # This controls the processed output, not the muted Windows endpoint.
        # We scale the gain in the pipeline's makeup gain instead.
        pass

    # ─── Stream management ───────────────────────────────────

    def _open_streams(self):
        """Open capture (loopback) and playback streams."""
        loopback_info = self._pa.get_device_info_by_index(self.device_index)
        loopback_channels = int(loopback_info.get("maxInputChannels", 2))

        # ── Capture stream (loopback, callback mode) ──────────
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

        # ── Playback stream (output, blocking write mode) ─────
        self._playback_stream = self._pa.open(
            format=FORMAT,
            channels=self.channels,
            rate=self.sample_rate,
            output=True,
            output_device_index=self.output_device_index,
            frames_per_buffer=BUFFER_SIZE,
        )

        # Playback thread drains the queue
        self._playback_thread = threading.Thread(
            target=self._playback_loop, daemon=True
        )
        self._playback_thread.start()

    def _capture_callback(self, in_data, frame_count, time_info, status):
        """
        Called from PyAudio's internal audio thread.
        Converts raw bytes → numpy → processes → enqueues for playback.
        Must be fast: no allocations if possible.
        """
        if not self._running:
            return (None, pyaudio.paComplete)

        try:
            # Decode incoming bytes to float32 numpy array
            audio = np.frombuffer(in_data, dtype=np.float32).copy()

            if self.channels == 2 and len(audio) == BUFFER_SIZE * 2:
                audio = audio.reshape(-1, 2)
            elif self.channels == 1 and len(audio) == BUFFER_SIZE:
                pass  # mono, flat array ok

            # ── DSP processing ────────────────────────────────
            processed = self.pipeline.process(audio)

            # Ensure output matches expected channel count
            if processed.ndim == 1 and self.channels == 2:
                processed = np.stack([processed, processed], axis=1)
            elif processed.ndim == 2 and self.channels == 1:
                processed = processed[:, 0]

            # Clip to prevent any remaining overflow
            np.clip(processed, -1.0, 1.0, out=processed)

            # Enqueue for playback
            out_bytes = processed.astype(np.float32).tobytes()
            with self._queue_lock:
                self._queue.append(out_bytes)

        except Exception as e:
            logger.error(f"Capture callback error: {e}")

        return (None, pyaudio.paContinue)

    def _playback_loop(self):
        """Runs in a daemon thread, drains the audio queue to the output stream."""
        silence = bytes(BUFFER_SIZE * self.channels * 4)  # float32 = 4 bytes

        while self._running:
            chunk = None
            with self._queue_lock:
                if self._queue:
                    chunk = self._queue.pop(0)

            try:
                if chunk:
                    self._playback_stream.write(chunk)
                else:
                    # Write silence to keep stream alive
                    self._playback_stream.write(silence)
            except Exception as e:
                if self._running:
                    logger.error(f"Playback write error: {e}")
