#!/usr/bin/env python3
"""UT71A Logger: live plot, statistics and CSV logging for UNI-T UT71x multimeters."""
import csv
import html
import math
import sys
import time
import zipfile
from datetime import datetime
from pathlib import Path

import numpy as np
import pyqtgraph as pg
from PyQt6.QtCore import QSettings, Qt, QThread, QTimer, pyqtSignal
from PyQt6.QtGui import QAction, QColor, QKeySequence, QPalette
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

import ut71a
from ut71a_data import CSV_FIELDS, Recording, Series, load_project, save_project

APP_NAME = "UT71A Logger"
LOG_DIR = Path(__file__).resolve().parent / "logs"
PROJECT_EXT = ".ut71proj"

UNIT_SYMBOL = {"Ohm": "Ω"}
NO_SI_PREFIX = {"°C", "°F", "%"}
QUANTITY = {"A": "Current", "V": "Voltage", "Ohm": "Resistance", "F": "Capacitance",
            "Hz": "Frequency", "°C": "Temperature", "°F": "Temperature", "W": "Power", "%": "Percent"}
RATE_WINDOW = 10  # seconds, for the live sample rate
SYMBOLS_MAX_VISIBLE = 400  # draw sample dots only when zoomed in this far
POINTS_PER_PIXEL = 8  # upper bound; LOD levels step by 16x, so 0.5-8 points per pixel
REFRESH_MS = 33
SPANS = [("All", 0), ("10 s", 10), ("1 min", 60), ("5 min", 300), ("15 min", 900), ("1 h", 3600)]

BG = "#15171c"
PANEL = "#1d2027"
BORDER = "#2b2f38"
TEXT = "#d7dae0"
MUTED = "#8a909c"
ACCENT = "#ff8c42"
GREEN = "#3ecf8e"
AXIS_WIDTH = 64  # fixed left axis width keeps the overview aligned with the plot
RED = "#ef5b5b"
HEADER_SPAN = 0.955  # selection header band: top part of the plot, fraction of its height
HEADER_TEXT = "#101215"
SEL_COLORS = ["#4ea8de", "#c77dff", "#3ecf8e", "#ffd166", "#ef5b5b", "#80ffdb"]

STYLE = f"""
QMainWindow, QWidget {{ background: {BG}; color: {TEXT}; font-size: 13px; }}
QLabel, QCheckBox {{ background: transparent; }}
QToolBar {{ background: {PANEL}; border: none; border-bottom: 1px solid {BORDER}; spacing: 6px; padding: 6px; }}
QToolBar QToolButton, QPushButton {{ background: #262a33; border: 1px solid {BORDER}; border-radius: 6px; padding: 6px 12px; }}
QToolBar QToolButton:hover, QPushButton:hover {{ background: #303540; }}
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
QTableWidget {{ background: {PANEL}; border: none; gridline-color: {BORDER};
    selection-background-color: #3a3f4b; selection-color: {TEXT}; }}
QHeaderView::section {{ background: {PANEL}; color: {MUTED}; border: none; border-bottom: 1px solid {BORDER}; padding: 3px; }}
QScrollArea {{ border: none; }}
QScrollBar:vertical {{ background: {BG}; width: 8px; margin: 0; }}
QScrollBar::handle:vertical {{ background: {BORDER}; border-radius: 4px; min-height: 30px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; }}
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


def fmt_clock(epoch):
    return datetime.fromtimestamp(epoch).strftime("%H:%M:%S.%f")[:-5]



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


class ClockAxis(pg.DateAxisItem):
    """Wall-clock axis for data plotted against time relative to `origin()` (epoch)."""

    def __init__(self, origin):
        super().__init__(orientation="top")
        self.origin = origin

    def tickValues(self, minVal, maxVal, size):
        t0 = self.origin()
        if t0 is None:
            return []
        return [(spacing, [v - t0 for v in values])
                for spacing, values in super().tickValues(minVal + t0, maxVal + t0, size)]

    def tickStrings(self, values, scale, spacing):
        t0 = self.origin() or 0
        return super().tickStrings([v + t0 for v in values], scale, spacing)


class Selection:
    def __init__(self, name, lo, hi, color):
        self.name = name
        self.color = color
        c = QColor(color)
        self.brush = pg.mkBrush(c.red(), c.green(), c.blue(), 30)
        self.active_brush = pg.mkBrush(c.red(), c.green(), c.blue(), 65)
        self.region = pg.LinearRegionItem(
            (lo, hi), brush=self.brush, hoverBrush=pg.mkBrush(c.red(), c.green(), c.blue(), 50),
            pen=pg.mkPen(color, width=1.5), hoverPen=pg.mkPen(color, width=3))
        self.region.setZValue(10)
        self.region.setCursor(Qt.CursorShape.OpenHandCursor)
        for line in self.region.lines:
            line.setCursor(Qt.CursorShape.SizeHorCursor)
            line.addMarker("<|>", position=0.5, size=12)  # visible handle, also widens the grab area
        # Otii-style header: a colored band at the top with the name and a close button.
        # It ignores mouse drags, so dragging the header moves the region below it.
        self.header_brush = pg.mkBrush(c.red(), c.green(), c.blue(), 150)
        self.header_active_brush = pg.mkBrush(c.red(), c.green(), c.blue(), 235)
        self.header = pg.LinearRegionItem((lo, hi), movable=False, span=(HEADER_SPAN, 1), brush=self.header_brush,
                                          pen=pg.mkPen(None))
        self.header.setZValue(9)  # below the region, so its line labels stay on top
        for line in self.header.lines:
            line.hide()
        self.region.sigRegionChanged.connect(lambda r: self.header.setRegion(r.getRegion()))
        label_pos = (1 + HEADER_SPAN) / 2
        self.label = pg.InfLineLabel(self.region.lines[0], text=f" {name}", position=label_pos,
                                     anchors=[(0, 0.5), (0, 0.5)], color=HEADER_TEXT)
        self.close_label = pg.InfLineLabel(self.region.lines[1], text="✕ ", position=label_pos,
                                           anchors=[(1, 0.5), (1, 0.5)], color=HEADER_TEXT)
        self.close_label.setToolTip("Remove selection")

    def bounds(self):
        return self.region.getRegion()

    def items(self):
        return self.region, self.header

    def header_hit(self, vb, scene_pos):
        p = vb.mapSceneToView(scene_pos)
        lo, hi = self.bounds()
        _, (y0, y1) = vb.viewRange()
        return lo <= p.x() <= hi and p.y() >= y0 + (y1 - y0) * HEADER_SPAN

    def close_hit(self, scene_pos):
        return self.close_label.sceneBoundingRect().adjusted(-3, -3, 3, 3).contains(scene_pos)

    def set_active(self, on):
        self.region.setBrush(self.active_brush if on else self.brush)
        self.region.update()
        self.header.setBrush(self.header_active_brush if on else self.header_brush)
        self.header.update()

    def rename(self, name):
        self.name = name
        self.label.setFormat(f" {name}")

    def to_dict(self):
        lo, hi = self.bounds()
        return {"name": self.name, "start": lo, "end": hi, "color": self.color}


STATS_ROWS = [("dur", "Duration"), ("n", "Samples"), ("rate", "Rate"), ("min", "Min"), ("max", "Max"),
              ("avg", "Average"), ("charge", "Charge"), ("energy", "Energy")]


def stats_texts(st, unit, supply_v):
    """Formatted values for STATS_ROWS keys."""
    if st is None:
        return {key: "–" for key, _ in STATS_ROWS}
    charge = st["charge"]
    return {
        "dur": fmt_duration(st["dur"], True),
        "n": str(st["n"]),
        "rate": fmt_rate(st["rate"]),
        "min": fmt_value(st["min"], unit, 5),
        "max": fmt_value(st["max"], unit, 5),
        "avg": fmt_value(st["avg"], unit, 5),
        "charge": "–" if charge is None else pg.siFormat(charge, precision=4, suffix="Ah"),
        "energy": "–" if charge is None else pg.siFormat(charge * supply_v, precision=4, suffix="Wh"),
    }


class StatsBox(QGroupBox):
    ROWS = STATS_ROWS

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
        for key, text in stats_texts(st, unit, supply_v).items():
            self.labels[key].setText(text)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.settings = QSettings("UNI-T", APP_NAME)
        self.rec = Recording()
        self.reader = None
        self.log_file = self.log_writer = self.log_path = None
        self.file_path = None  # project or CSV the data was loaded from / saved to
        self.from_file = False  # data was loaded, Start begins a new recording
        self.unsaved = False
        self.series = Series()
        self.data_dirty = False  # new samples to process
        self.view_dirty = False  # plot needs re-rendering
        self.overview_dirty = False
        self._ov_sync = False  # overview region is being moved by code
        self.auto_y = True
        self.receiving = False
        self.selections = []
        self.active = None
        self.sel_counter = 0
        self._unit = ""
        self._t0 = None
        self._symbols = True

        self._build_ui()
        self._restore_settings()
        self._update_title()

        self.timer = QTimer(self, interval=REFRESH_MS, timeout=self.refresh)
        self.timer.start()

    # UI construction

    def _build_ui(self):
        tb = QToolBar("Main", movable=False)
        tb.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        self.addToolBar(tb)

        self.act_run = QAction("▶  Start", self, shortcut=QKeySequence("Space"), triggered=self.toggle_capture)
        self.act_clear = QAction("Clear", self, shortcut=QKeySequence("Ctrl+K"), triggered=self.clear)
        self.act_open = QAction("Open…", self, shortcut=QKeySequence.StandardKey.Open, triggered=self.open_file)
        self.act_save = QAction("Save", self, shortcut=QKeySequence.StandardKey.Save, triggered=self.save)
        self.act_save_as = QAction("Save as…", self, shortcut=QKeySequence("Ctrl+Shift+S"), triggered=self.save_as)
        self.act_follow = QAction("Follow", self, checkable=True, shortcut=QKeySequence("F"), triggered=self.on_follow)
        self.act_follow.setChecked(True)
        self.act_fit = QAction("Fit", self, shortcut=QKeySequence("A"), triggered=self.fit_view)
        self.act_add_sel = QAction("+ Selection", self, shortcut=QKeySequence("S"),
                                   triggered=lambda: self.add_selection())
        self.act_del_sel = QAction("Remove selection", self, triggered=lambda: self.remove_selection())
        self.act_del_sel.setShortcuts([QKeySequence("Delete"), QKeySequence("Backspace")])
        self.addAction(self.act_del_sel)
        for a in (self.act_run, None, self.act_open, self.act_save, self.act_save_as, self.act_clear, None,
                  self.act_follow, self.act_fit, self.act_add_sel):
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
        self.clock_axis = ClockAxis(lambda: self.rec.t0)
        self.plot = pg.PlotWidget(axisItems={"bottom": TimeAxis("bottom"), "top": self.clock_axis})
        self.plot.showAxis("top")
        self.plot.showGrid(x=True, y=True, alpha=0.15)
        self.plot.setLabel("bottom", "Time")
        self.plot.setLabel("top", "Clock")
        self.plot.setMouseEnabled(x=True, y=True)
        self.plot.getAxis("left").setWidth(AXIS_WIDTH)
        self.vb = self.plot.getViewBox()
        # Ranges are managed here: pyqtgraph auto-range would scan all data on every change.
        self.vb.disableAutoRange()
        self.vb.sigRangeChangedManually.connect(self.on_manual_range)
        self.vb.sigXRangeChanged.connect(self.invalidate_view)
        self.vb.sigResized.connect(self.invalidate_view)
        # 1 px pen: wider antialiased lines are ~50x slower to draw in Qt.
        self.curve = self.plot.plot(pen=pg.mkPen(ACCENT, width=1), symbol="o", symbolSize=3,
                                    symbolPen=None, symbolBrush=pg.mkBrush(255, 140, 66, 170))
        # Cache the drawn curve so hover line/label updates do not redraw the data.
        for item in (self.curve.curve, self.curve.scatter):
            item.setCacheMode(item.CacheMode.DeviceCoordinateCache)

        self.vline = pg.InfiniteLine(angle=90, movable=False, pen=pg.mkPen("#ffffff55", width=1))
        self.hover = pg.TextItem(color=TEXT, anchor=(0, 1), fill=pg.mkBrush(29, 32, 39, 220))
        for item in (self.vline, self.hover):
            item.setZValue(20)
            item.hide()
            self.plot.addItem(item, ignoreBounds=True)
        self.mouse_proxy = pg.SignalProxy(self.plot.scene().sigMouseMoved, rateLimit=30, slot=self.on_mouse)
        self.plot.scene().sigMouseClicked.connect(self.on_plot_click)

        # overview of the whole recording; the region is the visible part of the main plot
        self.overview = pg.PlotWidget()
        self.overview.setFixedHeight(30)
        ov = self.overview.getPlotItem()
        ov.hideButtons()
        ov.setMenuEnabled(False)
        ov.hideAxis("bottom")
        ov.getAxis("left").setWidth(AXIS_WIDTH)
        ov.getAxis("left").setStyle(showValues=False, tickLength=0)
        ov.getAxis("left").setPen(pg.mkPen(None))
        self.ovb = ov.getViewBox()
        self.ovb.setMouseEnabled(x=False, y=False)
        self.ovb.disableAutoRange()
        self.ovb.sigResized.connect(lambda *_: setattr(self, "overview_dirty", True))
        self.ov_curve = self.overview.plot(pen=pg.mkPen(255, 140, 66, 150, width=1))
        self.ov_region = pg.LinearRegionItem(brush=pg.mkBrush(255, 255, 255, 28),
                                             hoverBrush=pg.mkBrush(255, 255, 255, 45),
                                             pen=pg.mkPen("#ffffff99", width=1),
                                             hoverPen=pg.mkPen("#ffffff", width=2))
        self.ov_region.setZValue(10)
        self.ov_region.setCursor(Qt.CursorShape.OpenHandCursor)
        for line in self.ov_region.lines:
            line.setCursor(Qt.CursorShape.SizeHorCursor)
        self.ov_region.sigRegionChanged.connect(self.on_overview_region)
        self.ov_region.hide()
        self.overview.addItem(self.ov_region, ignoreBounds=True)
        self.ov_text = pg.TextItem(color=TEXT, anchor=(0.5, 0.5), fill=pg.mkBrush(21, 23, 28, 200))
        self.ov_text.setZValue(20)
        self.overview.addItem(self.ov_text, ignoreBounds=True)
        self.overview.scene().sigMouseClicked.connect(self.on_overview_click)
        self.overview.setToolTip("Whole recording. Drag the highlighted part to pan, its edges to zoom; "
                                 "click to jump.")

        # side panel
        side = QWidget()
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
        lay.addWidget(self.box_all)

        sel_box = QGroupBox("Selections")
        sl = QVBoxLayout(sel_box)
        self.sel_table = QTableWidget(0, 4)
        self.sel_table.setHorizontalHeaderLabels(["Name", "Duration", "Average", "Charge"])
        hh = self.sel_table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for col in (1, 2, 3):
            hh.setSectionResizeMode(col, QHeaderView.ResizeMode.ResizeToContents)
        self.sel_table.verticalHeader().hide()
        self.sel_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.sel_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.sel_table.setEditTriggers(QAbstractItemView.EditTrigger.DoubleClicked
                                       | QAbstractItemView.EditTrigger.EditKeyPressed)
        self.sel_table.setMinimumHeight(110)
        self.sel_table.setToolTip("Double-click a name to rename, another column to zoom to the selection")
        self.sel_table.itemChanged.connect(self.on_sel_renamed)
        self.sel_table.currentCellChanged.connect(lambda row, *_: self.set_active(row))
        self.sel_table.cellDoubleClicked.connect(self.on_sel_double_click)
        sl.addWidget(self.sel_table)
        btns = QHBoxLayout()
        for text, action in (("+ Add", lambda: self.add_selection()), ("Remove", lambda: self.remove_selection()),
                             ("Zoom", self.zoom_selection)):
            b = QPushButton(text)
            b.clicked.connect(lambda _, a=action: a())
            btns.addWidget(b)
        sl.addLayout(btns)
        lay.addWidget(sel_box)

        self.box_sel = StatsBox("Selection")
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

        scroll = QScrollArea(widgetResizable=True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setWidget(side)
        scroll.setMinimumWidth(320)
        scroll.setMaximumWidth(440)

        split = QSplitter()
        plot_box = QWidget()
        pl = QVBoxLayout(plot_box)
        pl.setContentsMargins(0, 0, 0, 0)
        pl.setSpacing(0)
        pl.addWidget(self.overview)
        pl.addWidget(self.plot)
        split.addWidget(plot_box)
        split.addWidget(scroll)
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
            self.resize(1280, 760)
        self.chk_log.setChecked(s.value("autolog", True, type=bool))
        self.supply.setValue(s.value("supply", 3.3, type=float))
        self.span.setCurrentIndex(s.value("span", 0, type=int))

    def _update_title(self):
        name = Path(self.file_path).name if self.file_path else "live"
        self.setWindowTitle(f"{APP_NAME} – {name}{' *' if self.unsaved else ''}")

    def mark_unsaved(self):
        if len(self.rec) and not self.unsaved:
            self.unsaved = True
            self._update_title()

    # capture

    def toggle_capture(self):
        if self.reader:
            self.stop_capture()
        else:
            self.start_capture()

    def start_capture(self):
        if self.from_file:
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
        i = self.rec.append(epoch, m["value"], m["unit"], m["function"], m["ac"], m["dc"], m["auto"])
        if self.log_writer:
            self.log_writer.writerow(self.rec.row(i))
            self.log_file.flush()
        if not self.log_writer or self.file_path:
            self.mark_unsaved()
        self.show_readout(i)
        self.data_dirty = True
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
        _, _, v, unit, func, ac, dc, auto = self.rec.sample(i)
        self.lbl_value.setText(fmt_value(v, unit, 5))
        flags = [f for f, on in (("AC", ac), ("DC", dc), ("AUTO", auto)) if on]
        self.lbl_mode.setText(" · ".join([func, *flags]))

    # plot and statistics

    def update_rate(self):
        if not self.reader:
            self.lbl_rate.setText("")
            return
        now = time.time()
        window = min(RATE_WINDOW, now - self.capture_start)
        if window < 2 or self.rec.t0 is None:
            self.lbl_rate.setText("– Sa/s")
            return
        t = self.rec.t
        recent = len(t) - np.searchsorted(t, now - window - self.rec.t0)
        self.lbl_rate.setText(f"{recent / window:.2f} Sa/s")

    def refresh(self):
        self.update_rate()
        if self.data_dirty:
            self.data_dirty = False
            self.process_new_data()
        if self.overview_dirty:
            self.overview_dirty = False
            self.render_overview()
        if self.view_dirty:
            self.view_dirty = False
            self.render()

    def process_new_data(self):
        r = self.rec
        if r.t0 != self._t0:
            self._t0 = r.t0
            self.clock_axis.picture = None
            self.clock_axis.update()
        if not len(r):
            self.series.reset(None)
            self.update_stats()
            self.view_dirty = self.overview_dirty = True
            return
        unit_code = int(r.unit_codes[-1])
        if unit_code != self.series.code:
            # meter function changed: rebuild for the new unit (O(N), rare)
            self.series.reset(unit_code)
            self._unit = r.unit(len(r) - 1)
            axis = self.plot.getAxis("left")
            axis.enableAutoSIPrefix(self._unit not in NO_SI_PREFIX)
            self.plot.setLabel("left", QUANTITY.get(self._unit, r.sample(len(r) - 1)[4]),
                               units=UNIT_SYMBOL.get(self._unit, self._unit))
        self.series.sync(r)

        if self.act_follow.isChecked():
            t = r.t
            span = self.span.currentData()
            lo = max(t[0], t[-1] - span) if span else t[0]
            self.vb.setXRange(lo, max(t[-1], lo + 1e-3), padding=0.02)
        self.view_dirty = True
        self.overview_dirty = True
        self.update_stats()

    def invalidate_view(self, *_):
        self.view_dirty = True

    def visible_indices(self, margin=1):
        (x0, x1), _ = self.vb.viewRange()
        t = self.rec.t
        i0 = max(int(np.searchsorted(t, x0)) - margin, 0)
        i1 = min(int(np.searchsorted(t, x1, side="right")) + margin, self.series.n)
        return i0, i1

    def render(self):
        n = self.series.n
        if not n:
            self.curve.setData([], [])
            return
        i0, i1 = self.visible_indices()
        width = max(int(self.vb.width()), 200)
        x, y, raw = self.series.envelope(self.rec.t, i0, i1, POINTS_PER_PIXEL * width)
        if self.auto_y:
            self.fit_y(*self.visible_indices(margin=0))
        show_symbols = raw and (i1 - i0) <= SYMBOLS_MAX_VISIBLE
        if show_symbols != self._symbols:
            self._symbols = show_symbols
            self.curve.setSymbol("o" if show_symbols else None)
        # No fill under the curve: an antialiased fill costs ~0.5 s per redraw in Qt.
        self.curve.setData(x, y, connect="finite")
        self.sync_overview_region()

    def render_overview(self):
        n = self.series.n
        if not n:
            self.ov_curve.setData([], [])
            self.ov_region.hide()
            self.ov_text.setText("")
            return
        t = self.rec.t
        x, y, _ = self.series.envelope(t, 0, n, 2 * max(int(self.ovb.width()), 200))
        self.ov_curve.setData(x, y, connect="finite")
        mm = self.series.minmax(0, n)
        if mm:
            pad = (mm[1] - mm[0]) * 0.1 or abs(mm[1]) * 0.05 or 1e-9
            self.ovb.setYRange(mm[0] - pad, mm[1] + pad, padding=0)
        self.ovb.setXRange(t[0], max(t[-1], t[0] + 1e-3), padding=0)
        self.ov_region.show()
        self.sync_overview_region()

    def sync_overview_region(self):
        (x0, x1), _ = self.vb.viewRange()
        self._ov_sync = True
        self.ov_region.setRegion((x0, x1))
        self._ov_sync = False
        (ox0, ox1), (oy0, oy1) = self.ovb.viewRange()
        self.ov_text.setText(f"Δt {fmt_duration(x1 - x0, True)}")
        self.ov_text.setPos(min(max((x0 + x1) / 2, ox0 + (ox1 - ox0) * 0.05), ox1 - (ox1 - ox0) * 0.05),
                            (oy0 + oy1) / 2)

    def on_overview_region(self):
        if self._ov_sync:
            return
        lo, hi = self.ov_region.getRegion()
        self.act_follow.setChecked(False)
        self.auto_y = True
        self.vb.setXRange(lo, hi, padding=0)

    def on_overview_click(self, ev):
        if ev.button() != Qt.MouseButton.LeftButton or not self.series.n:
            return
        if not self.ovb.sceneBoundingRect().contains(ev.scenePos()):
            return
        x = self.ovb.mapSceneToView(ev.scenePos()).x()
        lo, hi = self.ov_region.getRegion()
        self.ov_region.setRegion((x - (hi - lo) / 2, x + (hi - lo) / 2))

    def fit_y(self, i0, i1):
        mm = self.series.minmax(i0, i1) if i1 > i0 else None
        if mm is None:
            return
        lo, hi = mm
        pad = (hi - lo) * 0.08 or abs(hi) * 0.05 or 1e-9
        top = hi + pad
        top += (top - (lo - pad)) * (1 - HEADER_SPAN) / HEADER_SPAN if self.selections else 0
        self.vb.setYRange(lo - pad, top, padding=0)

    def on_manual_range(self, mask=None, *_):
        self.act_follow.setChecked(False)
        if mask is None or (len(mask) > 1 and mask[1]):
            self.auto_y = False

    def update_stats(self):
        st = self.series.stats(self.rec.t, 0, self.series.n, self._unit) if self.series.n else None
        self.box_all.show_stats(st, self._unit, self.supply.value())
        self.update_selections()

    def selection_stats(self, sel):
        if not self.series.n:
            return None
        lo, hi = sel.bounds()
        t = self.rec.t
        i0 = int(np.searchsorted(t, lo))
        i1 = int(np.searchsorted(t, hi, side="right"))
        return self.series.stats(t, i0, i1, self._unit)

    def update_selections(self):
        self.sel_table.blockSignals(True)
        for row, sel in enumerate(self.selections):
            st = self.selection_stats(sel)
            cells = ["–", "–", "–"]
            if st:
                charge = pg.siFormat(st["charge"], precision=3, suffix="Ah") if st["charge"] is not None else "–"
                cells = [fmt_duration(st["dur"], True), fmt_value(st["avg"], self._unit, 4), charge]
            for col, text in enumerate(cells, start=1):
                self.sel_table.item(row, col).setText(text)
        self.sel_table.blockSignals(False)
        if self.active is not None:
            sel = self.selections[self.active]
            self.box_sel.setTitle(f"Selection: {sel.name}")
            self.box_sel.show_stats(self.selection_stats(sel), self._unit, self.supply.value())
        else:
            self.box_sel.setTitle("Selection")
            self.box_sel.show_stats(None, self._unit, 0)

    def on_follow(self):
        if self.act_follow.isChecked():
            self.auto_y = True
            self.data_dirty = True

    def fit_view(self):
        self.act_follow.setChecked(False)
        self.auto_y = True
        t = self.rec.t
        if len(t):
            self.vb.setXRange(t[0], max(t[-1], t[0] + 1e-3), padding=0.02)
        self.view_dirty = True

    # selections

    def add_selection(self, lo=None, hi=None, name=None, color=None):
        if lo is None:
            (x0, x1), _ = self.vb.viewRange()
            w = x1 - x0
            lo, hi = x0 + w * 0.4, x0 + w * 0.6
        self.sel_counter += 1
        sel = Selection(name or f"Sel {self.sel_counter}", lo, hi,
                        color or SEL_COLORS[(self.sel_counter - 1) % len(SEL_COLORS)])
        sel.region.sigRegionChanged.connect(lambda _, s=sel: self.on_region_changed(s))
        sel.region.sigRegionChangeFinished.connect(lambda *_: self.mark_unsaved())
        for item in sel.items():
            self.plot.addItem(item, ignoreBounds=True)
        self.selections.append(sel)
        self.view_dirty = True  # make room for the header band

        row = self.sel_table.rowCount()
        self.sel_table.blockSignals(True)
        self.sel_table.insertRow(row)
        name_item = QTableWidgetItem(sel.name)
        name_item.setForeground(QColor(sel.color))
        self.sel_table.setItem(row, 0, name_item)
        for col in (1, 2, 3):
            item = QTableWidgetItem("–")
            item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
            item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            self.sel_table.setItem(row, col, item)
        self.sel_table.blockSignals(False)
        self.set_active(row)
        self.mark_unsaved()
        return sel

    def remove_selection(self, sel=None):
        row = self.selections.index(sel) if sel is not None else self.active
        if row is None:
            return
        sel = self.selections.pop(row)
        for item in sel.items():
            self.plot.removeItem(item)
        self.sel_table.blockSignals(True)
        self.sel_table.removeRow(row)
        self.sel_table.blockSignals(False)
        self.active = None
        self.set_active(min(self.sel_table.rowCount() - 1, row))
        self.view_dirty = True
        self.mark_unsaved()

    def clear_selections(self):
        for sel in self.selections:
            for item in sel.items():
                self.plot.removeItem(item)
        self.selections.clear()
        self.sel_table.setRowCount(0)
        self.active = None
        self.sel_counter = 0
        self.update_selections()

    def set_active(self, row):
        if row is None or row < 0 or row >= len(self.selections):
            row = None
        self.active = row
        for i, sel in enumerate(self.selections):
            sel.set_active(i == row)
        if row is not None and self.sel_table.currentRow() != row:
            self.sel_table.blockSignals(True)
            self.sel_table.selectRow(row)
            self.sel_table.blockSignals(False)
        self.update_selections()

    def on_region_changed(self, sel):
        row = self.selections.index(sel)
        if row != self.active:
            self.set_active(row)
        else:
            self.update_selections()

    def on_sel_renamed(self, item):
        if item.column() != 0:
            return
        name = item.text().strip() or self.selections[item.row()].name
        self.selections[item.row()].rename(name)
        self.mark_unsaved()
        self.update_selections()

    def on_sel_double_click(self, row, col):
        if col != 0:
            self.set_active(row)
            self.zoom_selection()

    def zoom_selection(self):
        if self.active is None:
            return
        lo, hi = self.selections[self.active].bounds()
        self.act_follow.setChecked(False)
        self.auto_y = True
        self.vb.setXRange(lo, hi, padding=0.15)

    def on_mouse(self, evt):
        pos = evt[0]
        t = self.rec.t
        if not len(t) or not self.plot.sceneBoundingRect().contains(pos):
            self.vline.hide()
            self.hover.hide()
            return
        for sel in reversed(self.selections):
            if sel.header_hit(self.vb, pos):
                self.show_selection_tip(sel, pos)
                return
        x = self.vb.mapSceneToView(pos).x()
        i = int(np.clip(np.searchsorted(t, x), 1, len(t) - 1)) if len(t) > 1 else 0
        if i and abs(t[i - 1] - x) < abs(t[i] - x):
            i -= 1
        ti, epoch, v, unit, *_ = self.rec.sample(i)
        self.vline.setPos(ti)
        self.hover.setText(f"{fmt_duration(ti, True)}  ({fmt_clock(epoch)})\n{fmt_value(v, unit, 5)}")
        (x0, x1), (_, y1) = self.vb.viewRange()
        self.hover.setAnchor((1, 0) if ti > (x0 + x1) / 2 else (0, 0))
        self.hover.setPos(ti, y1)
        self.vline.show()
        self.hover.show()

    def on_plot_click(self, ev):
        if ev.button() != Qt.MouseButton.LeftButton:
            return
        pos = ev.scenePos()
        for sel in reversed(self.selections):
            if sel.close_hit(pos):
                self.remove_selection(sel)
                self.hover.hide()
                return
            if sel.header_hit(self.vb, pos):
                self.set_active(self.selections.index(sel))
                return

    def show_selection_tip(self, sel, pos):
        lo, hi = sel.bounds()
        texts = stats_texts(self.selection_stats(sel), self._unit, self.supply.value())
        rows = [("Δt", fmt_duration(hi - lo, True))]
        if self.rec.t0 is not None:
            rows.append(("Clock", f"{fmt_clock(self.rec.t0 + lo)} – {fmt_clock(self.rec.t0 + hi)}"))
        rows += [(name, texts[key]) for key, name in STATS_ROWS if key != "dur"]
        cells = "".join(f"<tr><td style='color:{MUTED}; padding-right:14px'>{k}</td>"
                        f"<td align='right'>{v}</td></tr>" for k, v in rows)
        self.hover.setHtml(f"<div style='color:{TEXT}'><b style='color:{sel.color}'>{html.escape(sel.name)}</b>"
                           f"<table cellspacing='0' cellpadding='1'>{cells}</table></div>")
        p = self.vb.mapSceneToView(pos)
        (x0, x1), (y0, y1) = self.vb.viewRange()
        self.hover.setAnchor((1 if p.x() > (x0 + x1) / 2 else 0, 1 if p.y() < (y0 + y1) / 2 else 0))
        self.hover.setPos(p.x(), p.y())
        self.vline.hide()
        self.hover.show()

    # files

    def confirm_discard(self):
        if not self.unsaved or not len(self.rec):
            return True
        res = QMessageBox.question(
            self, APP_NAME, "The recording has unsaved changes. Save them first?",
            QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Discard | QMessageBox.StandardButton.Cancel)
        if res == QMessageBox.StandardButton.Save:
            return self.save()
        return res == QMessageBox.StandardButton.Discard

    def reset_recording(self):
        self.rec.clear()
        self.clear_selections()
        self.log_path = None
        self.file_path = None
        self.from_file = False
        self.unsaved = False
        self.series.reset(None)
        self._unit = ""
        self.auto_y = True
        self.lbl_value.setText("–")
        self.lbl_mode.setText("no data")
        self.lbl_log.setText("")
        self.act_follow.setChecked(True)
        self.data_dirty = True
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

    def save(self):
        if self.file_path and self.file_path.endswith(PROJECT_EXT):
            return self.write_file(self.file_path)
        return self.save_as()

    def save_as(self):
        if not len(self.rec):
            QMessageBox.information(self, APP_NAME, "Nothing to save.")
            return False
        start = Path(self.settings.value("lastdir", str(Path.home()))) / f"ut71a_{datetime.now():%Y%m%d_%H%M%S}"
        project_filter = f"UT71A project (*{PROJECT_EXT})"
        path, chosen = QFileDialog.getSaveFileName(self, "Save recording", str(start),
                                                   f"{project_filter};;CSV samples only (*.csv)")
        if not path:
            return False
        if not path.endswith((PROJECT_EXT, ".csv")):
            path += PROJECT_EXT if chosen == project_filter else ".csv"
        return self.write_file(path)

    def write_file(self, path):
        try:
            if path.endswith(PROJECT_EXT):
                save_project(path, self.rec, [s.to_dict() for s in self.selections], self.supply.value())
            else:
                self.rec.save_csv(path)
        except OSError as e:
            QMessageBox.critical(self, APP_NAME, f"Save failed:\n{e}")
            return False
        self.settings.setValue("lastdir", str(Path(path).parent))
        if path.endswith(PROJECT_EXT):
            self.file_path = path
            self.unsaved = False
        elif not self.selections:
            self.unsaved = False
        self._update_title()
        note = "" if path.endswith(PROJECT_EXT) or not self.selections else " (selections are kept only in projects)"
        self.statusBar().showMessage(f"Saved {path}{note}", 6000)
        return True

    def open_file(self):
        if self.reader:
            self.stop_capture()
        if not self.confirm_discard():
            return
        start = self.settings.value("lastdir", str(LOG_DIR if LOG_DIR.exists() else Path.home()))
        path, _ = QFileDialog.getOpenFileName(
            self, "Open recording", start,
            f"Recordings (*{PROJECT_EXT} *.csv);;UT71A project (*{PROJECT_EXT});;CSV (*.csv);;All files (*)")
        if path:
            self.load_path(path)

    def load_path(self, path):
        self.reset_recording()
        try:
            if path.endswith(PROJECT_EXT):
                sels, supply_v = load_project(path, self.rec)
                if supply_v is not None:
                    self.supply.setValue(float(supply_v))
            else:
                self.rec.load_csv(path)
                sels = []
        except (OSError, ValueError, KeyError, TypeError, zipfile.BadZipFile) as e:
            self.rec.clear()
            msg = f"missing data: {e.args[0]}" if isinstance(e, KeyError) else str(e)
            QMessageBox.critical(self, APP_NAME, f"Cannot load {path}:\n{msg}")
            return
        self.settings.setValue("lastdir", str(Path(path).parent))
        self.file_path = path
        self.from_file = True
        self.show_readout(len(self.rec) - 1)
        self.act_follow.setChecked(False)
        self.process_new_data()
        self.fit_view()
        self.render()
        for s in sels:
            self.add_selection(float(s["start"]), float(s["end"]), s.get("name"), s.get("color"))
        self.set_active(0 if self.selections else None)
        self.unsaved = False
        self._update_title()

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
