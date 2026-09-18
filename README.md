# VolSteady 🔊

**Real-time system-wide audio stabilizer for Windows.**

No more painful volume spikes, no more straining to hear whispered dialogue.  
VolSteady sits silently in your system tray and automatically keeps your audio at a comfortable, consistent level — for every app, every video, every movie.

---

## What It Does

| Problem | VolSteady Solution |
|---------|-------------------|
| Movie whispers too quiet | Boosts quiet audio with makeup gain |
| Action scene explosions too loud | Compresses loud peaks instantly |
| Loudspeaker sudden loud noise | Brick-wall limiter prevents spikes |
| Background hiss during silence | Noise gate suppresses the floor |

---

## How It Works

VolSteady uses **WASAPI Loopback** (Windows native API) to capture all system audio in real-time, runs it through a 4-stage DSP pipeline, and plays back the stabilized audio — all with < 15ms latency.

```
System Audio → [Noise Gate] → [Compressor] → [Makeup Gain] → [Limiter] → Your Ears
```

- **Noise Gate** — silences background hum/hiss below a threshold
- **Dynamic Compressor** — narrows the gap between loud and quiet
- **Makeup Gain** — recovers overall loudness after compression
- **Brick-Wall Limiter** — guarantees peaks NEVER clip or spike

---

## Quick Start

### Requirements
- Windows 10/11
- Python 3.11+

### Install

```bash
pip install -r requirements.txt
```

### Run

```bash
python main.py              # Starts minimized to system tray
python main.py --show       # Opens settings window immediately
python main.py --strength 80  # Custom strength on launch
```

### Build standalone .exe

```bash
pip install pyinstaller
pyinstaller build.spec
# Output: dist/VolSteady.exe
```

---

## Usage

1. **Run VolSteady** — it appears as a small icon in your system tray (bottom-right)
2. **Right-click the tray icon** for quick controls:
   - Toggle enabled/disabled
   - Set strength (Low / Medium / High)
   - Open full settings
3. **Open Settings** for:
   - Strength slider (0–100%)
   - Real-time level meters (Input / Output / Gain Reduction)
   - Advanced DSP controls (threshold, ratio, attack, release, etc.)
   - Audio device selector

---

## Performance

| Metric | Value |
|--------|-------|
| CPU Usage | < 2% (modern hardware) |
| Latency | < 15ms |
| Memory | < 50 MB |
| Exe Size | ~25-30 MB |

---

## DSP Parameters (Advanced)

| Parameter | Default | Description |
|-----------|---------|-------------|
| Threshold | -25 dB | Level where compression starts |
| Ratio | 4:1 | How aggressively to compress |
| Attack | 10 ms | Reaction time to loud sounds |
| Release | 200 ms | Recovery time after loud sounds |
| Knee | 6 dB | Soft transition zone (sounds natural) |
| Makeup Gain | 0 dB | Loudness boost after compression |
| Gate Threshold | -50 dB | Below this = treated as silence |
| Limiter Ceiling | -1 dB | Absolute maximum output level |

---

## Troubleshooting

**No sound after starting VolSteady?**
- VolSteady mutes the Windows audio endpoint and plays processed audio instead. If you hear silence, check that your output device is set correctly in Settings.

**Audio artifacts / glitches?**
- Try increasing the buffer size (edit `BUFFER_SIZE` in `audio_engine.py`)
- Make sure no other exclusive-mode audio app is running

**App not starting?**
- Check `%APPDATA%\VolSteady\volsteady.log` for error details
- Ensure your audio drivers are up to date

---

## Architecture

```
main.py               ← App lifecycle, CLI args, shutdown
audio_engine.py       ← WASAPI capture + playback + volume control
dsp/
  pipeline.py         ← DSP chain orchestrator
  compressor.py       ← Dynamic range compressor (Numba JIT)
  limiter.py          ← Brick-wall peak limiter (Numba JIT)
  gate.py             ← Noise gate (Numba JIT)
  level_detector.py   ← RMS/Peak envelope follower (Numba JIT)
gui/
  tray.py             ← System tray icon (pystray)
  settings_window.py  ← Settings panel (tkinter)
config.py             ← JSON settings persistence
utils.py              ← Logging, single-instance lock
```

---

## License

MIT License — free to use, modify, and distribute.
