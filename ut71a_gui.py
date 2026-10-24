#!/usr/bin/env python3
"""UT71A Logger: live plot, statistics and CSV logging for UNI-T UT71x multimeters."""
import bisect
import csv
import math
import sys
import time
from datetime import datetime
from pathlib import Path

import numpy as np
import pyqtgraph as pg
from PyQt6.QtCore import QSettings, Qt, QThread, QTimer, pyqtSignal
from PyQt6.QtGui import QAction, QColor, QKeySequence, QPalette
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QSplitter,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

import ut71a

APP_NAME = "UT71A Logger"
LOG_DIR = Path(__file__).resolve().parent / "logs"
CSV_FIELDS = ["time_s", "timestamp", "value", "unit", "function", "ac", "dc", "auto"]

UNIT_SYMBOL = {"Ohm": "Ω"}
NO_SI_PREFIX = {"°C", "°F", "%"}
QUANTITY = {"A": "Current", "V": "Voltage", "Ohm": "Resistance", "F": "Capacitance",
            "Hz": "Frequency", "°C": "Temperature", "°F": "Temperature", "W": "Power", "%": "Percent"}
RATE_WINDOW = 10  # seconds, for the live sample rate
SPANS = [("All", 0), ("10 s", 10), ("1 min", 60), ("5 min", 300), ("15 min", 900), ("1 h", 3600)]

BG = "#15171c"
PANEL = "#1d2027"
BORDER = "#2b2f38"
TEXT = "#d7dae0"
MUTED = "#8a909c"
ACCENT = "#ff8c42"
GREEN = "#3ecf8e"
RED = "#ef5b5b"

STYLE = f"""
QMainWindow, QWidget {{ background: {BG}; color: {TEXT}; font-size: 13px; }}
QLabel, QCheckBox {{ background: transparent; }}
QToolBar {{ background: {PANEL}; border: none; border-bottom: 1px solid {BORDER}; spacing: 6px; padding: 6px; }}
QToolBar QToolButton {{ background: #262a33; border: 1px solid {BORDER}; border-radius: 6px; padding: 6px 12px; }}
QToolBar QToolButton:hover {{ background: #303540; }}
QToolBar QToolButton:checked {{ background: #3a3f4b; border-color: {ACCENT}; }}
QToolButton#start {{ background: #1f3b2e; border-color: {GREEN}; color: {GREEN}; font-weight: bold; }}
QToolButton#stop {{ background: #3d2222; border-color: {RED}; color: {RED}; font-weight: bold; }}
QGroupBox {{ background: {PANEL}; border: 1px solid {BORDER}; border-radius: 8px; margin-top: 14px; padding: 10px 8px 6px 8px; }}
QGroupBox::title {{ subcontrol-origin: margin; left: 10px; padding: 0 4px; color: {MUTED}; }}
QFrame#readout {{ background: {PANEL}; border: 1px solid {BORDER}; border-radius: 8px; }}
QLabel#value {{ color: {ACCENT}; font-size: 34px; font-weight: 600; }}
QLabel#mode {{ color: {MUTED}; }}
QLabel[class="key"] {{ color: {MUTED}; }}
QComboBox, QDoubleSpinBox {{ background: #262a33; border: 1px solid {BORDER}; border-radius: 5px; padding: 3px 6px; }}
QStatusBar {{ background: {PANEL}; border-top: 1px solid {BORDER}; color: {MUTED}; }}
QSplitter::handle {{ background: {BORDER}; }}
"""


def fmt_value(v, unit, precision=4):
    if v is None or math.isnan(v):
        return "–"
    if math.isinf(v):
        return "OL"
    sym = UNIT_SYMBOL.get(unit, unit)
    if unit in NO_SI_PREFIX:
        return f"{v:.{precision}g} {sym}"
    return pg.siFormat(v, precision=precision, suffix=sym)


def fmt_duration(s, decimals=False):
    sign = "-" if s < 0 else ""
    s = abs(s)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    sec_s = f"{sec:04.1f}" if decimals else f"{int(sec):02d}"
    if h >= 1:
        return f"{sign}{int(h)}:{int(m):02d}:{sec_s}"
    return f"{sign}{int(m)}:{sec_s}"


def fmt_rate(rate):
    return "–" if rate is None else f"{rate:.2f} Sa/s"


def compute_stats(t, y, unit):
    ok = np.isfinite(y)
    t, y = t[ok], y[ok]
    if not len(y):
        return None
    dur = float(t[-1] - t[0])
    if len(y) > 1 and dur > 0:
        area = float(np.trapezoid(y, t))
        avg = area / dur
    else:
        area, avg = 0.0, float(y.mean())
    return {
        "n": len(y),
        "dur": dur,
        "rate": (len(y) - 1) / dur if dur > 0 else None,
        "min": float(y.min()),
        "max": float(y.max()),
        "avg": avg,
        "charge": area / 3600 if unit == "A" else None,  # Ah
    }


class Recording:
    def __init__(self):
        self.clear()

    def clear(self):
        self.t0 = None
        self.t, self.epoch, self.v = [], [], []
        self.unit, self.func, self.ac, self.dc, self.auto = [], [], [], [], []

    def __len__(self):
        return len(self.t)

    def append(self, epoch, value, unit, func, ac, dc, auto, t=None):
        if self.t0 is None:
            self.t0 = epoch if t is None else epoch - t
        self.t.append(epoch - self.t0 if t is None else t)
        self.epoch.append(epoch)
        self.v.append(value)
        self.unit.append(unit)
        self.func.append(func)
        self.ac.append(ac)
        self.dc.append(dc)
        self.auto.append(auto)
        return len(self.t) - 1

    def row(self, i):
        ts = datetime.fromtimestamp(self.epoch[i]).isoformat(timespec="milliseconds")
        return [f"{self.t[i]:.3f}", ts, repr(self.v[i]), self.unit[i], self.func[i],
                int(self.ac[i]), int(self.dc[i]), int(self.auto[i])]

    def save(self, path):
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(CSV_FIELDS)
            for i in range(len(self)):
                w.writerow(self.row(i))

    def load(self, path):
        """Load CSV written by this app or by `ut71a.py --csv`."""
        self.clear()
        with open(path, newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                if r.get("timestamp"):
                    epoch = datetime.fromisoformat(r["timestamp"]).timestamp()
                else:
                    epoch = datetime.strptime(r["time"], "%Y-%m-%d %H:%M:%S").timestamp()
                t = float(r["time_s"]) if r.get("time_s") else None
                self.append(epoch, float(r["value"]), r["unit"], r["function"],
                            parse_bool(r["ac"]), parse_bool(r["dc"]), parse_bool(r["auto"]), t)
        if not len(self):
            raise ValueError("file contains no samples")


def parse_bool(s):
    return s.strip().lower() in ("1", "true", "yes")


class Reader(QThread):
    sample = pyqtSignal(float, dict)
    status = pyqtSignal(str, str)  # message, state: connected / waiting / stopped

    def __init__(self):
        super().__init__()
        self._stop = False

    def stop(self):
        self._stop = True
        self.wait(3000)

    def run(self):
        failures = 0
        while not self._stop:
            link = None
            try:
                link = ut71a.Ch9325()
                self.status.emit("Connected – hold SEND on the meter if no data arrives", "connected")
                framer = ut71a.Framer()
                while not self._stop:
                    for pkt in framer.feed(link.read(200)):
                        m = ut71a.decode(pkt)
                        if m:
                            failures = 0
                            self.sample.emit(time.time(), m)
            except (OSError, RuntimeError) as e:
                failures += 1
                hint = "  Cable keeps dropping: close Google Chrome (WebUSB/WebHID)." if failures >= 3 else ""
                self.status.emit(f"{e}. Retrying…{hint}", "waiting")
                for _ in range(10):
                    if self._stop:
                        break
                    self.msleep(100)
            finally:
                if link:
                    link.close()
        self.status.emit("Stopped", "stopped")


class TimeAxis(pg.AxisItem):
    def tickStrings(self, values, scale, spacing):
        return [fmt_duration(v, spacing < 1) for v in values]


class StatsBox(QGroupBox):
    ROWS = [("dur", "Duration"), ("n", "Samples"), ("rate", "Rate"), ("min", "Min"), ("max", "Max"),
            ("avg", "Average"), ("charge", "Charge"), ("energy", "Energy")]

    def __init__(self, title):
        super().__init__(title)
        grid = QGridLayout(self)
        grid.setVerticalSpacing(4)
        self.labels = {}
        for row, (key, name) in enumerate(self.ROWS):
            k = QLabel(name)
            k.setProperty("class", "key")
            v = QLabel("–")
            v.setAlignment(Qt.AlignmentFlag.AlignRight)
            v.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            grid.addWidget(k, row, 0)
            grid.addWidget(v, row, 1)
            self.labels[key] = v

    def show_stats(self, st, unit, supply_v):
        if st is None:
            for v in self.labels.values():
                v.setText("–")
            return
        L = self.labels
        L["dur"].setText(fmt_duration(st["dur"], True))
        L["n"].setText(str(st["n"]))
        L["rate"].setText(fmt_rate(st["rate"]))
        for key in ("min", "max", "avg"):
            L[key].setText(fmt_value(st[key], unit, 5))
        if st["charge"] is None:
            L["charge"].setText("–")
            L["energy"].setText("–")
        else:
            L["charge"].setText(pg.siFormat(st["charge"], precision=4, suffix="Ah"))
            L["energy"].setText(pg.siFormat(st["charge"] * supply_v, precision=4, suffix="Wh"))


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.settings = QSettings("UNI-T", APP_NAME)
        self.rec = Recording()
        self.reader = None
        self.log_file = self.log_writer = self.log_path = None
        self.source_file = None
        self.unsaved = False
        self.dirty = False
        self.receiving = False
        self._t = np.empty(0)
        self._y = np.empty(0)
        self._unit = ""

        self._build_ui()
        self._restore_settings()
        self._update_title()

        self.timer = QTimer(self, interval=100, timeout=self.refresh)
        self.timer.start()

    # UI construction

    def _build_ui(self):
        tb = QToolBar("Main", movable=False)
        tb.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        self.addToolBar(tb)

        self.act_run = QAction("▶  Start", self, shortcut=QKeySequence("Space"), triggered=self.toggle_capture)
        self.act_clear = QAction("Clear", self, shortcut=QKeySequence("Ctrl+K"), triggered=self.clear)
        self.act_open = QAction("Open…", self, shortcut=QKeySequence.StandardKey.Open, triggered=self.open_file)
        self.act_save = QAction("Save as…", self, shortcut=QKeySequence.StandardKey.Save, triggered=self.save_file)
        self.act_follow = QAction("Follow", self, checkable=True, shortcut=QKeySequence("F"), triggered=self.on_follow)
        self.act_follow.setChecked(True)
        self.act_fit = QAction("Fit", self, shortcut=QKeySequence("A"), triggered=self.fit_view)
        self.act_sel = QAction("Selection", self, checkable=True, shortcut=QKeySequence("S"),
                               toggled=self.on_selection)
        for a in (self.act_run, None, self.act_open, self.act_save, self.act_clear, None,
                  self.act_follow, self.act_fit, self.act_sel):
            tb.addSeparator() if a is None else tb.addAction(a)
        tb.widgetForAction(self.act_run).setObjectName("start")

        tb.addSeparator()
        tb.addWidget(QLabel(" Window "))
        self.span = QComboBox()
        for name, sec in SPANS:
            self.span.addItem(name, sec)
        self.span.currentIndexChanged.connect(self.on_follow)
        tb.addWidget(self.span)
        tb.addSeparator()
        self.chk_log = QCheckBox("Auto-log to CSV")
        self.chk_log.setToolTip(f"Write every sample to {LOG_DIR}")
        tb.addWidget(self.chk_log)

        # plot
        pg.setConfigOptions(antialias=True, background=BG, foreground=MUTED)
        self.plot = pg.PlotWidget(axisItems={"bottom": TimeAxis("bottom")})
        self.plot.showGrid(x=True, y=True, alpha=0.15)
        self.plot.setLabel("bottom", "Time")
        self.plot.setMouseEnabled(x=True, y=True)
        self.vb = self.plot.getViewBox()
        self.vb.setAutoVisible(y=True)
        self.vb.sigRangeChangedManually.connect(lambda *_: self.act_follow.setChecked(False))
        self.curve = self.plot.plot(pen=pg.mkPen(ACCENT, width=1.6), symbol="o", symbolSize=4,
                                    symbolPen=None, symbolBrush=ACCENT, connect="finite")
        self.curve.setBrush(pg.mkBrush(255, 140, 66, 40))

        self.vline = pg.InfiniteLine(angle=90, movable=False, pen=pg.mkPen("#ffffff55", width=1))
        self.hover = pg.TextItem(color=TEXT, anchor=(0, 1), fill=pg.mkBrush(29, 32, 39, 220))
        for item in (self.vline, self.hover):
            item.setZValue(20)
            item.hide()
            self.plot.addItem(item, ignoreBounds=True)
        self.mouse_proxy = pg.SignalProxy(self.plot.scene().sigMouseMoved, rateLimit=30, slot=self.on_mouse)

        self.region = pg.LinearRegionItem(brush=pg.mkBrush(255, 255, 255, 22),
                                          pen=pg.mkPen("#ffffff88", width=1))
        self.region.setZValue(10)
        self.region.sigRegionChanged.connect(self.update_selection)

        # side panel
        side = QWidget()
        side.setMinimumWidth(270)
        side.setMaximumWidth(360)
        lay = QVBoxLayout(side)
        lay.setContentsMargins(8, 8, 8, 8)

        readout = QFrame(objectName="readout")
        rl = QVBoxLayout(readout)
        self.lbl_value = QLabel("–", objectName="value")
        self.lbl_value.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_mode = QLabel("no data", objectName="mode")
        self.lbl_mode.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_rate = QLabel("", objectName="mode")
        self.lbl_rate.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_rate.setToolTip(f"Samples received during the last {RATE_WINDOW} s")
        rl.addWidget(self.lbl_value)
        rl.addWidget(self.lbl_mode)
        rl.addWidget(self.lbl_rate)
        lay.addWidget(readout)

        self.box_all = StatsBox("Recording")
        self.box_sel = StatsBox("Selection")
        self.box_sel.setVisible(False)
        lay.addWidget(self.box_all)
        lay.addWidget(self.box_sel)

        sup = QHBoxLayout()
        lbl = QLabel("Supply voltage (for energy)")
        lbl.setProperty("class", "key")
        sup.addWidget(lbl)
        self.supply = QDoubleSpinBox(decimals=2, minimum=0.0, maximum=1000.0, singleStep=0.1, suffix=" V")
        self.supply.valueChanged.connect(self.update_stats)
        sup.addWidget(self.supply)
        lay.addLayout(sup)
        lay.addStretch(1)

        split = QSplitter()
        split.addWidget(self.plot)
        split.addWidget(side)
        split.setStretchFactor(0, 1)
        split.setCollapsible(0, False)
        self.setCentralWidget(split)

        sb = self.statusBar()
        self.lbl_conn = QLabel()
        self.lbl_status = QLabel()
        self.lbl_log = QLabel()
        sb.addWidget(self.lbl_conn)
        sb.addWidget(self.lbl_status, 1)
        sb.addPermanentWidget(self.lbl_log)
        self.on_status("Press Start (Space) to connect", "stopped")

    def _restore_settings(self):
        s = self.settings
        if geo := s.value("geometry"):
            self.restoreGeometry(geo)
        else:
            self.resize(1280, 720)
        self.chk_log.setChecked(s.value("autolog", True, type=bool))
        self.supply.setValue(s.value("supply", 3.3, type=float))
        self.span.setCurrentIndex(s.value("span", 0, type=int))

    def _update_title(self):
        name = Path(self.source_file).name if self.source_file else "live"
        self.setWindowTitle(f"{APP_NAME} – {name}{' *' if self.unsaved else ''}")

    # capture

    def toggle_capture(self):
        if self.reader:
            self.stop_capture()
        else:
            self.start_capture()

    def start_capture(self):
        if self.source_file:
            if not self.confirm_discard():
                return
            self.reset_recording()
        if self.chk_log.isChecked():
            try:
                self.open_log()
            except OSError as e:
                QMessageBox.warning(self, APP_NAME, f"Cannot create log file:\n{e}")
        self.capture_start = time.time()
        self.reader = Reader()
        self.reader.sample.connect(self.on_sample)
        self.reader.status.connect(self.on_status)
        self.reader.start()
        self.act_run.setText("■  Stop")
        self._set_run_style("stop")
        self.chk_log.setEnabled(False)

    def stop_capture(self):
        if self.reader:
            self.reader.stop()
            self.reader = None
        self.close_log()
        self.act_run.setText("▶  Start")
        self._set_run_style("start")
        self.chk_log.setEnabled(True)

    def _set_run_style(self, name):
        w = self.findChild(QToolBar).widgetForAction(self.act_run)
        w.setObjectName(name)
        w.style().unpolish(w)
        w.style().polish(w)

    def open_log(self):
        if self.log_path is None:
            LOG_DIR.mkdir(exist_ok=True)
            self.log_path = LOG_DIR / f"ut71a_{datetime.now():%Y%m%d_%H%M%S}.csv"
            new = True
        else:
            new = False
        self.log_file = open(self.log_path, "a", newline="", encoding="utf-8")
        self.log_writer = csv.writer(self.log_file)
        if new:
            self.log_writer.writerow(CSV_FIELDS)
            for i in range(len(self.rec)):
                self.log_writer.writerow(self.rec.row(i))
            self.log_file.flush()
        self.lbl_log.setText(f"Logging → {self.log_path.name}")

    def close_log(self):
        if self.log_file:
            self.log_file.close()
        self.log_file = self.log_writer = None
        self.lbl_log.setText(f"Log: {self.log_path.name}" if self.log_path else "")

    def on_sample(self, epoch, m):
        value = m["value"]
        i = self.rec.append(epoch, value, m["unit"], m["function"], m["ac"], m["dc"], m["auto"])
        if self.log_writer:
            self.log_writer.writerow(self.rec.row(i))
            self.log_file.flush()
        else:
            self.unsaved = True
            self._update_title()
        self.show_readout(i)
        self.dirty = True
        if not self.receiving:
            self.receiving = True
            self.lbl_status.setText("Receiving data")

    def on_status(self, msg, state):
        self.receiving = False
        color = {"connected": GREEN, "waiting": ACCENT}.get(state, MUTED)
        text = {"connected": "Connected", "waiting": "Waiting"}.get(state, "Idle")
        self.lbl_conn.setText(f"<span style='color:{color}'>●</span> {text}")
        self.lbl_status.setText(msg)

    def show_readout(self, i):
        r = self.rec
        self.lbl_value.setText(fmt_value(r.v[i], r.unit[i], 5))
        flags = [f for f, on in (("AC", r.ac[i]), ("DC", r.dc[i]), ("AUTO", r.auto[i])) if on]
        self.lbl_mode.setText(" · ".join([r.func[i], *flags]))

    # plot and statistics

    def update_rate(self):
        if not self.reader:
            self.lbl_rate.setText("")
            return
        now = time.time()
        window = min(RATE_WINDOW, now - self.capture_start)
        if window < 2:
            self.lbl_rate.setText("– Sa/s")
            return
        recent = self.rec.epoch[bisect.bisect_left(self.rec.epoch, now - window):]
        self.lbl_rate.setText(f"{len(recent) / window:.2f} Sa/s")

    def refresh(self):
        self.update_rate()
        if not self.dirty:
            return
        self.dirty = False
        r = self.rec
        if not len(r):
            self.curve.setData([], [])
            self.update_stats()
            return
        unit = r.unit[-1]
        t = np.asarray(r.t, dtype=float)
        v = np.asarray(r.v, dtype=float)
        mask = (np.asarray(r.unit) == unit) & np.isfinite(v)
        y = np.where(mask, v, np.nan)
        self._t, self._y = t, y

        if unit != self._unit:
            self._unit = unit
            axis = self.plot.getAxis("left")
            axis.enableAutoSIPrefix(unit not in NO_SI_PREFIX)
            self.plot.setLabel("left", QUANTITY.get(unit, r.func[-1]), units=UNIT_SYMBOL.get(unit, unit))

        self.curve.setData(t, y, connect="finite", symbol="o" if len(t) < 3000 else None)
        self.curve.setFillLevel(0 if mask.all() else None)

        if self.act_follow.isChecked():
            span = self.span.currentData()
            if span:
                self.vb.setXRange(max(t[0], t[-1] - span), t[-1], padding=0.02)
            else:
                self.vb.enableAutoRange(x=True)
            self.vb.enableAutoRange(y=True)
        self.update_stats()

    def update_stats(self):
        self.box_all.show_stats(compute_stats(self._t, self._y, self._unit) if len(self._t) else None,
                                self._unit, self.supply.value())
        self.update_selection()

    def update_selection(self):
        if not self.act_sel.isChecked() or not len(self._t):
            self.box_sel.show_stats(None, self._unit, 0)
            return
        lo, hi = self.region.getRegion()
        sel = (self._t >= lo) & (self._t <= hi)
        st = compute_stats(self._t[sel], self._y[sel], self._unit) if sel.any() else None
        self.box_sel.show_stats(st, self._unit, self.supply.value())

    def on_follow(self):
        if self.act_follow.isChecked():
            self.dirty = True

    def fit_view(self):
        self.act_follow.setChecked(False)
        self.vb.autoRange()

    def on_selection(self, on):
        self.box_sel.setVisible(on)
        if on:
            (x0, x1), _ = self.vb.viewRange()
            w = x1 - x0
            self.region.setRegion((x0 + w * 0.3, x0 + w * 0.7))
            self.plot.addItem(self.region, ignoreBounds=True)
        else:
            self.plot.removeItem(self.region)
        self.update_selection()

    def on_mouse(self, evt):
        pos = evt[0]
        if not len(self._t) or not self.plot.sceneBoundingRect().contains(pos):
            self.vline.hide()
            self.hover.hide()
            return
        x = self.vb.mapSceneToView(pos).x()
        i = int(np.clip(np.searchsorted(self._t, x), 1, len(self._t) - 1))
        if abs(self._t[i - 1] - x) < abs(self._t[i] - x):
            i -= 1
        t, v = self._t[i], self.rec.v[i]
        self.vline.setPos(t)
        self.hover.setText(f"{fmt_duration(t, True)}\n{fmt_value(v, self.rec.unit[i], 5)}")
        (x0, x1), (_, y1) = self.vb.viewRange()
        self.hover.setAnchor((1, 0) if t > (x0 + x1) / 2 else (0, 0))
        self.hover.setPos(t, y1)
        self.vline.show()
        self.hover.show()

    # files

    def confirm_discard(self):
        if not self.unsaved or not len(self.rec):
            return True
        res = QMessageBox.question(
            self, APP_NAME, "The recording has unsaved data. Save it first?",
            QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Discard | QMessageBox.StandardButton.Cancel)
        if res == QMessageBox.StandardButton.Save:
            return self.save_file()
        return res == QMessageBox.StandardButton.Discard

    def reset_recording(self):
        self.rec.clear()
        self.log_path = None
        self.source_file = None
        self.unsaved = False
        self._t, self._y, self._unit = np.empty(0), np.empty(0), ""
        self.lbl_value.setText("–")
        self.lbl_mode.setText("no data")
        self.lbl_log.setText("")
        self.act_follow.setChecked(True)
        self.dirty = True
        self._update_title()

    def clear(self):
        if not self.confirm_discard():
            return
        running = self.reader is not None
        if running:
            self.stop_capture()
        self.reset_recording()
        if running:
            self.start_capture()

    def save_file(self):
        if not len(self.rec):
            QMessageBox.information(self, APP_NAME, "Nothing to save.")
            return False
        start = Path(self.settings.value("lastdir", str(Path.home()))) / f"ut71a_{datetime.now():%Y%m%d_%H%M%S}.csv"
        path, _ = QFileDialog.getSaveFileName(self, "Save recording", str(start), "CSV (*.csv)")
        if not path:
            return False
        try:
            self.rec.save(path)
        except OSError as e:
            QMessageBox.critical(self, APP_NAME, f"Save failed:\n{e}")
            return False
        self.settings.setValue("lastdir", str(Path(path).parent))
        self.unsaved = False
        self._update_title()
        self.statusBar().showMessage(f"Saved {path}", 5000)
        return True

    def open_file(self):
        if self.reader:
            self.stop_capture()
        if not self.confirm_discard():
            return
        start = self.settings.value("lastdir", str(LOG_DIR if LOG_DIR.exists() else Path.home()))
        path, _ = QFileDialog.getOpenFileName(self, "Open recording", start, "CSV (*.csv);;All files (*)")
        if path:
            self.load_path(path)

    def load_path(self, path):
        self.reset_recording()
        try:
            self.rec.load(path)
        except (OSError, ValueError, KeyError) as e:
            self.rec.clear()
            msg = f"missing column {e}" if isinstance(e, KeyError) else str(e)
            QMessageBox.critical(self, APP_NAME, f"Cannot load {path}:\n{msg}")
            return
        self.settings.setValue("lastdir", str(Path(path).parent))
        self.source_file = path
        self.show_readout(len(self.rec) - 1)
        self.act_follow.setChecked(False)
        self.refresh_now()
        self.vb.autoRange()
        self._update_title()

    def refresh_now(self):
        self.dirty = True
        self.refresh()

    def closeEvent(self, e):
        if not self.confirm_discard():
            e.ignore()
            return
        self.stop_capture()
        s = self.settings
        s.setValue("geometry", self.saveGeometry())
        s.setValue("autolog", self.chk_log.isChecked())
        s.setValue("supply", self.supply.value())
        s.setValue("span", self.span.currentIndex())
        e.accept()


def dark_palette():
    p = QPalette()
    for role, color in (
        (QPalette.ColorRole.Window, BG), (QPalette.ColorRole.WindowText, TEXT),
        (QPalette.ColorRole.Base, PANEL), (QPalette.ColorRole.AlternateBase, BG),
        (QPalette.ColorRole.Text, TEXT), (QPalette.ColorRole.Button, PANEL),
        (QPalette.ColorRole.ButtonText, TEXT), (QPalette.ColorRole.Highlight, ACCENT),
        (QPalette.ColorRole.HighlightedText, "#000000"), (QPalette.ColorRole.ToolTipBase, PANEL),
        (QPalette.ColorRole.ToolTipText, TEXT), (QPalette.ColorRole.PlaceholderText, MUTED),
    ):
        p.setColor(role, QColor(color))
    return p


def main():
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setStyle("Fusion")
    app.setPalette(dark_palette())
    app.setStyleSheet(STYLE)
    win = MainWindow()
    win.show()
    if len(sys.argv) > 1:
        win.load_path(sys.argv[1])
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
