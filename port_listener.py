#!/usr/bin/env python3
"""
Port Listener - a PuTTY-like serial (UART) monitor with CSV logging.

Two logging functions:
  1. Raw log  -> every received line, timestamped, junk-filtered, written to CSV.
  2. Tag log  -> only lines containing one of the user supplied tags
                 (e.g. "<info>") are written to a second CSV.

Requires: PyQt6, pyserial
"""

from __future__ import annotations

import csv
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime

import serial
from serial.tools import list_ports

import theme as theming

from PyQt6.QtCore import Qt, QThread, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QTextCursor
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QColorDialog,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QSplitter,
    QStatusBar,
    QVBoxLayout,
    QWidget,
)

APP_NAME = "Port Listener"

BAUD_RATES = [
    300, 1200, 2400, 4800, 9600, 14400, 19200, 28800, 38400, 57600,
    76800, 115200, 230400, 250000, 460800, 500000, 921600, 1000000, 2000000,
]

PARITIES = {
    "None": serial.PARITY_NONE,
    "Even": serial.PARITY_EVEN,
    "Odd": serial.PARITY_ODD,
    "Mark": serial.PARITY_MARK,
    "Space": serial.PARITY_SPACE,
}

STOP_BITS = {
    "1": serial.STOPBITS_ONE,
    "1.5": serial.STOPBITS_ONE_POINT_FIVE,
    "2": serial.STOPBITS_TWO,
}

DATA_BITS = {
    "8": serial.EIGHTBITS,
    "7": serial.SEVENBITS,
    "6": serial.SIXBITS,
    "5": serial.FIVEBITS,
}

LINE_ENDINGS = {
    "LF (\\n)": b"\n",
    "CR (\\r)": b"\r",
    "CRLF (\\r\\n)": b"\r\n",
    "None": b"",
}

MAX_TERMINAL_LINES = 20000

# ANSI / VT100 escape sequences that terminals interpret but CSV files should not keep.
ANSI_RE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")


# --------------------------------------------------------------------------- #
# Text handling
# --------------------------------------------------------------------------- #

@dataclass
class LineRecord:
    """One line received from the serial port."""
    stamp: datetime
    elapsed: float
    raw: str        # decoded, unmodified
    clean: str      # ANSI + control characters removed, stripped
    junk_ratio: float


def clean_line(text: str) -> str:
    """Remove escape sequences and non printable control characters."""
    text = ANSI_RE.sub("", text)
    return "".join(c for c in text if c == "\t" or (32 <= ord(c) < 127) or ord(c) > 159).strip()


SPACES_RE = re.compile(r"[ \t]{2,}")


def apply_removals(text: str, patterns: list[re.Pattern], tidy: bool) -> str:
    """Delete every match of every pattern from a line."""
    for pattern in patterns:
        text = pattern.sub("", text)
    if tidy:
        text = SPACES_RE.sub(" ", text).strip()
    return text


def junk_ratio(text: str) -> float:
    """
    Fraction of characters that carry no information.

    A normal log line is mostly letters, digits and spaces. A line produced by a
    baud rate mismatch or line noise looks like !"§!%"§%"§$ and scores near 1.0.
    """
    if not text:
        return 1.0
    useful = sum(1 for c in text if c.isalnum() or c.isspace())
    return 1.0 - useful / len(text)


# --------------------------------------------------------------------------- #
# Serial reader thread
# --------------------------------------------------------------------------- #

class SerialReader(QThread):
    """Reads the port in the background and emits complete lines in batches."""

    lines_received = pyqtSignal(list)      # list[LineRecord]
    failed = pyqtSignal(str)
    disconnected = pyqtSignal()

    # A partial line is flushed after this long without new bytes, so prompts
    # that are printed without a newline still show up.
    IDLE_FLUSH_S = 1.0
    BATCH_INTERVAL_S = 0.05

    def __init__(self, port: serial.Serial, parent=None):
        super().__init__(parent)
        self._port = port
        self._running = True
        self._t0 = time.monotonic()

    def stop(self) -> None:
        self._running = False

    def run(self) -> None:
        buffer = bytearray()
        batch: list[LineRecord] = []
        last_byte_at = time.monotonic()
        last_emit_at = time.monotonic()

        try:
            while self._running:
                try:
                    waiting = self._port.in_waiting
                except (OSError, serial.SerialException):
                    if self._running:
                        self.failed.emit("Port closed or device removed.")
                    break

                chunk = self._port.read(waiting or 1)
                now = time.monotonic()

                if chunk:
                    buffer.extend(chunk)
                    last_byte_at = now
                    while b"\n" in buffer:
                        raw_line, _, rest = buffer.partition(b"\n")
                        buffer = bytearray(rest)
                        batch.append(self._make_record(raw_line))
                elif buffer and (now - last_byte_at) >= self.IDLE_FLUSH_S:
                    batch.append(self._make_record(bytes(buffer)))
                    buffer.clear()

                if batch and (now - last_emit_at) >= self.BATCH_INTERVAL_S:
                    self.lines_received.emit(batch)
                    batch = []
                    last_emit_at = now

            if batch:
                self.lines_received.emit(batch)

        except serial.SerialException as exc:
            if self._running:
                self.failed.emit(str(exc))
        finally:
            try:
                if self._port.is_open:
                    self._port.close()
            except Exception:
                pass
            self.disconnected.emit()

    def _make_record(self, data: bytes) -> LineRecord:
        raw = data.decode("utf-8", errors="replace").rstrip("\r")
        cleaned = clean_line(raw)
        return LineRecord(
            stamp=datetime.now(),
            elapsed=time.monotonic() - self._t0,
            raw=raw,
            clean=cleaned,
            junk_ratio=junk_ratio(cleaned),
        )


# --------------------------------------------------------------------------- #
# CSV writer
# --------------------------------------------------------------------------- #

class CsvLogger:
    """Appends rows to a CSV file, flushing after every write."""

    def __init__(self, path: str, header: list[str]):
        new_file = not os.path.exists(path) or os.path.getsize(path) == 0
        self.path = path
        self._fh = open(path, "a", newline="", encoding="utf-8")
        self._writer = csv.writer(self._fh)
        if new_file:
            self._writer.writerow(header)
            self._fh.flush()
        self.rows = 0

    def write(self, row: list) -> None:
        self._writer.writerow(row)
        self._fh.flush()
        self.rows += 1

    def close(self) -> None:
        try:
            self._fh.close()
        except Exception:
            pass


DEFAULT_LOG_DIR = os.path.join(os.path.expanduser("~"), "Documents", "PortListener")


def default_path(prefix: str, folder: str | None = None) -> str:
    folder = folder or DEFAULT_LOG_DIR
    try:
        os.makedirs(folder, exist_ok=True)
    except OSError:
        folder = DEFAULT_LOG_DIR
        os.makedirs(folder, exist_ok=True)
    name = f"{prefix}_{datetime.now():%Y-%m-%d_%H-%M-%S}.csv"
    return os.path.join(folder, name)


# --------------------------------------------------------------------------- #
# Settings file
# --------------------------------------------------------------------------- #

SETTINGS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "settings.json")

DEFAULT_TAGS = "<info>\n<warn>\n<error>"

DEFAULTS: dict = {
    "theme": theming.DEFAULT_THEME,
    "save_on_exit": True,
    "baud": "115200",
    "data_bits": "8",
    "parity": "None",
    "stop_bits": "1",
    "flow": "None",
    "line_ending": "LF (\\n)",
    "show_timestamps": True,
    "autoscroll": True,
    "hide_junk": False,
    "clean_text": True,
    "drop_junk": True,
    "junk_threshold": 60,
    "drop_empty": True,
    "tags": DEFAULT_TAGS,
    "tags_regex": False,
    "tags_case": False,
    "tags_strip": False,
    "remove": "",
    "remove_regex": False,
    "remove_case": False,
    "remove_tidy": True,
    "remove_terminal": False,
    "colors": {},
    "raw_dir": DEFAULT_LOG_DIR,
    "tag_dir": DEFAULT_LOG_DIR,
    "window": [1300, 820],
    "splitter": [780, 520],
    "vsplitter": [420, 260],
}


def load_settings() -> dict:
    try:
        with open(SETTINGS_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_settings(data: dict) -> str | None:
    """Write the settings file. Returns an error message, or None on success."""
    try:
        with open(SETTINGS_PATH, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
            fh.write("\n")
        return None
    except OSError as exc:
        return str(exc)


# --------------------------------------------------------------------------- #
# Main window
# --------------------------------------------------------------------------- #

class MainWindow(QMainWindow):

    def __init__(self, settings: dict | None = None):
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.resize(1300, 820)

        self.serial_port: serial.Serial | None = None
        self.reader: SerialReader | None = None
        self.raw_logger: CsvLogger | None = None
        self.tag_logger: CsvLogger | None = None
        self.tag_patterns: list[tuple[str, re.Pattern]] = []

        self.total_lines = 0
        self.dropped_lines = 0
        self.tagged_lines = 0
        self._rx_bytes = 0

        self.theme_name = theming.DEFAULT_THEME
        self.colors = theming.PALETTES[self.theme_name]
        self.custom_colors: dict[str, dict[str, str]] = {}
        self.remove_patterns: list[re.Pattern] = []
        self._pending_splitter: list[int] | None = None
        self._pending_vsplitter: list[int] | None = None

        self._build_ui()
        self.refresh_ports()
        self.apply_settings(settings or {})
        self._rebuild_tags()
        self._update_connection_state(False)

        self._stats_timer = QTimer(self)
        self._stats_timer.timeout.connect(self._update_status)
        self._stats_timer.start(500)

    # ---------------------------------------------------------------- UI ----

    def _build_ui(self) -> None:
        central = QWidget()
        root = QVBoxLayout(central)
        root.setContentsMargins(12, 10, 12, 8)
        root.setSpacing(9)

        root.addLayout(self._build_header())
        root.addWidget(self._build_connection_bar())

        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.splitter.addWidget(self._build_left_panel())
        self.splitter.addWidget(self._build_side_panel())
        self.splitter.setStretchFactor(0, 3)
        self.splitter.setStretchFactor(1, 2)
        self.splitter.setSizes([780, 520])
        root.addWidget(self.splitter, 1)

        self.setCentralWidget(central)
        self.setStatusBar(QStatusBar())
        self._update_status()

    def _build_header(self) -> QHBoxLayout:
        row = QHBoxLayout()

        title = QLabel(APP_NAME)
        title.setObjectName("apptitle")
        subtitle = QLabel("UART monitor with CSV logging")
        subtitle.setObjectName("subtitle")
        row.addWidget(title)
        row.addWidget(subtitle)
        row.addStretch(1)

        self.theme_combo = QComboBox()
        self.theme_combo.addItem("Dark", "dark")
        self.theme_combo.addItem("Light", "light")
        self.theme_combo.setFixedWidth(110)
        self.theme_combo.currentIndexChanged.connect(
            lambda: self.set_theme(self.theme_combo.currentData())
        )

        self.autosave_cb = QCheckBox("Save on exit")
        self.autosave_cb.setChecked(True)
        self.autosave_cb.setToolTip("Write the current settings to settings.json when the app closes.")

        save_btn = QPushButton("Save settings")
        save_btn.clicked.connect(self.save_settings_now)
        reset_btn = QPushButton("Reset")
        reset_btn.setToolTip("Restore the built-in defaults.")
        reset_btn.clicked.connect(self.reset_settings)

        row.addWidget(QLabel("Theme"))
        row.addWidget(self.theme_combo)
        row.addSpacing(12)
        row.addWidget(self.autosave_cb)
        row.addWidget(save_btn)
        row.addWidget(reset_btn)
        return row

    def _build_connection_bar(self) -> QWidget:
        box = QGroupBox("Connection")
        layout = QGridLayout(box)
        layout.setHorizontalSpacing(10)

        self.port_combo = QComboBox()
        self.port_combo.setMinimumWidth(240)
        refresh_btn = QPushButton("Refresh")
        refresh_btn.clicked.connect(self.refresh_ports)

        self.baud_combo = QComboBox()
        self.baud_combo.setEditable(True)
        self.baud_combo.addItems([str(b) for b in BAUD_RATES])
        self.baud_combo.setCurrentText("115200")
        self.baud_combo.setMinimumWidth(110)

        self.databits_combo = QComboBox()
        self.databits_combo.addItems(DATA_BITS.keys())

        self.parity_combo = QComboBox()
        self.parity_combo.addItems(PARITIES.keys())

        self.stopbits_combo = QComboBox()
        self.stopbits_combo.addItems(STOP_BITS.keys())

        self.flow_combo = QComboBox()
        self.flow_combo.addItems(["None", "RTS/CTS", "XON/XOFF"])

        self.connect_btn = QPushButton("Connect")
        self.connect_btn.setObjectName("primary")
        self.connect_btn.setMinimumWidth(120)
        self.connect_btn.clicked.connect(self.toggle_connection)

        layout.addWidget(QLabel("Port"), 0, 0)
        layout.addWidget(self.port_combo, 0, 1)
        layout.addWidget(refresh_btn, 0, 2)
        layout.addWidget(QLabel("Baud rate"), 0, 3)
        layout.addWidget(self.baud_combo, 0, 4)
        layout.addWidget(QLabel("Data bits"), 0, 5)
        layout.addWidget(self.databits_combo, 0, 6)
        layout.addWidget(QLabel("Parity"), 0, 7)
        layout.addWidget(self.parity_combo, 0, 8)
        layout.addWidget(QLabel("Stop bits"), 0, 9)
        layout.addWidget(self.stopbits_combo, 0, 10)
        layout.addWidget(QLabel("Flow"), 0, 11)
        layout.addWidget(self.flow_combo, 0, 12)
        layout.addWidget(self.connect_btn, 0, 13)
        layout.setColumnStretch(1, 1)
        return box

    def _build_left_panel(self) -> QWidget:
        """Terminal above, matched lines below, send row at the bottom."""
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        self.vsplitter = QSplitter(Qt.Orientation.Vertical)
        self.vsplitter.addWidget(self._build_terminal_panel())
        self.vsplitter.addWidget(self._build_matched_panel())
        self.vsplitter.setStretchFactor(0, 3)
        self.vsplitter.setStretchFactor(1, 2)
        self.vsplitter.setSizes([420, 260])
        layout.addWidget(self.vsplitter, 1)

        send_row = QHBoxLayout()
        self.send_edit = QLineEdit()
        self.send_edit.setPlaceholderText("Type a command and press Enter to send to the board…")
        self.send_edit.returnPressed.connect(self.send_text)
        self.ending_combo = QComboBox()
        self.ending_combo.addItems(LINE_ENDINGS.keys())
        self.send_btn = QPushButton("Send")
        self.send_btn.clicked.connect(self.send_text)
        send_row.addWidget(self.send_edit, 1)
        send_row.addWidget(self.ending_combo)
        send_row.addWidget(self.send_btn)
        layout.addLayout(send_row)

        return panel

    def _build_matched_panel(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)

        header = QHBoxLayout()
        header.addWidget(QLabel("<b>Matched lines</b>"))
        self.matched_count = QLabel()
        header.addWidget(self.matched_count)
        header.addStretch(1)
        layout.addLayout(header)

        self.tag_view = QPlainTextEdit()
        self.tag_view.setObjectName("tagview")
        self.tag_view.setReadOnly(True)
        self.tag_view.setMaximumBlockCount(MAX_TERMINAL_LINES)
        self.tag_view.setFont(self._mono_font())
        layout.addWidget(self.tag_view, 1)
        return panel

    def _build_terminal_panel(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)

        header = QHBoxLayout()
        header.addWidget(QLabel("<b>Terminal</b>"))
        header.addStretch(1)

        self.show_time_cb = QCheckBox("Timestamps")
        self.show_time_cb.setChecked(True)
        self.autoscroll_cb = QCheckBox("Auto scroll")
        self.autoscroll_cb.setChecked(True)
        self.hide_junk_cb = QCheckBox("Hide junk lines")
        self.hide_junk_cb.setToolTip("Also hide filtered-out lines in this view.")
        clear_btn = QPushButton("Clear")
        clear_btn.clicked.connect(self.terminal_clear)

        for w in (self.show_time_cb, self.autoscroll_cb, self.hide_junk_cb, clear_btn):
            header.addWidget(w)
        layout.addLayout(header)

        self.terminal = QPlainTextEdit()
        self.terminal.setObjectName("terminal")
        self.terminal.setReadOnly(True)
        self.terminal.setMaximumBlockCount(MAX_TERMINAL_LINES)
        self.terminal.setFont(self._mono_font())
        layout.addWidget(self.terminal, 1)
        return panel

    def _build_side_panel(self) -> QWidget:
        inner = QWidget()
        layout = QVBoxLayout(inner)
        layout.setContentsMargins(0, 0, 6, 0)
        layout.setSpacing(10)
        layout.addWidget(self._build_raw_log_box())
        layout.addWidget(self._build_tag_box())
        layout.addWidget(self._build_remove_box())
        layout.addWidget(self._build_colors_box())
        layout.addStretch(1)

        scroll = QScrollArea()
        scroll.setWidget(inner)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setMinimumWidth(380)
        return scroll

    def _build_raw_log_box(self) -> QWidget:
        box = QGroupBox("1 · Raw log → CSV (timestamped, junk filtered)")
        layout = QVBoxLayout(box)

        path_row = QHBoxLayout()
        self.raw_path_edit = QLineEdit(default_path("raw"))
        self.raw_path_edit.setMinimumWidth(120)
        browse = QPushButton("Browse…")
        browse.setFixedWidth(84)
        browse.clicked.connect(lambda: self._browse_into(self.raw_path_edit, "raw"))
        path_row.addWidget(self.raw_path_edit, 1)
        path_row.addWidget(browse)
        layout.addLayout(path_row)

        opts = QFormLayout()
        self.clean_cb = QCheckBox("Strip escape codes and control characters")
        self.clean_cb.setChecked(True)
        opts.addRow(self.clean_cb)

        self.drop_junk_cb = QCheckBox("Drop noise lines above")
        self.drop_junk_cb.setChecked(True)
        self.junk_spin = QSpinBox()
        self.junk_spin.setRange(0, 100)
        self.junk_spin.setValue(60)
        self.junk_spin.setSuffix(" %")
        self.junk_spin.setFixedWidth(78)
        junk_tip = ('Share of characters that are not letters, digits or spaces.\n'
                    'A line like !"§!%"§%"§$ scores 100 %; normal log text stays below 40 %.')
        self.drop_junk_cb.setToolTip(junk_tip)
        self.junk_spin.setToolTip(junk_tip)
        junk_row = QHBoxLayout()
        junk_row.addWidget(self.drop_junk_cb)
        junk_row.addWidget(self.junk_spin)
        junk_row.addWidget(QLabel("junk"))
        junk_row.addStretch(1)
        opts.addRow(junk_row)

        self.drop_empty_cb = QCheckBox("Drop empty lines")
        self.drop_empty_cb.setChecked(True)
        opts.addRow(self.drop_empty_cb)
        layout.addLayout(opts)

        btn_row = QHBoxLayout()
        self.raw_record_btn = QPushButton("Start recording")
        self.raw_record_btn.setCheckable(True)
        self.raw_record_btn.clicked.connect(self.toggle_raw_recording)
        self.raw_status = QLabel("not recording")
        btn_row.addWidget(self.raw_record_btn)
        btn_row.addWidget(self.raw_status, 1)
        layout.addLayout(btn_row)

        return box

    def _build_tag_box(self) -> QWidget:
        box = QGroupBox("2 · Tag log → CSV (keep only lines containing a tag)")
        layout = QVBoxLayout(box)

        layout.addWidget(QLabel("Tags / keywords — one per line, or comma separated:"))
        self.tags_edit = QPlainTextEdit(DEFAULT_TAGS)
        self.tags_edit.setFixedHeight(88)
        self.tags_edit.setFont(self._mono_font())
        self.tags_edit.textChanged.connect(self._rebuild_tags)
        layout.addWidget(self.tags_edit)

        opt_row = QHBoxLayout()
        self.regex_cb = QCheckBox("Regex")
        self.regex_cb.setToolTip("Treat each tag as a regular expression.")
        self.regex_cb.toggled.connect(self._rebuild_tags)
        self.case_cb = QCheckBox("Case sensitive")
        self.case_cb.toggled.connect(self._rebuild_tags)
        self.strip_tag_cb = QCheckBox("Strip tag from saved text")
        opt_row.addWidget(self.regex_cb)
        opt_row.addWidget(self.case_cb)
        opt_row.addWidget(self.strip_tag_cb)
        opt_row.addStretch(1)
        layout.addLayout(opt_row)

        self.tags_status = QLabel()
        layout.addWidget(self.tags_status)

        path_row = QHBoxLayout()
        self.tag_path_edit = QLineEdit(default_path("tagged"))
        self.tag_path_edit.setMinimumWidth(120)
        browse = QPushButton("Browse…")
        browse.setFixedWidth(84)
        browse.clicked.connect(lambda: self._browse_into(self.tag_path_edit, "tagged"))
        path_row.addWidget(self.tag_path_edit, 1)
        path_row.addWidget(browse)
        layout.addLayout(path_row)

        btn_row = QHBoxLayout()
        self.tag_record_btn = QPushButton("Start recording")
        self.tag_record_btn.setCheckable(True)
        self.tag_record_btn.clicked.connect(self.toggle_tag_recording)
        self.tag_status = QLabel("not recording")
        btn_row.addWidget(self.tag_record_btn)
        btn_row.addWidget(self.tag_status, 1)
        layout.addLayout(btn_row)

        return box

    def _build_remove_box(self) -> QWidget:
        box = QGroupBox("3 · Remove words / symbols from every saved line")
        layout = QVBoxLayout(box)

        hint = QLabel(
            "Applies to both CSV files. One entry per line — so commas and other\n"
            "symbols can be filtered too. Tags are matched before this runs."
        )
        hint.setObjectName("subtitle")
        layout.addWidget(hint)

        self.remove_edit = QPlainTextEdit()
        self.remove_edit.setPlaceholderText("e.g.\n<info>\n;;\n0x\n [V]")
        self.remove_edit.setFixedHeight(84)
        self.remove_edit.setFont(self._mono_font())
        self.remove_edit.textChanged.connect(self._rebuild_removals)
        layout.addWidget(self.remove_edit)

        opt_row = QHBoxLayout()
        self.remove_regex_cb = QCheckBox("Regex")
        self.remove_regex_cb.setToolTip("Treat each entry as a regular expression.")
        self.remove_regex_cb.toggled.connect(self._rebuild_removals)
        self.remove_case_cb = QCheckBox("Case sensitive")
        self.remove_case_cb.toggled.connect(self._rebuild_removals)
        opt_row.addWidget(self.remove_regex_cb)
        opt_row.addWidget(self.remove_case_cb)
        opt_row.addStretch(1)
        layout.addLayout(opt_row)

        opt_row2 = QHBoxLayout()
        self.remove_tidy_cb = QCheckBox("Tidy leftover spaces")
        self.remove_tidy_cb.setChecked(True)
        self.remove_tidy_cb.setToolTip("Collapse double spaces and trim the ends after removing text.")
        self.remove_terminal_cb = QCheckBox("Apply in terminal too")
        self.remove_terminal_cb.setToolTip(
            "Off by default, so the terminal keeps showing what the board really sent."
        )
        opt_row2.addWidget(self.remove_tidy_cb)
        opt_row2.addWidget(self.remove_terminal_cb)
        opt_row2.addStretch(1)
        layout.addLayout(opt_row2)

        self.remove_status = QLabel()
        layout.addWidget(self.remove_status)

        return box

    def _build_colors_box(self) -> QWidget:
        box = QGroupBox("Colours")
        layout = QVBoxLayout(box)

        hint = QLabel("Click a swatch to change it. Saved per theme with your settings.")
        hint.setObjectName("subtitle")
        layout.addWidget(hint)

        grid = QGridLayout()
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(6)
        self.swatches: dict[str, QPushButton] = {}
        for index, (key, label) in enumerate(theming.EDITABLE):
            row, col = divmod(index, 2)
            swatch = QPushButton()
            swatch.setFixedSize(30, 20)
            swatch.setCursor(Qt.CursorShape.PointingHandCursor)
            swatch.clicked.connect(lambda _=False, k=key, n=label: self.pick_colour(k, n))
            self.swatches[key] = swatch
            grid.addWidget(swatch, row, col * 2)
            grid.addWidget(QLabel(label), row, col * 2 + 1)
        grid.setColumnStretch(1, 1)
        grid.setColumnStretch(3, 1)
        layout.addLayout(grid)

        reset = QPushButton("Reset colours of this theme")
        reset.clicked.connect(self.reset_colours)
        layout.addWidget(reset)

        return box

    @staticmethod
    def _mono_font() -> QFont:
        font = QFont("Menlo")
        font.setStyleHint(QFont.StyleHint.Monospace)
        font.setPointSize(11)
        return font

    def _browse_into(self, edit: QLineEdit, prefix: str) -> None:
        start = edit.text() or default_path(prefix)
        path, _ = QFileDialog.getSaveFileName(self, "Choose CSV file", start, "CSV files (*.csv)")
        if path:
            if not path.lower().endswith(".csv"):
                path += ".csv"
            edit.setText(path)

    # ------------------------------------------------------------ theme ----

    def set_theme(self, name: str | None) -> None:
        if name not in theming.PALETTES:
            name = theming.DEFAULT_THEME
        self.theme_name = name
        self.refresh_theme()

    def refresh_theme(self) -> None:
        """Re-apply the current theme plus the user's colour overrides."""
        overrides = self.custom_colors.get(self.theme_name, {})
        self.colors = theming.apply(QApplication.instance(), self.theme_name, overrides)
        self._refresh_swatches()
        self._rebuild_tags()          # re-colours the hint labels
        self._rebuild_removals()
        self._update_status()

    def _refresh_swatches(self) -> None:
        for key, button in self.swatches.items():
            button.setStyleSheet(
                f"background:{self.colors[key]};"
                f"border:1px solid {self.colors['border2']};"
                "border-radius:4px;"
            )
            button.setToolTip(self.colors[key])

    def pick_colour(self, key: str, label: str) -> None:
        chosen = QColorDialog.getColor(
            QColor(self.colors[key]), self, f"{label} colour",
            QColorDialog.ColorDialogOption.DontUseNativeDialog,
        )
        if not chosen.isValid():
            return
        self.custom_colors.setdefault(self.theme_name, {})[key] = chosen.name()
        self.refresh_theme()

    def reset_colours(self) -> None:
        if self.custom_colors.pop(self.theme_name, None) is None:
            return
        self.refresh_theme()
        self._append_system(f"--- {self.theme_name} theme colours reset ---")

    # --------------------------------------------------------- settings ----

    def collect_settings(self) -> dict:
        return {
            "theme": self.theme_name,
            "save_on_exit": self.autosave_cb.isChecked(),
            "port": self.port_combo.currentData(),
            "baud": self.baud_combo.currentText(),
            "data_bits": self.databits_combo.currentText(),
            "parity": self.parity_combo.currentText(),
            "stop_bits": self.stopbits_combo.currentText(),
            "flow": self.flow_combo.currentText(),
            "line_ending": self.ending_combo.currentText(),
            "show_timestamps": self.show_time_cb.isChecked(),
            "autoscroll": self.autoscroll_cb.isChecked(),
            "hide_junk": self.hide_junk_cb.isChecked(),
            "clean_text": self.clean_cb.isChecked(),
            "drop_junk": self.drop_junk_cb.isChecked(),
            "junk_threshold": self.junk_spin.value(),
            "drop_empty": self.drop_empty_cb.isChecked(),
            "tags": self.tags_edit.toPlainText(),
            "tags_regex": self.regex_cb.isChecked(),
            "tags_case": self.case_cb.isChecked(),
            "tags_strip": self.strip_tag_cb.isChecked(),
            "remove": self.remove_edit.toPlainText(),
            "remove_regex": self.remove_regex_cb.isChecked(),
            "remove_case": self.remove_case_cb.isChecked(),
            "remove_tidy": self.remove_tidy_cb.isChecked(),
            "remove_terminal": self.remove_terminal_cb.isChecked(),
            "colors": self.custom_colors,
            "raw_dir": os.path.dirname(self.raw_path_edit.text()) or DEFAULT_LOG_DIR,
            "tag_dir": os.path.dirname(self.tag_path_edit.text()) or DEFAULT_LOG_DIR,
            "window": [self.width(), self.height()],
            "splitter": self.splitter.sizes(),
            "vsplitter": self.vsplitter.sizes(),
        }

    def apply_settings(self, data: dict) -> None:
        """Apply a settings dict. Missing or unusable keys keep their defaults."""

        def combo_text(combo: QComboBox, key: str) -> None:
            value = data.get(key)
            if isinstance(value, str) and value:
                index = combo.findText(value)
                if index >= 0:
                    combo.setCurrentIndex(index)
                elif combo.isEditable():
                    combo.setCurrentText(value)

        def check(box: QCheckBox, key: str) -> None:
            if isinstance(data.get(key), bool):
                box.setChecked(data[key])

        # Colour overrides must land before the theme is applied.
        self.custom_colors = {}
        stored = data.get("colors")
        if isinstance(stored, dict):
            for theme_key, values in stored.items():
                if theme_key in theming.PALETTES and isinstance(values, dict):
                    valid = {
                        k: v for k, v in values.items()
                        if k in theming.PALETTES[theme_key]
                        and isinstance(v, str) and QColor(v).isValid()
                    }
                    if valid:
                        self.custom_colors[theme_key] = valid

        theme_name = data.get("theme", theming.DEFAULT_THEME)
        index = self.theme_combo.findData(theme_name)
        self.theme_combo.blockSignals(True)
        self.theme_combo.setCurrentIndex(index if index >= 0 else 0)
        self.theme_combo.blockSignals(False)
        self.set_theme(self.theme_combo.currentData())

        check(self.autosave_cb, "save_on_exit")

        port = data.get("port")
        if isinstance(port, str):
            index = self.port_combo.findData(port)
            if index >= 0:
                self.port_combo.setCurrentIndex(index)

        combo_text(self.baud_combo, "baud")
        combo_text(self.databits_combo, "data_bits")
        combo_text(self.parity_combo, "parity")
        combo_text(self.stopbits_combo, "stop_bits")
        combo_text(self.flow_combo, "flow")
        combo_text(self.ending_combo, "line_ending")

        check(self.show_time_cb, "show_timestamps")
        check(self.autoscroll_cb, "autoscroll")
        check(self.hide_junk_cb, "hide_junk")
        check(self.clean_cb, "clean_text")
        check(self.drop_junk_cb, "drop_junk")
        check(self.drop_empty_cb, "drop_empty")
        check(self.regex_cb, "tags_regex")
        check(self.case_cb, "tags_case")
        check(self.strip_tag_cb, "tags_strip")
        check(self.remove_regex_cb, "remove_regex")
        check(self.remove_case_cb, "remove_case")
        check(self.remove_tidy_cb, "remove_tidy")
        check(self.remove_terminal_cb, "remove_terminal")

        if isinstance(data.get("junk_threshold"), (int, float)):
            self.junk_spin.setValue(int(data["junk_threshold"]))

        if isinstance(data.get("tags"), str):
            self.tags_edit.setPlainText(data["tags"])
        if isinstance(data.get("remove"), str):
            self.remove_edit.setPlainText(data["remove"])

        # Folders are remembered; the file name itself is always freshly dated.
        if isinstance(data.get("raw_dir"), str) and data["raw_dir"]:
            self.raw_path_edit.setText(default_path("raw", data["raw_dir"]))
        if isinstance(data.get("tag_dir"), str) and data["tag_dir"]:
            self.tag_path_edit.setText(default_path("tagged", data["tag_dir"]))

        size = data.get("window")
        if isinstance(size, list) and len(size) == 2:
            try:
                self.resize(max(900, int(size[0])), max(600, int(size[1])))
            except (TypeError, ValueError):
                pass

        def valid_sizes(key: str) -> list[int] | None:
            sizes = data.get(key)
            if (isinstance(sizes, list) and len(sizes) == 2
                    and all(isinstance(s, int) for s in sizes) and sum(sizes) > 0):
                return sizes
            return None

        # The layout overrules setSizes() until the window is on screen.
        self._pending_splitter = valid_sizes("splitter")
        self._pending_vsplitter = valid_sizes("vsplitter")
        if self._pending_splitter:
            self.splitter.setSizes(self._pending_splitter)
        if self._pending_vsplitter:
            self.vsplitter.setSizes(self._pending_vsplitter)

    def save_settings_now(self) -> None:
        error = save_settings(self.collect_settings())
        if error:
            QMessageBox.critical(self, APP_NAME, f"Could not save settings:\n\n{error}")
        else:
            self._append_system(f"--- settings saved: {SETTINGS_PATH} ---")

    def reset_settings(self) -> None:
        answer = QMessageBox.question(
            self, APP_NAME,
            "Restore the default settings?\n\nThe saved settings.json file will be deleted.",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            if os.path.exists(SETTINGS_PATH):
                os.remove(SETTINGS_PATH)
        except OSError as exc:
            QMessageBox.critical(self, APP_NAME, f"Could not delete settings file:\n\n{exc}")
            return
        self.apply_settings(DEFAULTS)
        self.autosave_cb.setChecked(False)
        self._append_system("--- settings reset to defaults (turn 'Save on exit' back on to keep changes) ---")

    # -------------------------------------------------------- connection ----

    def refresh_ports(self) -> None:
        current = self.port_combo.currentData()
        self.port_combo.clear()
        for info in sorted(list_ports.comports(), key=lambda p: p.device):
            label = f"{info.device}  —  {info.description}" if info.description else info.device
            self.port_combo.addItem(label, info.device)
        if self.port_combo.count() == 0:
            self.port_combo.addItem("No serial ports found", None)
        elif current:
            index = self.port_combo.findData(current)
            if index >= 0:
                self.port_combo.setCurrentIndex(index)

    def toggle_connection(self) -> None:
        if self.reader is not None:
            self.disconnect_port()
        else:
            self.connect_port()

    def connect_port(self) -> None:
        device = self.port_combo.currentData()
        if not device:
            QMessageBox.warning(self, APP_NAME, "No serial port selected.")
            return

        try:
            baud = int(self.baud_combo.currentText().strip())
            if baud <= 0:
                raise ValueError
        except ValueError:
            QMessageBox.warning(self, APP_NAME, "Baud rate must be a positive whole number.")
            return

        flow = self.flow_combo.currentText()
        try:
            port = serial.Serial(
                port=device,
                baudrate=baud,
                bytesize=DATA_BITS[self.databits_combo.currentText()],
                parity=PARITIES[self.parity_combo.currentText()],
                stopbits=STOP_BITS[self.stopbits_combo.currentText()],
                rtscts=(flow == "RTS/CTS"),
                xonxoff=(flow == "XON/XOFF"),
                timeout=0.05,
                write_timeout=2.0,
            )
        except (serial.SerialException, ValueError, OSError) as exc:
            QMessageBox.critical(self, APP_NAME, f"Could not open {device}:\n\n{exc}")
            return

        self.serial_port = port
        self._rx_bytes = 0
        self.reader = SerialReader(port, self)
        self.reader.lines_received.connect(self.on_lines)
        self.reader.failed.connect(self.on_reader_failed)
        self.reader.disconnected.connect(self.on_reader_finished)
        self.reader.start()

        self._append_system(f"--- connected to {device} at {baud} baud ---")
        self._update_connection_state(True)

    def disconnect_port(self) -> None:
        if self.reader is not None:
            self.reader.stop()
            self.reader.wait(2000)

    def on_reader_failed(self, message: str) -> None:
        self._append_system(f"--- error: {message} ---")

    def on_reader_finished(self) -> None:
        self.reader = None
        self.serial_port = None
        self._append_system("--- disconnected ---")
        self._update_connection_state(False)

    def _update_connection_state(self, connected: bool) -> None:
        self.connect_btn.setText("Disconnect" if connected else "Connect")
        for widget in (
            self.port_combo, self.baud_combo, self.databits_combo,
            self.parity_combo, self.stopbits_combo, self.flow_combo,
        ):
            widget.setEnabled(not connected)
        self.send_edit.setEnabled(connected)
        self.send_btn.setEnabled(connected)

    def send_text(self) -> None:
        if not (self.serial_port and self.serial_port.is_open):
            return
        text = self.send_edit.text()
        payload = text.encode("utf-8") + LINE_ENDINGS[self.ending_combo.currentText()]
        try:
            self.serial_port.write(payload)
        except (serial.SerialException, serial.SerialTimeoutException, OSError) as exc:
            self._append_system(f"--- send failed: {exc} ---")
            return
        self._append_system(f">>> {text}")
        self.send_edit.clear()

    # ------------------------------------------------------------- data ----

    def on_lines(self, records: list[LineRecord]) -> None:
        threshold = self.junk_spin.value() / 100.0
        drop_junk = self.drop_junk_cb.isChecked()
        drop_empty = self.drop_empty_cb.isChecked()
        use_clean = self.clean_cb.isChecked()
        show_time = self.show_time_cb.isChecked()
        hide_junk = self.hide_junk_cb.isChecked()
        removals = self.remove_patterns
        tidy = self.remove_tidy_cb.isChecked()
        strip_in_terminal = self.remove_terminal_cb.isChecked()
        strip_tag = self.strip_tag_cb.isChecked()

        terminal_out: list[str] = []
        tagged_out: list[str] = []

        for rec in records:
            self.total_lines += 1
            self._rx_bytes += len(rec.raw) + 1

            text = rec.clean if use_clean else rec.raw
            is_junk = (drop_empty and not rec.clean) or (drop_junk and rec.junk_ratio > threshold)
            if is_junk:
                self.dropped_lines += 1

            if not (hide_junk and is_junk):
                shown = apply_removals(text, removals, tidy) if strip_in_terminal else text
                prefix = f"[{rec.stamp.strftime('%H:%M:%S.%f')[:-3]}] " if show_time else ""
                terminal_out.append(prefix + shown)

            if is_junk:
                continue

            if self.raw_logger is not None:
                self.raw_logger.write([
                    rec.stamp.strftime("%Y-%m-%d"),
                    rec.stamp.strftime("%H:%M:%S.%f")[:-3],
                    f"{rec.elapsed:.3f}",
                    apply_removals(text, removals, tidy),
                ])

            # Tags are matched on the untouched line, so removing a word cannot
            # stop it from being recognised as a tag.
            tag = self._match_tag(rec.clean)
            if tag is not None:
                self.tagged_lines += 1
                saved = self._strip_tag(rec.clean, tag) if strip_tag else rec.clean
                saved = apply_removals(saved, removals, tidy)
                tagged_out.append(f"[{rec.stamp:%H:%M:%S}] {saved}")
                if self.tag_logger is not None:
                    self.tag_logger.write([
                        rec.stamp.strftime("%Y-%m-%d"),
                        rec.stamp.strftime("%H:%M:%S.%f")[:-3],
                        f"{rec.elapsed:.3f}",
                        tag,
                        saved,
                    ])

        if terminal_out:
            self._append_block(self.terminal, terminal_out, self.autoscroll_cb.isChecked())
        if tagged_out:
            self._append_block(self.tag_view, tagged_out, True)

    def _match_tag(self, text: str) -> str | None:
        for label, pattern in self.tag_patterns:
            if pattern.search(text):
                return label
        return None

    def _strip_tag(self, text: str, label: str) -> str:
        for tag_label, pattern in self.tag_patterns:
            if tag_label == label:
                return pattern.sub("", text, count=1).strip()
        return text

    def _rebuild_tags(self) -> None:
        flags = 0 if self.case_cb.isChecked() else re.IGNORECASE
        raw = self.tags_edit.toPlainText()
        tokens = [t.strip() for line in raw.splitlines() for t in line.split(",")]
        tokens = [t for t in tokens if t]

        patterns: list[tuple[str, re.Pattern]] = []
        bad: list[str] = []
        for token in tokens:
            source = token if self.regex_cb.isChecked() else re.escape(token)
            try:
                patterns.append((token, re.compile(source, flags)))
            except re.error:
                bad.append(token)

        self.tag_patterns = patterns
        if bad:
            self.tags_status.setText(f"⚠ invalid regex: {', '.join(bad)}")
            colour = self.colors["warn"]
        elif patterns:
            self.tags_status.setText(f"{len(patterns)} tag(s) active — other lines are ignored.")
            colour = self.colors["text_dim"]
        else:
            self.tags_status.setText("No tags entered — nothing will be matched.")
            colour = self.colors["text_dim"]
        self.tags_status.setStyleSheet(f"color:{colour};")

    def _rebuild_removals(self) -> None:
        """Compile the 'remove these words/symbols' list. One entry per line."""
        flags = 0 if self.remove_case_cb.isChecked() else re.IGNORECASE
        tokens = [line for line in self.remove_edit.toPlainText().splitlines() if line.strip()]

        patterns: list[re.Pattern] = []
        bad: list[str] = []
        for token in tokens:
            source = token if self.remove_regex_cb.isChecked() else re.escape(token)
            try:
                patterns.append(re.compile(source, flags))
            except re.error:
                bad.append(token)

        self.remove_patterns = patterns
        if bad:
            self.remove_status.setText(f"⚠ invalid regex: {', '.join(bad)}")
            colour = self.colors["warn"]
        elif patterns:
            self.remove_status.setText(f"{len(patterns)} entr(y/ies) will be stripped from saved lines.")
            colour = self.colors["text_dim"]
        else:
            self.remove_status.setText("Nothing to remove — lines are saved as they arrive.")
            colour = self.colors["text_dim"]
        self.remove_status.setStyleSheet(f"color:{colour};")

    # -------------------------------------------------------- recording ----

    def toggle_raw_recording(self) -> None:
        if self.raw_logger is None:
            path = self.raw_path_edit.text().strip() or default_path("raw")
            try:
                self.raw_logger = CsvLogger(path, ["date", "time", "elapsed_s", "line"])
            except OSError as exc:
                QMessageBox.critical(self, APP_NAME, f"Could not open log file:\n\n{exc}")
                self.raw_record_btn.setChecked(False)
                return
            self.raw_path_edit.setEnabled(False)
            self.raw_record_btn.setText("Stop recording")
            self.raw_record_btn.setChecked(True)
        else:
            self.raw_logger.close()
            self._append_system(f"--- raw log saved: {self.raw_logger.path} ---")
            self.raw_logger = None
            self.raw_path_edit.setEnabled(True)
            self.raw_path_edit.setText(default_path("raw"))
            self.raw_record_btn.setText("Start recording")
            self.raw_record_btn.setChecked(False)
        self._update_status()

    def toggle_tag_recording(self) -> None:
        if self.tag_logger is None:
            if not self.tag_patterns:
                QMessageBox.warning(self, APP_NAME, "Enter at least one tag before recording.")
                self.tag_record_btn.setChecked(False)
                return
            path = self.tag_path_edit.text().strip() or default_path("tagged")
            try:
                self.tag_logger = CsvLogger(path, ["date", "time", "elapsed_s", "tag", "line"])
            except OSError as exc:
                QMessageBox.critical(self, APP_NAME, f"Could not open log file:\n\n{exc}")
                self.tag_record_btn.setChecked(False)
                return
            self.tag_path_edit.setEnabled(False)
            self.tag_record_btn.setText("Stop recording")
            self.tag_record_btn.setChecked(True)
        else:
            self.tag_logger.close()
            self._append_system(f"--- tag log saved: {self.tag_logger.path} ---")
            self.tag_logger = None
            self.tag_path_edit.setEnabled(True)
            self.tag_path_edit.setText(default_path("tagged"))
            self.tag_record_btn.setText("Start recording")
            self.tag_record_btn.setChecked(False)
        self._update_status()

    # ----------------------------------------------------------- output ----

    @staticmethod
    def _append_block(view: QPlainTextEdit, lines: list[str], autoscroll: bool) -> None:
        bar = view.verticalScrollBar()
        at_bottom = bar.value() >= bar.maximum() - 4
        view.appendPlainText("\n".join(lines))
        if autoscroll or at_bottom:
            view.moveCursor(QTextCursor.MoveOperation.End)
            bar.setValue(bar.maximum())

    def _append_system(self, text: str) -> None:
        self.terminal.appendPlainText(text)
        if self.autoscroll_cb.isChecked():
            self.terminal.moveCursor(QTextCursor.MoveOperation.End)

    def terminal_clear(self) -> None:
        self.terminal.clear()
        self.tag_view.clear()

    def _update_status(self) -> None:
        state = "connected" if self.reader is not None else "disconnected"
        raw_state = f"raw → {os.path.basename(self.raw_logger.path)} ({self.raw_logger.rows})" \
            if self.raw_logger else "raw off"
        tag_state = f"tags → {os.path.basename(self.tag_logger.path)} ({self.tag_logger.rows})" \
            if self.tag_logger else "tags off"
        self.statusBar().showMessage(
            f"{state}   |   lines {self.total_lines}   filtered {self.dropped_lines}   "
            f"matched {self.tagged_lines}   |   {raw_state}   |   {tag_state}"
        )
        self.raw_status.setText(
            f"recording — {self.raw_logger.rows} rows" if self.raw_logger else "not recording"
        )
        self.tag_status.setText(
            f"recording — {self.tag_logger.rows} rows" if self.tag_logger else "not recording"
        )
        self.matched_count.setText(f"({self.tagged_lines})" if self.tagged_lines else "")
        self.matched_count.setStyleSheet(f"color:{self.colors['text_dim']};")

    # ------------------------------------------------------------ close ----

    def showEvent(self, event) -> None:
        super().showEvent(event)
        # Queued so these land after the first layout pass, which would
        # otherwise redistribute the panes from their size hints.
        for splitter, attr in ((self.splitter, "_pending_splitter"),
                               (self.vsplitter, "_pending_vsplitter")):
            sizes = getattr(self, attr)
            if sizes is not None:
                setattr(self, attr, None)
                QTimer.singleShot(0, lambda s=splitter, z=sizes: s.setSizes(z))

    def closeEvent(self, event) -> None:
        if self.autosave_cb.isChecked():
            save_settings(self.collect_settings())
        self.disconnect_port()
        if self.raw_logger:
            self.raw_logger.close()
        if self.tag_logger:
            self.tag_logger.close()
        event.accept()


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    settings = load_settings()
    start_theme = settings.get("theme", theming.DEFAULT_THEME)
    overrides = (settings.get("colors") or {}).get(start_theme)
    theming.apply(app, start_theme, overrides if isinstance(overrides, dict) else None)
    window = MainWindow(settings)
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
