"""
Pump-Probe Microscope — Agentic Control
========================================
Main window layout:

  ┌──────────────────────────────────────────┐
  │           Tab Bar (QTabWidget)           │  ← all instrument tabs
  │                                          │
  ├──────────────────────────────────────────┤
  │              Agent Panel                 │  ← always visible
  │  [task input ................] [Send] [■] │
  │  ┌──────────────────┬───────────────────┐│
  │  │  Chat (summaries)│    Full log       ││
  │  └──────────────────┴───────────────────┘│
  └──────────────────────────────────────────┘
"""

import sys
import re
from collections import deque
from datetime import datetime
from pathlib import Path

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QSplitter, QTabWidget, QLabel, QLineEdit, QPushButton, QTextEdit,
    QMessageBox,
)
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QFont, QTextCursor

from microscope_state import MicroscopeState
from instrument_manager import InstrumentManager
from controller import MicroscopeController
from agent_worker import AgentWorker
from tabs.one_d_scan_tab import OneDScanTab
from tabs.dynamic_controls_tab import DynamicControlsTab
from tabs.two_d_scan_tab import TwoDScanTab
from tabs.three_d_scan_tab import ThreeDScanTab
from tabs.webcam_tab import WebcamTab
from tabs.find_focus_tab import FindFocusTab
from tabs.alignment_tab import AlignmentTab
from tabs.powermeter_tab import PowerMeterTab
from tabs.resistance_tab import ResistanceTab

CONTEXT_FILE = Path(__file__).parent / "context.txt"

# ── Colour palette ────────────────────────────────────────────────────────────
C_BG     = "#f5f5f5"
C_PANEL  = "#ffffff"
C_BORDER = "#cccccc"
C_TEXT   = "#1a1a1a"
C_DIM    = "#666666"
C_GREEN  = "#27ae60"
C_BLUE   = "#2980b9"
C_RED    = "#c0392b"


def _ts() -> str:
    return datetime.now().strftime("%H:%M:%S")


# ── Chat box ──────────────────────────────────────────────────────────────────

class ChatBox(QTextEdit):
    _BUBBLE = (
        '<div style="margin:4px 2px; padding:6px 8px; '
        'background:{bg}; border-left:3px solid {border}; border-radius:3px;">'
        '<span style="color:{hc}; font-size:10px; font-weight:bold;">{header}</span><br>'
        '<span style="color:{tc}; font-size:11px;">{body}</span>'
        '</div>'
    )

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setReadOnly(True)
        self.setFont(QFont("Segoe UI", 10))
        self.setStyleSheet(f"background:{C_PANEL}; border:1px solid {C_BORDER};")

    def add_turn(self, turn_num: int, text: str):
        self._append(self._BUBBLE.format(
            bg="#eaf7ee", border=C_GREEN, hc=C_GREEN,
            header=f"Turn {turn_num}  ·  {_ts()}",
            tc=C_TEXT, body=text.replace("\n", "<br>"),
        ))

    def add_conclusion(self, text: str):
        self._append(self._BUBBLE.format(
            bg="#e8f1f8", border=C_BLUE, hc=C_BLUE,
            header=f"Done  ·  {_ts()}",
            tc=C_TEXT, body=text.replace("\n", "<br>"),
        ))

    def add_error(self, text: str):
        self._append(self._BUBBLE.format(
            bg="#fbeaea", border=C_RED, hc=C_RED,
            header=f"Error  ·  {_ts()}",
            tc=C_TEXT, body=text.replace("\n", "<br>"),
        ))

    def _append(self, html: str):
        self.moveCursor(QTextCursor.MoveOperation.End)
        self.insertHtml(html)
        self.moveCursor(QTextCursor.MoveOperation.End)


# ── Context helpers ───────────────────────────────────────────────────────────

def _parse_context_sections(text: str) -> dict[str, str]:
    """Parse [Section Name] blocks from context.txt into a dict."""
    sections: dict[str, str] = {}
    current = None
    lines: list[str] = []
    for line in text.splitlines():
        m = re.match(r"^\[(.+?)\]\s*$", line)
        if m:
            if current is not None:
                sections[current] = "\n".join(lines).strip()
            current = m.group(1).strip()
            lines = []
        else:
            if current is not None:
                lines.append(line)
    if current is not None:
        sections[current] = "\n".join(lines).strip()
    return sections


def read_context_for_tab(tab_name: str) -> str:
    """
    Return the General section plus the named tab section from context.txt,
    formatted for injection into the agent system prompt.
    """
    if not CONTEXT_FILE.exists():
        return ""
    try:
        sections = _parse_context_sections(CONTEXT_FILE.read_text(encoding="utf-8"))
    except Exception:
        return ""

    parts: list[str] = []
    general = sections.get("General", "").strip()
    if general:
        parts.append(f"=== General norms ===\n{general}")
    tab_ctx = sections.get(tab_name, "").strip()
    if tab_ctx:
        parts.append(f"=== {tab_name} norms ===\n{tab_ctx}")
    return "\n\n".join(parts)


# ── Context editor tab ────────────────────────────────────────────────────────

class ContextTab(QWidget):
    """
    Editable view of context.txt, shown as one of the main tabs.
    Sections are [General] and [Tab Name] blocks.
    Changes are saved to disk on Save or when the main window closes.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        # Header row
        hdr = QHBoxLayout()
        lbl = QLabel(
            "Edit operator context and norms below. "
            "Use <b>[Section Name]</b> headers — e.g. <b>[General]</b>, <b>[1D Scan]</b>. "
            "Lines starting with # are comments."
        )
        lbl.setWordWrap(True)
        lbl.setStyleSheet(f"color:{C_DIM}; font-size:11px;")

        self.save_btn = QPushButton("Save to disk")
        self.save_btn.setFixedWidth(110)
        self.save_btn.setStyleSheet(
            f"background:{C_BLUE}; color:white; font-weight:bold; "
            "border-radius:3px; padding:4px 8px;"
        )
        self.save_btn.clicked.connect(self._save)

        hdr.addWidget(lbl, stretch=1)
        hdr.addWidget(self.save_btn)

        # Text editor
        self.editor = QTextEdit()
        self.editor.setFont(QFont("Consolas", 10))
        self.editor.setStyleSheet(
            f"background:{C_PANEL}; color:{C_TEXT}; "
            f"border:1px solid {C_BORDER}; padding:4px;"
        )

        self.status_lbl = QLabel("")
        self.status_lbl.setStyleSheet(f"color:{C_DIM}; font-size:10px;")

        layout.addLayout(hdr)
        layout.addWidget(self.editor, stretch=1)
        layout.addWidget(self.status_lbl)

        self._load()

    def _load(self):
        if CONTEXT_FILE.exists():
            try:
                self.editor.setPlainText(CONTEXT_FILE.read_text(encoding="utf-8"))
                self.status_lbl.setText(f"Loaded from {CONTEXT_FILE}")
            except Exception as e:
                self.status_lbl.setText(f"Load error: {e}")
        else:
            self.status_lbl.setText(f"{CONTEXT_FILE} not found — starting blank.")

    def _save(self):
        try:
            CONTEXT_FILE.write_text(self.editor.toPlainText(), encoding="utf-8")
            self.status_lbl.setText(f"Saved  ·  {datetime.now().strftime('%H:%M:%S')}")
        except Exception as e:
            QMessageBox.critical(self, "Save error", str(e))

    def save_if_modified(self):
        """Called by MainWindow.closeEvent to flush unsaved changes."""
        try:
            CONTEXT_FILE.write_text(self.editor.toPlainText(), encoding="utf-8")
        except Exception:
            pass


# ── Agent panel ───────────────────────────────────────────────────────────────

class AgentPanel(QWidget):
    """
    Persistent bottom panel. Always visible regardless of which tab is active.
    Contains: task input, Send button, Stop button, chat box, full log.
    """
    PANEL_MIN_HEIGHT = 200
    PANEL_MAX_HEIGHT = 400

    def __init__(self, controller: MicroscopeController, tabs: QTabWidget,
                 instrument_tabs: dict | None = None, parent=None):
        super().__init__(parent)
        self.controller = controller
        self._tabs = tabs
        self._instrument_tabs = instrument_tabs or {}
        self._worker: AgentWorker | None = None
        self._queue: deque[tuple[str, str]] = deque()  # (task, context) pairs

        self.setMinimumHeight(self.PANEL_MIN_HEIGHT)
        self.setMaximumHeight(self.PANEL_MAX_HEIGHT)
        self.setStyleSheet(f"background:{C_PANEL};")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 4, 6, 4)
        layout.setSpacing(4)

        # ── Top bar ──────────────────────────────────────────────────────────
        top = QHBoxLayout()
        top.setSpacing(6)

        lbl = QLabel("Agent:")
        lbl.setStyleSheet(f"color:{C_DIM}; font-size:11px;")
        lbl.setFixedWidth(44)

        self.task_input = QLineEdit()
        self.task_input.setPlaceholderText("Type a task and press Send…")
        self.task_input.setStyleSheet(
            f"background:{C_PANEL}; color:{C_TEXT}; border:1px solid {C_BORDER}; "
            "border-radius:3px; padding:3px 6px; font-size:12px;"
        )
        self.task_input.returnPressed.connect(self._send)

        self.send_btn = QPushButton("Send")
        self.send_btn.setFixedWidth(60)
        self.send_btn.setStyleSheet(
            f"background:{C_GREEN}; color:white; font-weight:bold; "
            "border-radius:3px; padding:4px;"
        )
        self.send_btn.clicked.connect(self._send)

        self.stop_btn = QPushButton("■ Stop")
        self.stop_btn.setFixedWidth(70)
        self.stop_btn.setEnabled(False)
        self.stop_btn.setStyleSheet(
            f"background:{C_RED}; color:white; font-weight:bold; "
            "border-radius:3px; padding:4px;"
        )
        self.stop_btn.clicked.connect(self._stop)

        self.clear_queue_btn = QPushButton("Clear queue")
        self.clear_queue_btn.setFixedWidth(90)
        self.clear_queue_btn.setEnabled(False)
        self.clear_queue_btn.setStyleSheet(
            f"border:1px solid {C_BORDER}; border-radius:3px; padding:4px;"
        )
        self.clear_queue_btn.clicked.connect(self._clear_queue)

        self.queue_lbl = QLabel("")
        self.queue_lbl.setStyleSheet(f"color:{C_DIM}; font-size:10px;")

        top.addWidget(lbl)
        top.addWidget(self.task_input, stretch=1)
        top.addWidget(self.send_btn)
        top.addWidget(self.stop_btn)
        top.addWidget(self.clear_queue_btn)
        top.addWidget(self.queue_lbl)

        # ── Log area ─────────────────────────────────────────────────────────
        splitter = QSplitter(Qt.Orientation.Horizontal)

        self.chat_box = ChatBox()
        self.chat_box.setMinimumWidth(200)

        self.full_log = QTextEdit()
        self.full_log.setReadOnly(True)
        self.full_log.setFont(QFont("Consolas", 9))
        self.full_log.setStyleSheet(
            f"background:{C_PANEL}; color:{C_TEXT}; border:1px solid {C_BORDER};"
        )
        self.full_log.setMinimumWidth(200)

        splitter.addWidget(self.chat_box)
        splitter.addWidget(self.full_log)
        splitter.setSizes([400, 400])

        layout.addLayout(top)
        layout.addWidget(splitter, stretch=1)

    # ── Divider line ──────────────────────────────────────────────────────────
    # Paint a thin line above the panel to visually separate it from the tabs
    def paintEvent(self, event):
        from PyQt6.QtGui import QPainter, QColor
        super().paintEvent(event)
        p = QPainter(self)
        p.setPen(QColor(C_BORDER))
        p.drawLine(0, 0, self.width(), 0)

    # ── Actions ───────────────────────────────────────────────────────────────

    def _send(self):
        task = self.task_input.text().strip()
        if not task:
            return

        active_tab = self._tabs.tabText(self._tabs.currentIndex())
        context    = read_context_for_tab(active_tab)
        self.task_input.clear()

        if self._worker and self._worker.isRunning():
            # Agent is busy — queue the task
            self._queue.append((task, context))
            self._update_queue_label()
            self._log_line(f"[{_ts()}] Queued: {task}  [tab: {active_tab}]")
        else:
            self._start_task(task, context, active_tab)

    def _start_task(self, task: str, context: str, tab_label: str = ""):
        self._log_line(f"[{_ts()}] Task: {task}"
                       + (f"  [tab: {tab_label}]" if tab_label else ""))
        self.stop_btn.setEnabled(True)

        self._worker = AgentWorker(self.controller, task, context=context,
                                   tabs=self._instrument_tabs)
        self._worker.agent_message.connect(self._on_agent_message)
        self._worker.tool_called.connect(self._on_tool_called)
        self._worker.turn_summary.connect(self._on_turn_summary)
        self._worker.finished.connect(self._on_finished)
        self._worker.error.connect(self._on_error)
        self._worker.start()

    def _stop(self):
        self._queue.clear()
        self._update_queue_label()
        if self._worker:
            self._worker.stop()
        self.controller.request_stop_all()
        self._log_line(f"[{_ts()}] Stop requested — queue cleared.")

    def _clear_queue(self):
        self._queue.clear()
        self._update_queue_label()
        self._log_line(f"[{_ts()}] Queue cleared.")

    def _update_queue_label(self):
        n = len(self._queue)
        if n == 0:
            self.queue_lbl.setText("")
            self.clear_queue_btn.setEnabled(False)
        else:
            self.queue_lbl.setText(f"{n} task{'s' if n != 1 else ''} queued")
            self.clear_queue_btn.setEnabled(True)

    def _maybe_start_next(self):
        """Pop and start the next queued task, if any."""
        if self._queue:
            task, context = self._queue.popleft()
            self._update_queue_label()
            self._log_line(f"[{_ts()}] Starting queued task: {task}")
            self._start_task(task, context)
        else:
            self.stop_btn.setEnabled(False)

    # ── Worker signal handlers ────────────────────────────────────────────────

    def _on_agent_message(self, text: str):
        self._log_line(f"[agent] {text}")

    def _on_tool_called(self, name: str, compact: str):
        self._log_line(f"[tool]  {name}: {compact}")

    def _on_turn_summary(self, turn_num: int, summary: str):
        self.chat_box.add_turn(turn_num, summary)

    def _on_finished(self, conclusion: str):
        self.chat_box.add_conclusion(conclusion)
        self._log_line(f"[{_ts()}] Finished.")
        self._worker = None
        self._maybe_start_next()

    def _on_error(self, msg: str):
        self.chat_box.add_error(msg)
        self._log_line(f"[error] {msg}")
        self._worker = None
        self._maybe_start_next()

    def _log_line(self, text: str):
        self.full_log.moveCursor(QTextCursor.MoveOperation.End)
        self.full_log.insertPlainText(text + "\n")
        self.full_log.moveCursor(QTextCursor.MoveOperation.End)


# ── Main window ───────────────────────────────────────────────────────────────

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Pump-Probe Microscope (Agentic)")
        self.resize(1400, 900)

        # Instruments & controller
        self.state = MicroscopeState()
        self.im    = InstrumentManager(self.state)
        try:
            self.im.open_all()
        except Exception as e:
            print(f"[WARNING] Could not open all instruments: {e}")
        self.controller = MicroscopeController(self.im, self.state)
        self._init_shutter_state()

        # Central layout: tab widget above, agent panel below
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ── Tabs ─────────────────────────────────────────────────────────────
        self.tabs = QTabWidget()
        self.tabs.setStyleSheet(f"""
            QTabWidget::pane {{ border:1px solid {C_BORDER}; background:{C_BG}; }}
            QTabBar::tab {{
                background:{C_PANEL}; color:{C_DIM};
                padding:6px 14px; border:1px solid {C_BORDER};
                border-bottom:none; border-radius:3px 3px 0 0;
            }}
            QTabBar::tab:selected {{ background:{C_BG}; color:{C_TEXT}; }}
            QTabBar::tab:hover    {{ color:{C_TEXT}; }}
        """)

        self._add_tabs()

        # Switch active tab when agent starts working on one
        self.controller.scan_signals.activate_tab.connect(
            self._switch_to_tab, Qt.ConnectionType.QueuedConnection
        )

        # ── Agent panel ───────────────────────────────────────────────────────
        instrument_tabs = {
            "dynamic_controls": self.dynamic_tab,
            "1d_scan":          self.one_d_tab,
            "2d_scan":          self.two_d_tab,
            "3d_scan":          self.three_d_tab,
            "alignment":        self.alignment_tab,
            "find_focus":       self.find_focus_tab,
        }
        self.agent_panel = AgentPanel(self.controller, self.tabs,
                                      instrument_tabs=instrument_tabs)

        root.addWidget(self.tabs, stretch=1)
        root.addWidget(self.agent_panel)

        self.setStyleSheet(f"QMainWindow {{ background:{C_BG}; }}")

    def _init_shutter_state(self):
        """Close both shutters on startup and record the closed state."""
        shutter = self.controller.get_instrument("Shutter")
        if shutter is not None:
            try:
                shutter.closePump()
                shutter.closeProbe()
            except Exception as e:
                print(f"[WARNING] Could not close shutters on startup: {e}")
        # Always write False regardless — physical state matches intent at startup
        self.state.settings["pump_shutter_open"]  = False
        self.state.settings["probe_shutter_open"] = False

    def _add_tabs(self):
        webcam = WebcamTab(self.controller)
        self.tabs.addTab(webcam, "Webcam")

        self.one_d_tab = OneDScanTab(self.controller)
        self.tabs.addTab(self.one_d_tab, "1D Scan")

        self.dynamic_tab = DynamicControlsTab(self.controller)
        self.tabs.addTab(self.dynamic_tab, "Dynamic Controls")

        self.alignment_tab = AlignmentTab(self.controller)
        alignment = self.alignment_tab
        self.tabs.addTab(alignment, "Alignment")

        self.two_d_tab = TwoDScanTab(self.controller, alignment_tab=alignment)
        self.tabs.addTab(self.two_d_tab, "2D Scan")

        self.three_d_tab = ThreeDScanTab(self.controller, alignment_tab=alignment)
        three_d_tab = self.three_d_tab
        self.tabs.addTab(three_d_tab, "3D Scan")

        self.find_focus_tab = FindFocusTab(self.controller)
        find_focus = self.find_focus_tab
        self.tabs.addTab(find_focus, "Find Focus")

        powermeter = PowerMeterTab(self.controller)
        self.tabs.addTab(powermeter, "Power Meter")

        resistance = ResistanceTab(self.controller)
        self.tabs.addTab(resistance, "Resistance")

        self.context_tab = ContextTab()
        self.tabs.addTab(self.context_tab, "Context / Norms")

    def _switch_to_tab(self, tab_name: str):
        for i in range(self.tabs.count()):
            if self.tabs.tabText(i) == tab_name:
                self.tabs.setCurrentIndex(i)
                return

    def closeEvent(self, event):
        if self.agent_panel._worker and self.agent_panel._worker.isRunning():
            self.agent_panel._worker.stop()
            self.agent_panel._worker.wait(3000)
        self.context_tab.save_if_modified()
        self.state.save()
        try:
            self.im.close_all()
        except Exception:
            pass
        event.accept()


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
