"""
VolSteady — Settings Window
Modern, dark-themed tkinter settings panel with:
  - Master strength slider
  - Real-time level meters (input, output, gain reduction)
  - Collapsible advanced controls
  - Device selector
  - Bypass toggle
"""

import tkinter as tk
from tkinter import ttk, font as tkfont
import threading
import logging

logger = logging.getLogger(__name__)

# ── Color palette ────────────────────────────────────────────
BG          = "#0f0f14"
BG_PANEL    = "#1a1a24"
BG_WIDGET   = "#24243a"
ACCENT      = "#7c5cbf"
ACCENT_LITE = "#a87dd6"
GREEN       = "#2ecc71"
YELLOW      = "#f0c040"
RED         = "#e74c3c"
TEXT        = "#e0e0f0"
TEXT_DIM    = "#8888aa"
BORDER      = "#33334a"


class LevelMeter(tk.Canvas):
    """Vertical or horizontal bar-graph level meter."""

    def __init__(self, parent, width=20, height=120, min_db=-60, max_db=0,
                 orientation="vertical", **kwargs):
        super().__init__(parent, width=width, height=height,
                         bg=BG_WIDGET, highlightthickness=0, **kwargs)
        self._w = width
        self._h = height
        self._min_db = min_db
        self._max_db = max_db
        self._orientation = orientation
        self._level_db = min_db
        self._draw()

    def set_level(self, db: float):
        self._level_db = max(self._min_db, min(self._max_db, db))
        self._draw()

    def _db_to_fraction(self, db: float) -> float:
        return (db - self._min_db) / (self._max_db - self._min_db)

    def _fraction_to_color(self, frac: float) -> str:
        if frac < 0.7:
            return GREEN
        elif frac < 0.9:
            return YELLOW
        else:
            return RED

    def _draw(self):
        self.delete("all")
        frac = self._db_to_fraction(self._level_db)
        color = self._fraction_to_color(frac)

        if self._orientation == "vertical":
            bar_h = int(self._h * frac)
            # Background segments
            seg_count = 20
            seg_h = self._h / seg_count
            for i in range(seg_count):
                y0 = self._h - (i + 1) * seg_h + 1
                y1 = self._h - i * seg_h - 1
                f = i / seg_count
                c = GREEN if f < 0.7 else (YELLOW if f < 0.9 else RED)
                alpha_color = c if (i / seg_count) < frac else "#1a1a2e"
                self.create_rectangle(2, y0, self._w - 2, y1,
                                      fill=alpha_color, outline="")
        else:
            seg_count = 30
            seg_w = self._w / seg_count
            for i in range(seg_count):
                x0 = i * seg_w + 1
                x1 = (i + 1) * seg_w - 1
                f = i / seg_count
                c = GREEN if f < 0.7 else (YELLOW if f < 0.9 else RED)
                alpha_color = c if (i / seg_count) < frac else "#1a1a2e"
                self.create_rectangle(x0, 2, x1, self._h - 2,
                                      fill=alpha_color, outline="")


class SettingsWindow:
    """
    Compact settings window (~440×560px).
    Opened from the system tray icon.
    """

    def __init__(self, pipeline, config, engine, on_close=None):
        self.pipeline = pipeline
        self.config = config
        self.engine = engine
        self._on_close = on_close
        self._root = None
        self._meter_running = False
        self._advanced_visible = False

    def show(self):
        """Create and display the settings window."""
        if self._root and self._root.winfo_exists():
            self._root.lift()
            self._root.focus_force()
            return

        self._root = tk.Tk()
        self._root.title("VolSteady")
        self._root.geometry("440x600")
        self._root.resizable(False, False)
        self._root.configure(bg=BG)
        self._root.protocol("WM_DELETE_WINDOW", self._on_window_close)

        # Try to set icon
        try:
            self._root.iconbitmap("assets/icon.ico")
        except Exception:
            pass

        self._build_ui()
        self._start_meter_updates()
        self._root.mainloop()

    def _on_window_close(self):
        self._meter_running = False
        if self._root:
            self._root.destroy()
            self._root = None
        if self._on_close:
            self._on_close()

    # ─── UI construction ──────────────────────────────────────

    def _build_ui(self):
        root = self._root

        # ── Header ────────────────────────────────────────────
        header = tk.Frame(root, bg=ACCENT, height=56)
        header.pack(fill="x")
        header.pack_propagate(False)

        tk.Label(
            header, text="🔊  VolSteady",
            font=("Segoe UI", 18, "bold"), fg="white", bg=ACCENT
        ).pack(side="left", padx=18, pady=12)

        self._bypass_var = tk.BooleanVar(value=False)
        bypass_btn = tk.Checkbutton(
            header, text="BYPASS", variable=self._bypass_var,
            command=self._on_bypass_toggle,
            font=("Segoe UI", 9, "bold"),
            fg=TEXT, bg=ACCENT, selectcolor=BG_WIDGET,
            activebackground=ACCENT, activeforeground="white",
            cursor="hand2"
        )
        bypass_btn.pack(side="right", padx=18)

        # ── Main content ──────────────────────────────────────
        content = tk.Frame(root, bg=BG)
        content.pack(fill="both", expand=True, padx=16, pady=12)

        # ── Status row ────────────────────────────────────────
        status_row = tk.Frame(content, bg=BG)
        status_row.pack(fill="x", pady=(0, 8))

        self._status_label = tk.Label(
            status_row, text="● Active",
            font=("Segoe UI", 10, "bold"),
            fg=GREEN, bg=BG
        )
        self._status_label.pack(side="left")

        self._gr_label = tk.Label(
            status_row, text="GR: 0.0 dB",
            font=("Segoe UI", 9), fg=TEXT_DIM, bg=BG
        )
        self._gr_label.pack(side="right")

        # ── Level meters ──────────────────────────────────────
        meters_frame = tk.Frame(content, bg=BG_PANEL, relief="flat")
        meters_frame.pack(fill="x", pady=(0, 12))
        self._build_meters(meters_frame)

        # ── Strength slider ───────────────────────────────────
        strength_panel = tk.Frame(content, bg=BG_PANEL)
        strength_panel.pack(fill="x", pady=(0, 10))
        self._build_strength_slider(strength_panel)

        # ── Enabled toggle ────────────────────────────────────
        ctrl_row = tk.Frame(content, bg=BG)
        ctrl_row.pack(fill="x", pady=(0, 8))

        self._enabled_var = tk.BooleanVar(value=self.config.enabled)
        enabled_chk = tk.Checkbutton(
            ctrl_row, text="  Enable VolSteady",
            variable=self._enabled_var,
            command=self._on_enabled_toggle,
            font=("Segoe UI", 10),
            fg=TEXT, bg=BG, selectcolor=BG_WIDGET,
            activebackground=BG, activeforeground=TEXT,
            cursor="hand2"
        )
        enabled_chk.pack(side="left")

        # ── Advanced toggle ───────────────────────────────────
        self._adv_btn = tk.Button(
            ctrl_row, text="▶  Advanced",
            command=self._toggle_advanced,
            font=("Segoe UI", 9),
            fg=ACCENT_LITE, bg=BG, activebackground=BG,
            activeforeground=ACCENT_LITE,
            bd=0, cursor="hand2", relief="flat"
        )
        self._adv_btn.pack(side="right")

        # ── Advanced panel (collapsible) ──────────────────────
        self._adv_frame = tk.Frame(content, bg=BG_PANEL)
        self._build_advanced_panel(self._adv_frame)

        # ── Device selector ───────────────────────────────────
        dev_frame = tk.Frame(content, bg=BG)
        dev_frame.pack(fill="x", pady=(8, 0), side="bottom")
        self._build_device_selector(dev_frame)

        # ── Footer ────────────────────────────────────────────
        footer = tk.Frame(content, bg=BG)
        footer.pack(fill="x", side="bottom")
        tk.Label(
            footer, text="VolSteady v1.0 — Real-time Audio Stabilizer",
            font=("Segoe UI", 8), fg=TEXT_DIM, bg=BG
        ).pack(pady=4)

    def _build_meters(self, parent):
        parent.configure(padx=12, pady=10)

        labels = ["INPUT", "OUTPUT", "G.R."]
        self._input_meter = LevelMeter(parent, width=26, height=90)
        self._output_meter = LevelMeter(parent, width=26, height=90)
        self._gr_meter = LevelMeter(parent, width=26, height=90,
                                    min_db=0, max_db=20)

        meters = [self._input_meter, self._output_meter, self._gr_meter]
        colors = [GREEN, ACCENT_LITE, YELLOW]

        # Store column frames so we can pack dB labels underneath
        cols = []
        for i, (meter, lbl, clr) in enumerate(zip(meters, labels, colors)):
            col = tk.Frame(parent, bg=BG_PANEL)
            col.pack(side="left", expand=True)
            tk.Label(col, text=lbl, font=("Segoe UI", 7, "bold"),
                     fg=clr, bg=BG_PANEL).pack(pady=(0, 4))
            meter_container = tk.Frame(col, bg=BG_PANEL)
            meter_container.pack()
            meter.pack(in_=meter_container)
            cols.append(col)

        # Pack dB readout labels under the INPUT and OUTPUT columns
        self._in_db_lbl = tk.Label(cols[0], text="-∞", font=("Segoe UI", 7),
                                    fg=TEXT_DIM, bg=BG_PANEL)
        self._in_db_lbl.pack(pady=(2, 0))
        self._out_db_lbl = tk.Label(cols[1], text="-∞", font=("Segoe UI", 7),
                                     fg=TEXT_DIM, bg=BG_PANEL)
        self._out_db_lbl.pack(pady=(2, 0))

    def _build_strength_slider(self, parent):
        parent.configure(padx=14, pady=12)

        tk.Label(parent, text="STRENGTH", font=("Segoe UI", 8, "bold"),
                 fg=TEXT_DIM, bg=BG_PANEL).pack(anchor="w")

        slider_row = tk.Frame(parent, bg=BG_PANEL)
        slider_row.pack(fill="x", pady=(6, 0))

        tk.Label(slider_row, text="Off", font=("Segoe UI", 8),
                 fg=TEXT_DIM, bg=BG_PANEL).pack(side="left")

        self._strength_var = tk.DoubleVar(value=self.config.strength)
        style = ttk.Style()
        style.theme_use("default")
        style.configure("Vol.Horizontal.TScale",
                         background=BG_PANEL,
                         troughcolor=BG_WIDGET,
                         slidercolor=ACCENT)

        self._strength_slider = ttk.Scale(
            slider_row, from_=0, to=100,
            variable=self._strength_var,
            orient="horizontal",
            style="Vol.Horizontal.TScale",
            command=self._on_strength_change
        )
        self._strength_slider.pack(side="left", fill="x", expand=True, padx=8)

        tk.Label(slider_row, text="Max", font=("Segoe UI", 8),
                 fg=TEXT_DIM, bg=BG_PANEL).pack(side="left")

        self._strength_val_lbl = tk.Label(
            parent, text=f"{self.config.strength:.0f}%",
            font=("Segoe UI", 11, "bold"), fg=ACCENT_LITE, bg=BG_PANEL
        )
        self._strength_val_lbl.pack(anchor="e", pady=(2, 0))

    def _build_advanced_panel(self, parent):
        parent.configure(padx=14, pady=10)

        params = [
            ("Threshold (dB)", "compressor_threshold_db", -60, 0),
            ("Ratio", "compressor_ratio", 1, 20),
            ("Attack (ms)", "compressor_attack_ms", 0, 100),
            ("Release (ms)", "compressor_release_ms", 10, 1000),
            ("Makeup Gain (dB)", "makeup_gain_db", -12, 24),
            ("Gate Threshold (dB)", "gate_threshold_db", -80, -10),
            ("Limiter Ceiling (dB)", "limiter_ceiling_db", -12, 0),
        ]

        self._adv_vars = {}
        adv = self.config.get_advanced()

        for label, key, lo, hi in params:
            row = tk.Frame(parent, bg=BG_PANEL)
            row.pack(fill="x", pady=3)

            tk.Label(row, text=label, width=20, anchor="w",
                     font=("Segoe UI", 8), fg=TEXT, bg=BG_PANEL).pack(side="left")

            var = tk.DoubleVar(value=adv.get(key, 0))
            self._adv_vars[key] = var

            val_lbl = tk.Label(row, text=f"{var.get():.1f}", width=6,
                                font=("Segoe UI", 8), fg=ACCENT_LITE, bg=BG_PANEL)
            val_lbl.pack(side="right")

            def make_cmd(k, v, lbl):
                def cmd(val):
                    lbl.config(text=f"{float(val):.1f}")
                    self.config.set_advanced(k, float(val))
                    self.pipeline.set_advanced(**{k: float(val)})
                return cmd

            scale = ttk.Scale(row, from_=lo, to=hi, variable=var,
                              orient="horizontal",
                              command=make_cmd(key, var, val_lbl))
            scale.pack(side="left", fill="x", expand=True, padx=(4, 4))

    def _build_device_selector(self, parent):
        tk.Label(parent, text="Output Device:", font=("Segoe UI", 8),
                 fg=TEXT_DIM, bg=BG).pack(side="left")

        self._device_var = tk.StringVar(value=self.config.audio_device)
        devices = ["default"] + [
            name for name, _ in (self.engine.get_available_output_devices()
                                   if self.engine else [])
        ]

        device_combo = ttk.Combobox(
            parent, textvariable=self._device_var,
            values=devices, state="readonly",
            width=28, font=("Segoe UI", 8)
        )
        device_combo.pack(side="left", padx=(6, 0))
        device_combo.bind("<<ComboboxSelected>>", self._on_device_change)

    # ─── Event handlers ──────────────────────────────────────

    def _on_strength_change(self, val):
        strength = float(val)
        self._strength_val_lbl.config(text=f"{strength:.0f}%")
        self.config.strength = strength
        if not self.config.use_advanced:
            self.pipeline.set_strength(strength)

    def _on_enabled_toggle(self):
        enabled = self._enabled_var.get()
        self.config.enabled = enabled
        self.pipeline.set_enabled(enabled)
        self._status_label.config(
            text="● Active" if enabled else "○ Disabled",
            fg=GREEN if enabled else TEXT_DIM
        )

    def _on_bypass_toggle(self):
        bypass = self._bypass_var.get()
        self.pipeline.set_bypass(bypass)
        self._status_label.config(
            text="⊘ Bypassed" if bypass else "● Active",
            fg=YELLOW if bypass else GREEN
        )

    def _on_device_change(self, event=None):
        device = self._device_var.get()
        self.config.audio_device = device

    def _toggle_advanced(self):
        self._advanced_visible = not self._advanced_visible
        if self._advanced_visible:
            self._adv_frame.pack(fill="x", pady=(0, 8))
            self._adv_btn.config(text="▼  Advanced")
        else:
            self._adv_frame.pack_forget()
            self._adv_btn.config(text="▶  Advanced")

    # ─── Meter updates ────────────────────────────────────────

    def _start_meter_updates(self):
        self._meter_running = True
        self._update_meters()

    def _update_meters(self):
        if not self._meter_running or not self._root:
            return

        try:
            in_db = self.pipeline.input_level_db
            out_db = self.pipeline.output_level_db
            gr_db = self.pipeline.gain_reduction_db

            self._input_meter.set_level(in_db)
            self._output_meter.set_level(out_db)
            self._gr_meter.set_level(min(gr_db, 20))

            self._gr_label.config(text=f"GR: {gr_db:.1f} dB")

            in_text = f"{in_db:.0f}dB" if in_db > -59 else "-∞"
            out_text = f"{out_db:.0f}dB" if out_db > -59 else "-∞"

            self._in_db_lbl.config(text=in_text)
            self._out_db_lbl.config(text=out_text)

        except Exception:
            pass

        if self._root and self._meter_running:
            self._root.after(80, self._update_meters)  # ~12 fps meter refresh
