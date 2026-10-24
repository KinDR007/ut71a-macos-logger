# UT71A macOS Logger

**English** | [Čeština](README.cs.md)

Live plotting and logging for **UNI-T UT71A/B/C/D/E** multimeters on macOS, using the
**UT-D04** USB cable (WCH CH9325 HID-to-UART bridge, USB ID `1a86:e008`).
Built for watching the current consumption of embedded devices in the µA/mA ranges.

![UT71A Logger](docs/screenshot.png)

## Features

- Live plot of the measured value with dark, Otii-like UI (PyQt6 + pyqtgraph)
- Statistics for the whole recording and for any number of named selections: duration, samples,
  sample rate, min, max, time-weighted average, **charge (µAh/mAh)** and **energy (Wh)**
- Two time axes: time since start and wall-clock time
- Live sample rate (Sa/s)
- Auto-log of every sample to CSV; projects (samples + selections) can be saved and reopened
- Automatic reconnect when the cable drops
- Works with every meter function (V, A, Ω, F, Hz, °C/°F, …); charge is computed for current only
- Command-line reader `ut71a.py` for scripting

## Requirements

- macOS (tested on macOS 27, Apple Silicon)
- UNI-T UT71A/B/C/D/E with the UT-D04 USB cable

Everything else is installed by `install.sh`.

## Installation

```sh
git clone https://github.com/KinDR007/ut71a-macos-logger.git
cd ut71a-macos-logger
./install.sh
```

The installer is safe to run again; installed parts are skipped. It:

1. Installs [Homebrew](https://brew.sh) if it is missing (asks for your password; installs Xcode Command Line Tools)
2. Installs `python@3.14` from Homebrew and compares it with the tested version
3. Creates `.venv` and installs pinned Python packages from `requirements.txt`
4. Runs a self-test and reports whether the cable is connected

Tested versions:

| Component | Version |
|---|---|
| Python (Homebrew) | 3.14.5 |
| PyQt6 / Qt | 6.11.0 / 6.11.2 |
| pyqtgraph | 0.14.0 |
| numpy | 2.5.4 |
| hidapi (Python, bundles its own library) | 0.15.0 |

Homebrew always installs its current Python version; Python packages are pinned.
The `hidapi` package includes the native library, so no other system libraries are needed.

## Usage

### Meter setup

1. Connect the UT-D04 cable to the meter's optical port and to the Mac.
2. Turn the meter on and select the function (e.g. µA or mA).
3. Hold **SEND** for about 1 second until **SEND** appears on the display.

Changing the function or an automatic power-off turns SEND off; enable it again.

### GUI

```sh
./run.sh                    # or double-click "UT71A Logger.command" in Finder
./run.sh recording.csv      # open a saved recording
```

`run.sh` recreates `.venv` when needed and reinstalls packages only when `requirements.txt` changes.

| Key | Action |
|---|---|
| Space | Start / Stop |
| Ctrl+S / Ctrl+Shift+S | Save / Save as |
| Ctrl+O | Open |
| Ctrl+K | Clear |
| F | Follow the latest data |
| A | Fit the whole recording |
| S | Add a selection |
| Delete / Backspace | Remove the active selection |

Mouse: wheel zooms, drag pans (turns Follow off), right-drag zooms one axis.
Hover over the plot to read the time, clock time and value of a sample.

**Selections:** press S (or **+ Selection**) to add a region in the middle of the view. Drag the
edges (◆ handle, the cursor changes to ↔) to resize, drag the inside or the colored header to move.
Hover over the header to see Δt, clock range and full statistics; ✕ in the header removes it.
The Selections table lists all regions; click a row to show its statistics, double-click the name
to rename it, double-click another column (or **Zoom**) to zoom to it. The **Project** and **View**
rows summarize the whole recording and the visible range; the toolbar shows FROM / TO / Δ of the
active selection.

**Overview:** the strip above the plot shows the whole recording with the visible part highlighted
and its Δt, plus all selections as colored bands. Drag the highlight to pan, drag its edges to zoom,
click a band to zoom to that selection, click elsewhere to jump.

With **Auto-log to CSV** enabled, each sample is written to `logs/ut71a_YYYYMMDD_HHMMSS.csv` as it arrives.
Enter the supply voltage in the side panel to get energy from the measured charge.

### Command line

```sh
.venv/bin/python ut71a.py                 # print forever
.venv/bin/python ut71a.py -n 10           # 10 samples
.venv/bin/python ut71a.py --csv log.csv   # also append to CSV
.venv/bin/python ut71a.py --raw           # also print raw packets
```

### Files

- **Project** (`*.ut71proj`, ZIP with JSON metadata and numpy arrays): samples, selections and
  supply voltage. Save / Save as writes a project by default; Open restores everything.
  Older JSON projects open as well.
- **CSV** (`*.csv`): samples only. Choose "CSV samples only" in the Save dialog.

### CSV format

```
time_s,timestamp,value,unit,function,ac,dc,auto
0.000,2026-10-24T07:41:42.512,0.0558,A,mA,1,1,1
```

`value` is in base SI units (A, V, Ohm, F, Hz); overload is stored as `inf`.
CSV files from `ut71a.py --csv` can be opened in the GUI as well.

## Performance

The plot stays responsive with long recordings (tested up to 10⁷ samples, about 13 days at 9 Sa/s):

- Statistics of the recording and of every selection come from prefix sums and a min/max
  pyramid, so they cost O(log N) per update.
- Only the visible part is drawn. When zoomed out, each block contributes its minimum and maximum,
  so single-sample peaks stay visible while at most ~8 points per pixel are drawn.
- Redraw, pan, hover and selection dragging take ~15–30 ms independent of the recording length.

Memory: about 40 bytes per sample (up to ~2× while recording live), i.e. ~0.4–0.8 GB per 10⁷ samples.

## Sample rate

The meter, not the PC, limits the rate:

| Mode | Rate |
|---|---|
| Normal (40000 counts) | ~1.5 Sa/s (display updates 2–3×/s) |
| **Fast (4000 counts)** | **~9 Sa/s** |
| Serial link limit (2400 Bd, 11-byte packets) | ~21 Sa/s |

**Fast mode:** with the meter off, hold the **blue button** and turn the rotary switch to the
function. The meter uses 4000 counts (one digit less, same accuracy) until it is switched off
or powers off automatically. Then enable SEND again.

At ~9 Sa/s, short current peaks (MCU wake-ups, radio bursts) are not captured and periodic loads
can alias. Average and charge are reliable only for steady or slowly changing loads.
For µs-level current profiles use a dedicated tool (Otii, Nordic PPK2, Joulescope).

## Troubleshooting

### The cable disconnects every ~0.4 s

Symptoms: `send_feature_report()` returns `-1`
(`IOHIDDeviceSetReport failed: (0xE00002ED) device not responding`), the HID path
`DevSrvsID:…` changes all the time, and no data arrives.

The kernel log shows the device being reset over and over:

```sh
/usr/bin/log show --last 10s --style compact --predicate 'process == "kernel"' | grep 'USB to Serial'
# ... endpoint 0x82: status 0xe00002ed (transaction error)
# ... terminateDevice: destroying 0x1a86/e008/1300 (USB to Serial): hardware connection lost
```

In our case the loop stopped as soon as **Google Chrome** was quit. Chrome opens every USB device
(WebUSB/WebHID/WebSerial), and the CH9325 does not handle that well. **Quit Chrome while measuring.**

Check that the cable is stable (the `id` must stay the same):

```sh
for i in 1 2 3 4 5; do ioreg -p IOUSB -w0 | grep 'USB to Serial'; sleep 1; done
```

### Connected, but no data

- Hold SEND on the meter again (it turns off after a function change or auto power-off).
- Only one program can use the cable at a time: close other GUI windows or `ut71a.py`.

## How it works

1. The CH9325 is configured with a HID feature report (report ID 0):
   `60 09 00 00 03` = 2400 Bd (little-endian) + UART settings.
2. Each 8-byte HID input report carries up to 7 UART bytes; the low 3 bits of the first byte
   are the byte count. Bit 7 of each data byte is parity and is masked off.
3. The meter sends 11-byte packets terminated by `\r\n`:

| Byte | Meaning |
|---|---|
| 0–4 | digits `0`–`9` (`:` in byte 4 = 4000-count mode, `::0<:` = overload) |
| 5 | range |
| 6 | function (0 AC mV, 1 DC V, 2 AC V, 3 DC mV, 4 Ω, 5 F, 6 °C, 7 µA, 8 mA, 9 10 A, 10 continuity, 11 diode, 12 Hz, 13 °F, 14 W, 15 4–20 mA %) |
| 7 | bit 0 AC, bit 1 DC |
| 8 | bit 0 auto, bit 1 manual, bit 2 sign |
| 9–10 | `\r\n` |

The decimal exponent per function and range follows libsigrok
[`src/dmm/ut71x.c`](https://github.com/sigrokproject/libsigrok/blob/master/src/dmm/ut71x.c).

## Files

| File | Purpose |
|---|---|
| `install.sh` | Installs all dependencies on a clean macOS |
| `run.sh` | Prepares `.venv` and starts the GUI |
| `UT71A Logger.command` | Finder launcher (double-click) |
| `ut71a_gui.py` | GUI application |
| `ut71a_data.py` | Recording storage, statistics, plot decimation, project files |
| `ut71a.py` | CH9325 driver, packet decoder, CLI |
| `requirements.txt` | Pinned Python dependencies |

## License

[GPL-3.0](LICENSE). The UT71x packet tables come from [libsigrok](https://sigrok.org) (GPL-3.0-or-later).
