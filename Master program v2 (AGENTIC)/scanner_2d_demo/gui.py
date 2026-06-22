"""
Scanner GUI — PyQt6 view that drives the shared controller.

Left panel has two tabs:
  • Controls — manual stage/scan controls + agent launcher
  • Context  — editable instrument norms injected into agent system prompt

Right-panel layout
──────────────────
  ┌─────────────────────────────┐
  │       Matplotlib plot       │  ← resizable
  ├──────────────┬──────────────┤
  │  Agent Chat  │   Full Log   │  ← resizable (side-by-side)
  │  (per-turn   │  (every tool │
  │  summaries)  │   call/msg)  │
  └──────────────┴──────────────┘
"""

import sys
import os
from datetime import datetime
from pathlib import Path

import numpy as np
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QGridLayout, QLabel, QLineEdit, QPushButton, QGroupBox,
    QSplitter, QTextEdit, QScrollArea, QTabWidget,
)
from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QColor, QTextCursor, QFont

from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg as FigureCanvas
from matplotlib.figure import Figure

from controller import ScannerController

CONTEXT_FILE = Path(__file__).parent / "context.txt"

# ── Colour palette ────────────────────────────────────────────
C_BG        = "#2b2b2b"
C_PANEL     = "#1e1e1e"
C_BORDER    = "#555555"
C_TEXT      = "#dcdcdc"
C_DIM       = "#888888"
C_GREEN     = "#2ecc71"
C_BLUE      = "#3498db"
C_ORANGE    = "#e67e22"
C_RED       = "#e74c3c"
C_YELLOW    = "#f1c40f"


def _ts() -> str:
    """Short HH:MM:SS timestamp."""
    return datetime.now().strftime("%H:%M:%S")


class ChatBox(QTextEdit):
    """
    Read-only HTML chat log.
    Each entry is a rounded 'bubble' showing turn number, timestamp,
    and the agent's summary text.
    """

    _BUBBLE = (
        '<div style="margin:6px 2px; padding:8px 10px; '
        'background:{bg}; border-left:3px solid {border}; border-radius:4px;">'
        '<span style="color:{header_color}; font-size:10px; font-weight:bold;">'
        '{header}</span><br>'
        '<span style="color:{text_color}; font-size:12px;">{body}</span>'
        '</div>'
    )

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setReadOnly(True)
        self.setFont(QFont("Segoe UI", 10))
        self.document().setDefaultStyleSheet(f"body {{ background:{C_PANEL}; color:{C_TEXT}; }}")
        self.setStyleSheet(f"background:{C_PANEL}; border:1px solid {C_BORDER};")

    def add_turn(self, turn_num: int, text: str):
        html = self._BUBBLE.format(
            bg="#1a3a2a",
            border=C_GREEN,
            header_color=C_GREEN,
            header=f"Turn {turn_num}  ·  {_ts()}",
            text_color=C_TEXT,
            body=text.replace("\n", "<br>"),
        )
        self._append_html(html)

    def add_conclusion(self, text: str):
        html = self._BUBBLE.format(
            bg="#1a2a3a",
            border=C_BLUE,
            header_color=C_BLUE,
            header=f"✅  Done  ·  {_ts()}",
            text_color=C_TEXT,
            body=text.replace("\n", "<br>"),
        )
        self._append_html(html)

    def add_error(self, text: str):
        html = self._BUBBLE.format(
            bg="#3a1a1a",
            border=C_RED,
            header_color=C_RED,
            header=f"❌  Error  ·  {_ts()}",
            text_color=C_TEXT,
            body=text.replace("\n", "<br>"),
        )
        self._append_html(html)

    def add_stopped(self):
        html = self._BUBBLE.format(
            bg="#2a2a1a",
            border=C_YELLOW,
            header_color=C_YELLOW,
            header=f"■  Stopped  ·  {_ts()}",
            text_color=C_DIM,
            body="Agent was stopped by the user.",
        )
        self._append_html(html)

    def _append_html(self, html: str):
        cursor = self.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        cursor.insertHtml(html)
        self.verticalScrollBar().setValue(self.verticalScrollBar().maximum())


class FullLog(QTextEdit):
    """
    Read-only plain-text log of every event (tool calls, raw reasoning,
    rate-limit messages, …).
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setReadOnly(True)
        self.setFont(QFont("Consolas", 9))
        self.setStyleSheet(f"background:{C_PANEL}; color:{C_TEXT}; border:1px solid {C_BORDER};")

    def append_line(self, prefix: str, text: str, color: str = C_TEXT):
        cursor = self.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        self.setTextCursor(cursor)
        self.setTextColor(QColor(C_DIM))
        self.insertPlainText(f"[{_ts()}] ")
        self.setTextColor(QColor(color))
        self.insertPlainText(f"{prefix}  {text}\n")
        self.setTextColor(QColor(C_TEXT))
        self.verticalScrollBar().setValue(self.verticalScrollBar().maximum())

    def log_reasoning(self, text: str):
        self.append_line("💬", text, C_BLUE)

    def log_tool(self, name: str, result: str):
        self.append_line(f"🔧 {name}", result, C_ORANGE)

    def log_info(self, text: str):
        self.append_line("ℹ", text, C_DIM)

    def log_error(self, text: str):
        self.append_line("❌", text, C_RED)

    def log_done(self, text: str):
        self.append_line("✅", text, C_GREEN)


# ─────────────────────────────────────────────────────────────
#  Main window
# ─────────────────────────────────────────────────────────────

class ScannerGUI(QMainWindow):
    def __init__(self, controller: ScannerController):
        super().__init__()
        self.ctrl = controller
        self._agent_worker = None
        self._last_data_count = 0
        self._last_pp_trace_count = 0

        self.setWindowTitle("2D Scanner Control")
        self.setMinimumSize(1300, 800)

        central = QWidget()
        self.setCentralWidget(central)
        root = QHBoxLayout(central)
        root.setContentsMargins(4, 4, 4, 4)

        # ── Left column: tabbed (Controls | Context) ──────────
        left = QWidget()
        left.setFixedWidth(320)
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)

        self.left_tabs = QTabWidget()
        self.left_tabs.setDocumentMode(True)

        # ── Tab 1: Controls ───────────────────────────────────
        controls_tab = QWidget()
        controls_layout = QVBoxLayout(controls_tab)
        controls_layout.setContentsMargins(2, 2, 2, 2)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        inner = QWidget()
        il = QVBoxLayout(inner)
        il.addWidget(self._build_position_group())
        il.addWidget(self._build_single_measure_group())
        il.addWidget(self._build_line_scan_group())
        il.addWidget(self._build_2d_scan_group())
        il.addWidget(self._build_data_group())
        il.addStretch()
        scroll.setWidget(inner)
        controls_layout.addWidget(scroll, stretch=1)
        controls_layout.addWidget(self._build_agent_group())
        self.left_tabs.addTab(controls_tab, "Controls")

        # ── Tab 2: Context ────────────────────────────────────
        context_tab = QWidget()
        context_layout = QVBoxLayout(context_tab)
        context_layout.setContentsMargins(4, 4, 4, 4)

        context_layout.addWidget(QLabel(
            "Instrument norms injected into the agent system prompt.\n"
            "Loaded fresh each time you click ▶ Run Agent."
        ))

        self.context_editor = QTextEdit()
        self.context_editor.setFont(QFont("Consolas", 9))
        self.context_editor.setPlaceholderText("Enter norms, scan parameters, strategies…")
        context_layout.addWidget(self.context_editor, stretch=1)

        btn_row = QHBoxLayout()
        save_btn = QPushButton("💾  Save")
        save_btn.setToolTip(f"Save to {CONTEXT_FILE}")
        save_btn.clicked.connect(self._on_context_save)
        btn_row.addWidget(save_btn)

        reload_btn = QPushButton("↺  Reload from file")
        reload_btn.clicked.connect(self._on_context_reload)
        btn_row.addWidget(reload_btn)
        context_layout.addLayout(btn_row)

        self.context_status = QLabel("")
        self.context_status.setStyleSheet(f"color:{C_DIM}; font-style:italic; font-size:10px;")
        context_layout.addWidget(self.context_status)

        self.left_tabs.addTab(context_tab, "Context")

        # ── Tab 3: Pump-Probe ─────────────────────────────────
        pp_tab = QWidget()
        pp_layout = QVBoxLayout(pp_tab)
        pp_layout.setContentsMargins(4, 4, 4, 4)

        pp_scroll = QScrollArea()
        pp_scroll.setWidgetResizable(True)
        pp_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        pp_inner = QWidget()
        pp_inner_layout = QVBoxLayout(pp_inner)
        pp_inner_layout.addWidget(self._build_pp_power_group())
        pp_inner_layout.addWidget(self._build_pp_trace_group())
        pp_inner_layout.addWidget(self._build_pp_single_group())
        pp_inner_layout.addWidget(self._build_pp_data_group())
        pp_inner_layout.addStretch()
        pp_scroll.setWidget(pp_inner)
        pp_layout.addWidget(pp_scroll)

        self.left_tabs.addTab(pp_tab, "Pump-Probe")
        left_layout.addWidget(self.left_tabs)

        # Load context file on startup
        self._on_context_reload(silent=True)

        # ── Right column: tabbed plots on top, chat+log on bottom ─
        right_splitter = QSplitter(Qt.Orientation.Vertical)

        # Tabbed plot area: Scan Data | Pump-Probe
        self.plot_tabs = QTabWidget()
        self.plot_tabs.setDocumentMode(True)

        # Scan plot
        scan_plot_widget = QWidget()
        scan_plot_layout = QVBoxLayout(scan_plot_widget)
        scan_plot_layout.setContentsMargins(0, 0, 0, 0)
        self.figure = Figure(figsize=(7, 5), facecolor=C_BG)
        self.canvas = FigureCanvas(self.figure)
        self.canvas.setStyleSheet(f"background:{C_BG};")
        scan_plot_layout.addWidget(self.canvas)
        self.plot_tabs.addTab(scan_plot_widget, "Scan Data")

        # Pump-probe plot
        pp_plot_widget = QWidget()
        pp_plot_layout = QVBoxLayout(pp_plot_widget)
        pp_plot_layout.setContentsMargins(0, 0, 0, 0)
        self.pp_figure = Figure(figsize=(7, 5), facecolor=C_BG)
        self.pp_canvas = FigureCanvas(self.pp_figure)
        self.pp_canvas.setStyleSheet(f"background:{C_BG};")
        pp_plot_layout.addWidget(self.pp_canvas)
        self.plot_tabs.addTab(pp_plot_widget, "Pump-Probe")

        right_splitter.addWidget(self.plot_tabs)

        # Chat box + full log side by side
        bottom_splitter = QSplitter(Qt.Orientation.Horizontal)

        chat_group = QGroupBox("Agent Chat")
        chat_layout = QVBoxLayout(chat_group)
        chat_layout.setContentsMargins(4, 4, 4, 4)
        self.chat_box = ChatBox()
        chat_layout.addWidget(self.chat_box)
        bottom_splitter.addWidget(chat_group)

        log_group = QGroupBox("Full Log")
        log_layout = QVBoxLayout(log_group)
        log_layout.setContentsMargins(4, 4, 4, 4)
        self.full_log = FullLog()
        log_layout.addWidget(self.full_log)
        bottom_splitter.addWidget(log_group)

        bottom_splitter.setSizes([400, 500])
        right_splitter.addWidget(bottom_splitter)
        right_splitter.setSizes([480, 260])

        # Assemble root
        root_splitter = QSplitter(Qt.Orientation.Horizontal)
        root_splitter.addWidget(left)
        root_splitter.addWidget(right_splitter)
        root_splitter.setStretchFactor(0, 0)
        root_splitter.setStretchFactor(1, 1)
        root.addWidget(root_splitter)

        self.statusBar().showMessage("Ready")
        self._update_position_display()

        self._poll_timer = QTimer()
        self._poll_timer.timeout.connect(self._poll_state)
        self._poll_timer.start(300)

    # ── Manual control groups ─────────────────────────────────

    def _build_position_group(self) -> QGroupBox:
        group = QGroupBox("Stage Position")
        layout = QGridLayout(group)

        layout.addWidget(QLabel("X (mm):"), 0, 0)
        self.x_entry = QLineEdit("50.0")
        layout.addWidget(self.x_entry, 0, 1)

        layout.addWidget(QLabel("Y (mm):"), 1, 0)
        self.y_entry = QLineEdit("50.0")
        layout.addWidget(self.y_entry, 1, 1)

        self.pos_label = QLabel("Current: (50.000, 50.000)")
        self.pos_label.setStyleSheet("font-weight:bold;")
        layout.addWidget(self.pos_label, 2, 0, 1, 2)

        move_btn = QPushButton("Move To")
        move_btn.clicked.connect(self._on_move_to)
        layout.addWidget(move_btn, 3, 0, 1, 2)

        jog_layout = QHBoxLayout()
        for label, dx, dy in [("←", -5, 0), ("→", 5, 0), ("↑", 0, 5), ("↓", 0, -5)]:
            btn = QPushButton(label)
            btn.setFixedWidth(40)
            btn.clicked.connect(lambda _, dx=dx, dy=dy: self._on_jog(dx, dy))
            jog_layout.addWidget(btn)
        layout.addLayout(jog_layout, 4, 0, 1, 2)

        return group

    def _build_single_measure_group(self) -> QGroupBox:
        group = QGroupBox("Single Measurement")
        layout = QVBoxLayout(group)

        self.measure_label = QLabel("Value: ---")
        layout.addWidget(self.measure_label)
        btn = QPushButton("Measure")
        btn.clicked.connect(self._on_measure)
        layout.addWidget(btn)

        dwell_row = QHBoxLayout()
        dwell_row.addWidget(QLabel("Dwell (s):"))
        self.dwell_entry = QLineEdit("0.1")
        self.dwell_entry.setFixedWidth(60)
        dwell_row.addWidget(self.dwell_entry)
        dwell_btn = QPushButton("Set")
        dwell_btn.clicked.connect(self._on_set_dwell)
        dwell_row.addWidget(dwell_btn)
        layout.addLayout(dwell_row)

        return group

    def _build_line_scan_group(self) -> QGroupBox:
        group = QGroupBox("Line Scan")
        layout = QGridLayout(group)
        labels  = ["X Start:", "Y Start:", "X End:", "Y End:", "Points:"]
        defaults = ["0", "50", "100", "50", "50"]
        self.line_entries = {}
        for i, (lbl, val) in enumerate(zip(labels, defaults)):
            layout.addWidget(QLabel(lbl), i, 0)
            e = QLineEdit(val)
            layout.addWidget(e, i, 1)
            self.line_entries[lbl] = e
        btn = QPushButton("Run Line Scan")
        btn.clicked.connect(self._on_line_scan)
        layout.addWidget(btn, len(labels), 0, 1, 2)
        return group

    def _build_2d_scan_group(self) -> QGroupBox:
        group = QGroupBox("2D Raster Scan")
        layout = QGridLayout(group)
        labels   = ["X Start:", "Y Start:", "X End:", "Y End:", "X Points:", "Y Points:"]
        defaults = ["0", "0", "100", "100", "40", "40"]
        self.scan2d_entries = {}
        for i, (lbl, val) in enumerate(zip(labels, defaults)):
            layout.addWidget(QLabel(lbl), i, 0)
            e = QLineEdit(val)
            layout.addWidget(e, i, 1)
            self.scan2d_entries[lbl] = e
        btn = QPushButton("Run 2D Scan")
        btn.clicked.connect(self._on_2d_scan)
        layout.addWidget(btn, len(labels), 0, 1, 2)
        return group

    def _build_data_group(self) -> QGroupBox:
        group = QGroupBox("Data")
        layout = QHBoxLayout(group)
        clear_btn = QPushButton("Clear Data")
        clear_btn.clicked.connect(self._on_clear_data)
        layout.addWidget(clear_btn)
        plot_btn = QPushButton("Refresh Plot")
        plot_btn.clicked.connect(self._update_plot)
        layout.addWidget(plot_btn)
        return group

    # ── Pump-probe groups ─────────────────────────────────────

    def _build_pp_power_group(self) -> QGroupBox:
        group = QGroupBox("Beam Powers")
        layout = QGridLayout(group)

        layout.addWidget(QLabel("Pump (mW):"), 0, 0)
        self.pump_entry = QLineEdit("10.0")
        layout.addWidget(self.pump_entry, 0, 1)
        pump_btn = QPushButton("Set")
        pump_btn.clicked.connect(self._on_set_pump)
        layout.addWidget(pump_btn, 0, 2)

        layout.addWidget(QLabel("Probe (mW):"), 1, 0)
        self.probe_entry = QLineEdit("1.0")
        layout.addWidget(self.probe_entry, 1, 1)
        probe_btn = QPushButton("Set")
        probe_btn.clicked.connect(self._on_set_probe)
        layout.addWidget(probe_btn, 1, 2)

        self.pp_power_label = QLabel("Pump: 10.0 mW  |  Probe: 1.0 mW")
        self.pp_power_label.setStyleSheet("font-weight:bold;")
        layout.addWidget(self.pp_power_label, 2, 0, 1, 3)
        return group

    def _build_pp_trace_group(self) -> QGroupBox:
        group = QGroupBox("Time Trace")
        layout = QGridLayout(group)

        fields = [("Start (ps):", "-2.0"), ("End (ps):", "200.0"), ("Points:", "100")]
        self.pp_trace_entries = {}
        for i, (lbl, val) in enumerate(fields):
            layout.addWidget(QLabel(lbl), i, 0)
            e = QLineEdit(val)
            layout.addWidget(e, i, 1)
            self.pp_trace_entries[lbl] = e

        btn = QPushButton("Capture Trace")
        btn.clicked.connect(self._on_capture_trace)
        layout.addWidget(btn, len(fields), 0, 1, 2)
        return group

    def _build_pp_single_group(self) -> QGroupBox:
        group = QGroupBox("Single Delay")
        layout = QHBoxLayout(group)
        layout.addWidget(QLabel("Delay (ps):"))
        self.pp_delay_entry = QLineEdit("0.0")
        self.pp_delay_entry.setFixedWidth(70)
        layout.addWidget(self.pp_delay_entry)
        btn = QPushButton("Measure")
        btn.clicked.connect(self._on_pp_single)
        layout.addWidget(btn)
        self.pp_single_label = QLabel("---")
        layout.addWidget(self.pp_single_label)
        return group

    def _build_pp_data_group(self) -> QGroupBox:
        group = QGroupBox("PP Data")
        layout = QHBoxLayout(group)
        clear_btn = QPushButton("Clear Traces")
        clear_btn.clicked.connect(self._on_clear_pp)
        layout.addWidget(clear_btn)
        plot_btn = QPushButton("Refresh Plot")
        plot_btn.clicked.connect(self._update_pp_plot)
        layout.addWidget(plot_btn)
        return group

    def _build_agent_group(self) -> QGroupBox:
        group = QGroupBox("Agent Control")
        layout = QVBoxLayout(group)

        layout.addWidget(QLabel("Task:"))
        self.agent_task_entry = QTextEdit()
        self.agent_task_entry.setPlainText(
            "Find the strongest signal peak on the sample. "
            "Do a coarse survey first, then zoom in to precisely locate the peak. "
            "Report the peak position and intensity."
        )
        self.agent_task_entry.setMaximumHeight(80)
        layout.addWidget(self.agent_task_entry)

        btn_row = QHBoxLayout()
        self.agent_run_btn = QPushButton("▶  Run Agent")
        self.agent_run_btn.setStyleSheet(
            f"background:{C_GREEN}; color:white; font-weight:bold; padding:5px;"
        )
        self.agent_run_btn.clicked.connect(self._on_run_agent)
        btn_row.addWidget(self.agent_run_btn)

        self.agent_stop_btn = QPushButton("■  Stop")
        self.agent_stop_btn.setStyleSheet(
            f"background:{C_RED}; color:white; font-weight:bold; padding:5px;"
        )
        self.agent_stop_btn.setEnabled(False)
        self.agent_stop_btn.clicked.connect(self._on_stop_agent)
        btn_row.addWidget(self.agent_stop_btn)
        layout.addLayout(btn_row)

        self.agent_status_label = QLabel("Status: idle")
        self.agent_status_label.setStyleSheet(f"color:{C_DIM}; font-style:italic;")
        layout.addWidget(self.agent_status_label)

        return group

    # ── Manual handlers ───────────────────────────────────────

    def _on_move_to(self):
        try:
            x, y = float(self.x_entry.text()), float(self.y_entry.text())
            r = self.ctrl.move_to(x, y)
            self._update_position_display()
            self.statusBar().showMessage(f"Moved to ({r['x']:.3f}, {r['y']:.3f})")
            self.full_log.log_info(f"Manual move → ({r['x']:.3f}, {r['y']:.3f})")
        except Exception as e:
            self.statusBar().showMessage(f"Error: {e}")

    def _on_jog(self, dx, dy):
        r = self.ctrl.move_relative(dx, dy)
        self.x_entry.setText(f"{r['x']:.1f}")
        self.y_entry.setText(f"{r['y']:.1f}")
        self._update_position_display()

    def _on_measure(self):
        r = self.ctrl.measure()
        self.measure_label.setText(f"Value: {r['value']:.4f}")
        self._update_position_display()
        self._update_plot()
        self.full_log.log_info(f"Manual measure → {r['value']:.4f}")

    def _on_set_dwell(self):
        try:
            r = self.ctrl.set_dwell_time(float(self.dwell_entry.text()))
            self.statusBar().showMessage(f"Dwell time: {r['dwell_time']:.3f}s")
        except Exception as e:
            self.statusBar().showMessage(f"Error: {e}")

    def _on_line_scan(self):
        try:
            e = self.line_entries
            r = self.ctrl.run_line_scan(
                float(e["X Start:"].text()), float(e["Y Start:"].text()),
                float(e["X End:"].text()),   float(e["Y End:"].text()),
                int(e["Points:"].text()),
            )
            self.statusBar().showMessage(f"Line scan: {r['num_points']} points")
            self.full_log.log_info(f"Manual line scan: {r['num_points']} points")
            self._update_plot()
        except Exception as e:
            self.statusBar().showMessage(f"Error: {e}")

    def _on_2d_scan(self):
        try:
            e = self.scan2d_entries
            r = self.ctrl.run_2d_scan(
                float(e["X Start:"].text()), float(e["Y Start:"].text()),
                float(e["X End:"].text()),   float(e["Y End:"].text()),
                int(e["X Points:"].text()),  int(e["Y Points:"].text()),
            )
            self.statusBar().showMessage(f"2D scan: {r['num_points']} points")
            self.full_log.log_info(f"Manual 2D scan: {r['num_points']} points")
            self._update_plot()
        except Exception as e:
            self.statusBar().showMessage(f"Error: {e}")

    def _on_clear_data(self):
        self.ctrl.clear_data()
        self._last_data_count = 0
        self.figure.clear()
        self.canvas.draw()
        self.statusBar().showMessage("Data cleared")
        self.full_log.log_info("Data cleared")

    # ── Pump-probe handlers ───────────────────────────────────

    def _on_set_pump(self):
        try:
            r = self.ctrl.set_pump_power(float(self.pump_entry.text()))
            self._refresh_pp_power_label()
            self.statusBar().showMessage(f"Pump power: {r['pump_power_mw']:.2f} mW")
        except Exception as e:
            self.statusBar().showMessage(f"Error: {e}")

    def _on_set_probe(self):
        try:
            r = self.ctrl.set_probe_power(float(self.probe_entry.text()))
            self._refresh_pp_power_label()
            self.statusBar().showMessage(f"Probe power: {r['probe_power_mw']:.3f} mW")
        except Exception as e:
            self.statusBar().showMessage(f"Error: {e}")

    def _on_capture_trace(self):
        try:
            e = self.pp_trace_entries
            result = self.ctrl.capture_time_trace(
                float(e["Start (ps):"].text()),
                float(e["End (ps):"].text()),
                int(e["Points:"].text()),
            )
            self.statusBar().showMessage(
                f"Trace captured at ({result['x']:.1f}, {result['y']:.1f}) mm — "
                f"{result['num_points']} pts"
            )
            self.full_log.log_info(
                f"PP trace at ({result['x']:.1f},{result['y']:.1f}) mm, "
                f"{result['num_points']} pts"
            )
            self._update_pp_plot()
            self.plot_tabs.setCurrentIndex(1)   # switch to PP plot tab
        except Exception as e:
            self.statusBar().showMessage(f"Error: {e}")

    def _on_pp_single(self):
        try:
            delay = float(self.pp_delay_entry.text())
            r = self.ctrl.measure_at_delay(delay)
            self.pp_single_label.setText(f"{r['signal_mOD']:.4f} mOD")
            self.statusBar().showMessage(
                f"PP single: {delay:.2f} ps → {r['signal_mOD']:.4f} mOD"
            )
        except Exception as e:
            self.statusBar().showMessage(f"Error: {e}")

    def _on_clear_pp(self):
        self.ctrl.clear_pp_data()
        self._last_pp_trace_count = 0
        self.pp_figure.clear()
        self.pp_canvas.draw()
        self.statusBar().showMessage("PP data cleared")
        self.full_log.log_info("PP data cleared")

    def _refresh_pp_power_label(self):
        state = self.ctrl.get_state()["pump_probe"]
        self.pp_power_label.setText(
            f"Pump: {state['pump_power_mw']:.2f} mW  |  "
            f"Probe: {state['probe_power_mw']:.3f} mW"
        )

    # ── Context tab handlers ──────────────────────────────────

    def _on_context_save(self):
        try:
            CONTEXT_FILE.write_text(self.context_editor.toPlainText(), encoding="utf-8")
            self.context_status.setText(f"Saved  ·  {_ts()}")
            self.context_status.setStyleSheet(f"color:{C_GREEN}; font-size:10px;")
        except Exception as e:
            self.context_status.setText(f"Save failed: {e}")
            self.context_status.setStyleSheet(f"color:{C_RED}; font-size:10px;")

    def _on_context_reload(self, silent: bool = False):
        try:
            text = CONTEXT_FILE.read_text(encoding="utf-8") if CONTEXT_FILE.exists() else ""
            self.context_editor.setPlainText(text)
            if not silent:
                self.context_status.setText(f"Reloaded  ·  {_ts()}")
                self.context_status.setStyleSheet(f"color:{C_GREEN}; font-size:10px;")
        except Exception as e:
            self.context_status.setText(f"Load failed: {e}")
            self.context_status.setStyleSheet(f"color:{C_RED}; font-size:10px;")

    # ── Agent handlers ────────────────────────────────────────

    def _on_run_agent(self):
        from agent_worker_local import LocalAgentWorker

        task = self.agent_task_entry.toPlainText().strip()
        if not task:
            self.statusBar().showMessage("Enter a task for the agent first.")
            return

        # Grab current context text (auto-saves to file too)
        context = self.context_editor.toPlainText().strip()
        self._agent_worker = LocalAgentWorker(self.ctrl, task, context=context)
        self._agent_worker.agent_message.connect(self._on_agent_raw_message)
        self._agent_worker.tool_called.connect(self._on_agent_tool)
        self._agent_worker.turn_summary.connect(self._on_agent_turn_summary)
        self._agent_worker.finished.connect(self._on_agent_finished)
        self._agent_worker.error.connect(self._on_agent_error)
        self._agent_worker.start()

        self.agent_run_btn.setEnabled(False)
        self.agent_stop_btn.setEnabled(True)
        self._set_agent_status("running…", C_GREEN)
        self.full_log.log_info(f"Agent started — task: {task[:60]}…" if len(task) > 60 else f"Agent started — task: {task}")

    def _on_stop_agent(self):
        if self._agent_worker:
            self._agent_worker.stop()
        self.agent_stop_btn.setEnabled(False)
        self._set_agent_status("stopping…", C_YELLOW)
        self.chat_box.add_stopped()
        self.full_log.log_info("Stop requested by user")

    def _on_agent_raw_message(self, text: str):
        """Claude's raw reasoning — full log only."""
        self.full_log.log_reasoning(text)

    def _on_agent_tool(self, tool_name: str, result: str):
        """Each tool call — full log only."""
        self.full_log.log_tool(tool_name, result)

    def _on_agent_turn_summary(self, turn_num: int, summary: str):
        """Per-turn summary — chat box."""
        self.chat_box.add_turn(turn_num, summary)
        self.statusBar().showMessage(f"Turn {turn_num}: {summary[:80]}")

    def _on_agent_finished(self, conclusion: str):
        self.chat_box.add_conclusion(conclusion)
        self.full_log.log_done(f"Agent finished: {conclusion}")
        self._set_agent_status("done", C_GREEN)
        self.agent_run_btn.setEnabled(True)
        self.agent_stop_btn.setEnabled(False)
        self.statusBar().showMessage("Agent finished.")
        self._update_plot()

    def _on_agent_error(self, msg: str):
        self.chat_box.add_error(msg)
        self.full_log.log_error(msg)
        self._set_agent_status("error", C_RED)
        self.agent_run_btn.setEnabled(True)
        self.agent_stop_btn.setEnabled(False)

    def _set_agent_status(self, text: str, color: str):
        self.agent_status_label.setText(f"Status: {text}")
        self.agent_status_label.setStyleSheet(f"color:{color}; font-weight:bold;")

    # ── Polling & plot ────────────────────────────────────────

    def _poll_state(self):
        self._update_position_display()
        state = self.ctrl.get_state()
        n = state["num_points_collected"]
        if n != self._last_data_count:
            self._last_data_count = n
            self._update_plot()
        pp_n = state["pump_probe"]["num_traces"]
        if pp_n != self._last_pp_trace_count:
            self._last_pp_trace_count = pp_n
            self._update_pp_plot()

    def _update_position_display(self):
        pos = self.ctrl.get_state()["position"]
        self.pos_label.setText(f"Current: ({pos['x']:.3f}, {pos['y']:.3f})")

    def _update_plot(self):
        data = self.ctrl.get_data()
        if data["num_points"] == 0:
            return

        self.figure.clear()
        pts  = data["data"]
        xs   = [p["x"]     for p in pts]
        ys   = [p["y"]     for p in pts]
        vals = [p["value"] for p in pts]

        ax = self.figure.add_subplot(111)
        ax.set_facecolor(C_PANEL)
        self.figure.patch.set_facecolor(C_BG)
        for spine in ax.spines.values():
            spine.set_edgecolor(C_BORDER)
        ax.tick_params(colors=C_DIM)
        ax.xaxis.label.set_color(C_DIM)
        ax.yaxis.label.set_color(C_DIM)
        ax.title.set_color(C_TEXT)

        ux, uy = sorted(set(xs)), sorted(set(ys))
        if len(ux) > 1 and len(uy) > 1 and len(pts) == len(ux) * len(uy):
            grid = np.zeros((len(uy), len(ux)))
            for p in pts:
                grid[uy.index(p["y"]), ux.index(p["x"])] = p["value"]
            im = ax.imshow(
                grid, extent=[min(xs), max(xs), min(ys), max(ys)],
                origin="lower", aspect="auto", cmap="viridis",
            )
            cb = self.figure.colorbar(im, ax=ax, label="Signal")
            cb.ax.yaxis.set_tick_params(color=C_DIM)
            cb.ax.yaxis.label.set_color(C_DIM)
            ax.set_title(f"2D Scan  {len(ux)}×{len(uy)}")
        else:
            sc = ax.scatter(xs, ys, c=vals, cmap="viridis", s=15)
            cb = self.figure.colorbar(sc, ax=ax, label="Signal")
            cb.ax.yaxis.set_tick_params(color=C_DIM)
            cb.ax.yaxis.label.set_color(C_DIM)
            ax.set_title(f"Scan  ({len(pts)} pts)")

        ax.set_xlabel("X (mm)")
        ax.set_ylabel("Y (mm)")
        self.figure.tight_layout()
        self.canvas.draw()

    def _update_pp_plot(self):
        """Redraw the pump-probe plot with all captured traces."""
        traces = self.ctrl.get_pp_traces().get("traces", [])
        self.pp_figure.clear()
        if not traces:
            self.pp_canvas.draw()
            return

        ax = self.pp_figure.add_subplot(111)
        ax.set_facecolor(C_PANEL)
        self.pp_figure.patch.set_facecolor(C_BG)
        for spine in ax.spines.values():
            spine.set_edgecolor(C_BORDER)
        ax.tick_params(colors=C_DIM)
        ax.xaxis.label.set_color(C_DIM)
        ax.yaxis.label.set_color(C_DIM)
        ax.title.set_color(C_TEXT)

        colours = [C_GREEN, C_BLUE, C_ORANGE, C_RED,
                   "#9b59b6", "#1abc9c", C_YELLOW, "#e91e63"]
        for i, tr in enumerate(traces):
            colour = colours[i % len(colours)]
            label  = f"({tr['x']:.1f}, {tr['y']:.1f}) mm"
            ax.plot(tr["delays"], tr["signals"],
                    color=colour, linewidth=1.5, label=label)

        ax.axhline(0, color=C_BORDER, linewidth=0.8, linestyle="--")
        ax.axvline(0, color=C_BORDER, linewidth=0.8, linestyle=":")
        ax.set_xlabel("Delay (ps)")
        ax.set_ylabel("Signal (mOD)")
        ax.set_title(f"Pump-Probe Traces ({len(traces)})")
        if len(traces) <= 8:
            ax.legend(fontsize=8, facecolor=C_PANEL, labelcolor=C_TEXT,
                      edgecolor=C_BORDER, loc="upper right")
        self.pp_figure.tight_layout()
        self.pp_canvas.draw()


# ─────────────────────────────────────────────────────────────

def run_gui(controller: ScannerController | None = None):
    app = QApplication(sys.argv)
    app.setStyleSheet(f"""
        QMainWindow, QWidget      {{ background:{C_BG};    color:{C_TEXT}; }}
        QGroupBox                 {{ border:1px solid {C_BORDER}; border-radius:4px;
                                     margin-top:6px; padding-top:6px; }}
        QGroupBox::title          {{ subcontrol-origin:margin; left:8px; color:#aaa; }}
        QLineEdit, QTextEdit      {{ background:{C_PANEL}; color:{C_TEXT};
                                     border:1px solid {C_BORDER}; border-radius:3px; padding:2px; }}
        QPushButton               {{ background:#3c3f41; color:{C_TEXT};
                                     border:1px solid {C_BORDER}; border-radius:3px;
                                     padding:4px 8px; }}
        QPushButton:hover         {{ background:#4c5052; }}
        QPushButton:disabled      {{ color:#555; }}
        QScrollArea               {{ border:none; }}
        QSplitter::handle         {{ background:{C_BORDER}; }}
    """)
    ctrl = controller or ScannerController()
    window = ScannerGUI(ctrl)
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    run_gui()
