"""
VolSteady — Main Entry Point
Real-time system-wide audio stabilizer for Windows.

Usage:
    python main.py                  # Normal launch (minimized to tray)
    python main.py --show           # Launch with settings window open
    python main.py --debug          # Enable debug logging
    python main.py --strength 80    # Set initial strength (0-100)
"""

import sys
import argparse
import logging
import signal
import threading
import time

from utils import setup_logging, acquire_single_instance_lock, release_single_instance_lock
from config import Config
from dsp.pipeline import DSPPipeline
from audio_engine import AudioEngine
from gui.tray import TrayIcon
from gui.settings_window import SettingsWindow

logger = logging.getLogger(__name__)


class VolSteadyApp:
    """Main application controller."""

    def __init__(self, args):
        self.args = args
        self._shutdown_event = threading.Event()
        self._shutdown_done = False

        # Core components
        self.config = Config()
        self.pipeline = DSPPipeline(sample_rate=48000, channels=2)
        self.engine = AudioEngine(self.pipeline, self.config.audio_device)
        self.tray = None
        self.settings_win = None

        # Apply saved settings to pipeline
        self._apply_config_to_pipeline()

    def _apply_config_to_pipeline(self):
        """Sync config → DSP pipeline on startup."""
        self.pipeline.set_enabled(self.config.enabled)

        if self.config.use_advanced:
            adv = self.config.get_advanced()
            self.pipeline.set_advanced(**adv)
        else:
            self.pipeline.set_strength(self.config.strength)

        if self.args.strength is not None:
            self.pipeline.set_strength(self.args.strength)
            self.config.strength = self.args.strength

    def run(self):
        """Start the application."""
        logger.info("VolSteady starting...")

        # Start DSP audio engine
        try:
            self.engine.start()
        except Exception as e:
            logger.error(f"Failed to start audio engine: {e}")
            self._show_error(str(e))
            return

        # Create settings window (shared instance, shown on demand)
        self.settings_win = SettingsWindow(
            pipeline=self.pipeline,
            config=self.config,
            engine=self.engine,
            on_close=None,
        )

        # Create system tray
        self.tray = TrayIcon(
            pipeline=self.pipeline,
            config=self.config,
            engine=self.engine,
            on_settings=self._open_settings,
            on_quit=self.shutdown,
        )

        # Start tray icon (this blocks in its own thread)
        self.tray.start()

        logger.info("VolSteady running — check system tray.")

        # Show settings window if requested (tkinter must run on the main thread)
        if self.args.show:
            self._open_settings()

        # Keep main thread alive, watching for shutdown.
        # Use a tkinter event loop if a settings window is open,
        # otherwise use a simple sleep loop.
        try:
            while not self._shutdown_event.is_set():
                time.sleep(0.5)
        except KeyboardInterrupt:
            pass

        self.shutdown()

    def _open_settings(self):
        """Open the settings window on the main thread (tkinter requires it on Windows)."""
        self.settings_win.show()

    def shutdown(self):
        """Graceful shutdown: stop audio, restore volume, exit."""
        if self._shutdown_done:
            return
        self._shutdown_done = True

        logger.info("Shutting down VolSteady...")

        if self.tray:
            try:
                self.tray.stop()
            except Exception as e:
                logger.warning(f"Tray stop error: {e}")

        if self.engine:
            self.engine.stop()  # Restores original Windows volume

        # Wait for playback thread to drain (if it exists)
        if (self.engine and self.engine._playback_thread
                and self.engine._playback_thread.is_alive()):
            self.engine._playback_thread.join(timeout=2.0)

        release_single_instance_lock()
        self._shutdown_event.set()

        logger.info("Shutdown complete.")

    def _show_error(self, message: str):
        """Show a simple error popup using tkinter."""
        try:
            import tkinter as tk
            from tkinter import messagebox
            root = tk.Tk()
            root.withdraw()
            messagebox.showerror(
                "VolSteady — Error",
                f"Could not start audio engine:\n\n{message}\n\n"
                "Please check that your audio device is connected and try again."
            )
            root.destroy()
        except Exception:
            print(f"ERROR: {message}", file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(
        description="VolSteady — Real-time system-wide audio stabilizer"
    )
    parser.add_argument("--show", action="store_true",
                        help="Open settings window on launch")
    parser.add_argument("--debug", action="store_true",
                        help="Enable debug logging")
    parser.add_argument("--strength", type=float, default=None,
                        help="Initial strength 0–100 (overrides saved setting)")
    args = parser.parse_args()

    # Setup logging
    log_file = setup_logging(debug=args.debug)
    logger.info(f"Log file: {log_file}")

    # Single-instance check
    if not acquire_single_instance_lock():
        logger.warning("Another instance of VolSteady is already running.")
        try:
            import tkinter as tk
            from tkinter import messagebox
            root = tk.Tk()
            root.withdraw()
            messagebox.showwarning(
                "VolSteady",
                "VolSteady is already running.\nCheck your system tray."
            )
            root.destroy()
        except Exception:
            pass
        sys.exit(1)

    # Handle Ctrl+C — signal the app to shut down gracefully
    # instead of calling sys.exit() which would skip cleanup.
    app = VolSteadyApp(args)

    def _sigint_handler(sig, frame):
        logger.info("Received SIGINT — shutting down.")
        app._shutdown_event.set()

    signal.signal(signal.SIGINT, _sigint_handler)

    # Run the app
    app.run()


if __name__ == "__main__":
    main()
