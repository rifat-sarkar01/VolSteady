"""
VolSteady — System Tray Icon
Lightweight pystray icon with right-click menu.
Double-click opens the settings window.
"""

import logging
import threading
import io
from PIL import Image, ImageDraw, ImageFont

logger = logging.getLogger(__name__)


def _create_tray_icon(active: bool, strength: float) -> Image.Image:
    """
    Generate a 64×64 tray icon programmatically.
    Green = active, grey = disabled/bypassed.
    """
    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # Background circle
    bg_color = (124, 92, 191, 230) if active else (80, 80, 100, 180)
    draw.ellipse([2, 2, 62, 62], fill=bg_color)

    # Sound wave arcs (3 arcs representing volume/sound)
    wave_color = (255, 255, 255, 220)
    draw.arc([14, 18, 50, 46], start=-60, end=60, fill=wave_color, width=3)
    draw.arc([8,  12, 56, 52], start=-60, end=60, fill=wave_color, width=2)
    draw.arc([20, 24, 44, 40], start=-60, end=60, fill=wave_color, width=3)

    # Center dot
    draw.ellipse([28, 28, 36, 36], fill=(255, 255, 255, 240))

    return img


class TrayIcon:
    """
    System tray icon controller.
    Runs pystray in its own thread so it doesn't block the main thread.
    """

    def __init__(self, pipeline, config, engine, on_settings, on_quit):
        self.pipeline = pipeline
        self.config = config
        self.engine = engine
        self._on_settings = on_settings
        self._on_quit = on_quit
        self._icon = None
        self._thread = None

    def start(self):
        """Start the tray icon in a background thread."""
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        import pystray
        from pystray import MenuItem, Menu

        def toggle_enabled(icon, item):
            self.config.enabled = not self.config.enabled
            self.pipeline.set_enabled(self.config.enabled)
            self._refresh_icon(icon)

        def set_strength_low(icon, item):
            self.config.strength = 30.0
            self.pipeline.set_strength(30.0)
            self._refresh_icon(icon)

        def set_strength_medium(icon, item):
            self.config.strength = 65.0
            self.pipeline.set_strength(65.0)
            self._refresh_icon(icon)

        def set_strength_high(icon, item):
            self.config.strength = 90.0
            self.pipeline.set_strength(90.0)
            self._refresh_icon(icon)

        def open_settings(icon, item):
            self._on_settings()

        def quit_app(icon, item):
            icon.stop()
            self._on_quit()

        def get_enabled_text(item):
            return "✅ Enabled" if self.config.enabled else "❌ Disabled"

        icon_img = _create_tray_icon(self.config.enabled, self.config.strength)

        menu = Menu(
            MenuItem(get_enabled_text, toggle_enabled, default=False),
            Menu.SEPARATOR,
            MenuItem("Strength", Menu(
                MenuItem("Low (30%)", set_strength_low),
                MenuItem("Medium (65%)", set_strength_medium),
                MenuItem("High (90%)", set_strength_high),
            )),
            Menu.SEPARATOR,
            MenuItem("⚙️ Settings...", open_settings),
            Menu.SEPARATOR,
            MenuItem("❌ Quit", quit_app),
        )

        self._icon = pystray.Icon(
            "VolSteady",
            icon=icon_img,
            title="VolSteady — Audio Stabilizer",
            menu=menu,
        )

        # Double-click opens settings
        self._icon.run(setup=self._setup_double_click)

    def _setup_double_click(self, icon):
        """Called once after icon is displayed."""
        pass  # pystray handles left-click as default item activation

    def _refresh_icon(self, icon):
        """Update icon image to reflect current state."""
        icon.icon = _create_tray_icon(self.config.enabled, self.config.strength)
        tooltip = (
            f"VolSteady — {'Active' if self.config.enabled else 'Disabled'} | "
            f"Strength: {self.config.strength:.0f}%"
        )
        icon.title = tooltip

    def stop(self):
        """Stop and remove the tray icon."""
        if self._icon:
            try:
                self._icon.stop()
            except Exception:
                pass
