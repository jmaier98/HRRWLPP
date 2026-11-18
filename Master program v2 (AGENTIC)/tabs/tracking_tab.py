# tabs/tracking_tab.py
"""
Tracking tab — sample drift since the last origin reset.

Listens to the Alignment tab: every completed find-feature alignment (manual,
during a scan, or agent-driven) appends one point; setting a new origin clears
the history, because offsets before and after a reset are not comparable.

Three views:
  • X/Y offset scatter — the drift path in galvo space
  • X and Y offset vs time
  • X and Y fit error vs time
"""
from __future__ import annotations
import os, time
from typing import List

import numpy as np

from PyQt6.QtCore import Qt, pyqtSlot
from PyQt6.QtWidgets import (
    QWidget, QHBoxLayout, QVBoxLayout, QGridLayout,
    QPushButton, QLineEdit, QLabel, QMessageBox, QFileDialog, QCheckBox,
)
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg as FigureCanvas
from matplotlib.figure import Figure

from helpers.state_header import instrument_state_lines


class TrackingTab(QWidget):
    def __init__(self, controller, alignment_tab=None):
        super().__init__()
        self.controller = controller
        self.state = controller.state

        # Recorded history since the last origin reset
        self._t:     List[float] = []   # epoch seconds
        self._x:     List[float] = []   # x offset
        self._y:     List[float] = []
        self._xe:    List[float] = []   # x fit error (1 sigma)
        self._ye:    List[float] = []
        self._src:   List[str]   = []
        self._t0: float | None = None   # origin reset time (x axis zero)
        self._origin = (float("nan"), float("nan"))

        # ── UI ────────────────────────────────────────────────────────────────
        outer = QHBoxLayout(self)
        ctrl  = QVBoxLayout()

        self.status_lbl = QLabel("No alignments recorded yet.")
        self.status_lbl.setWordWrap(True)
        self.latest_lbl = QLabel("")
        self.latest_lbl.setWordWrap(True)

        save_grid = QGridLayout()
        self.folder   = QLineEdit(os.path.expanduser("~/tracking_logs"))
        self.filename = QLineEdit("drift")
        browse_btn    = QPushButton("Browse…")
        browse_btn.clicked.connect(self._browse)
        save_grid.addWidget(QLabel("Folder:"),   0, 0)
        save_grid.addWidget(self.folder,         0, 1)
        save_grid.addWidget(browse_btn,          0, 2)
        save_grid.addWidget(QLabel("Filename:"), 1, 0)
        save_grid.addWidget(self.filename,       1, 1)

        self.save_btn  = QPushButton("Save offset data")
        self.save_btn.clicked.connect(self._save)
        self.clear_btn = QPushButton("Clear history")
        self.clear_btn.clicked.connect(self._clear_clicked)
        self.errbar_chk = QCheckBox("Show error bars on offset vs time")
        self.errbar_chk.setChecked(True)
        self.errbar_chk.stateChanged.connect(lambda _=None: self._redraw())

        ctrl.addWidget(QLabel("<b>Drift since last origin reset</b>"))
        ctrl.addWidget(self.status_lbl)
        ctrl.addWidget(self.latest_lbl)
        ctrl.addSpacing(10)
        ctrl.addWidget(self.errbar_chk)
        ctrl.addSpacing(10)
        ctrl.addLayout(save_grid)
        ctrl.addWidget(self.save_btn)
        ctrl.addWidget(self.clear_btn)
        ctrl.addStretch(1)

        # Plots
        right = QVBoxLayout()
        self.fig_xy = Figure(figsize=(5, 4), tight_layout=True, facecolor="#f5f5f5")
        self.ax_xy  = self.fig_xy.add_subplot(111)
        self.canvas_xy = FigureCanvas(self.fig_xy)
        self._cbar = None

        self.fig_t = Figure(figsize=(6, 5), tight_layout=True, facecolor="#f5f5f5")
        self.ax_off = self.fig_t.add_subplot(211)
        self.ax_err = self.fig_t.add_subplot(212, sharex=self.ax_off)
        self.canvas_t = FigureCanvas(self.fig_t)

        right.addWidget(self.canvas_xy, stretch=1)
        right.addWidget(self.canvas_t,  stretch=1)

        outer.addLayout(ctrl,  stretch=1)
        outer.addLayout(right, stretch=3)

        self._redraw()

        # ── Alignment tab hookup ──────────────────────────────────────────────
        if alignment_tab is not None:
            self.attach_alignment_tab(alignment_tab)

    def attach_alignment_tab(self, alignment_tab):
        """Connect to an Alignment tab's tracking signals (queued: the blocking
        alignment paths emit from worker threads)."""
        alignment_tab.alignment_recorded.connect(
            self.record_alignment, Qt.ConnectionType.QueuedConnection)
        alignment_tab.origin_reset.connect(
            self.reset_origin, Qt.ConnectionType.QueuedConnection)

    # ── Data intake ───────────────────────────────────────────────────────────

    @pyqtSlot(dict)
    def record_alignment(self, rec: dict):
        t = float(rec.get("t", time.time()))
        if self._t0 is None:
            self._t0 = t
        self._t.append(t)
        self._x.append(float(rec.get("x_offset", float("nan"))))
        self._y.append(float(rec.get("y_offset", float("nan"))))
        self._xe.append(float(rec.get("x_err", float("nan"))))
        self._ye.append(float(rec.get("y_err", float("nan"))))
        self._src.append(str(rec.get("source", "")))
        self._redraw()

    @pyqtSlot(dict)
    def reset_origin(self, rec: dict):
        """A new origin was set — previous offsets are meaningless now."""
        self._clear()
        self._t0 = float(rec.get("t", time.time()))
        self._origin = (float(rec.get("x_center", float("nan"))),
                        float(rec.get("y_center", float("nan"))))
        self.controller.log(
            f"Tracking reset at origin X={self._origin[0]:.4f} Y={self._origin[1]:.4f}")
        self._redraw()

    def _clear(self):
        self._t.clear(); self._x.clear(); self._y.clear()
        self._xe.clear(); self._ye.clear(); self._src.clear()
        self._t0 = None

    def _clear_clicked(self):
        self._clear()
        self._origin = (float("nan"), float("nan"))
        self._redraw()

    # ── Plotting ──────────────────────────────────────────────────────────────

    def _minutes(self) -> np.ndarray:
        if not self._t:
            return np.array([])
        t0 = self._t0 if self._t0 is not None else self._t[0]
        return (np.asarray(self._t, float) - t0) / 60.0

    def _redraw(self):
        x  = np.asarray(self._x,  float)
        y  = np.asarray(self._y,  float)
        xe = np.asarray(self._xe, float)
        ye = np.asarray(self._ye, float)
        tm = self._minutes()
        n  = len(tm)

        # XY drift path
        ax = self.ax_xy
        # The colorbar owns its own axes; clearing ax alone would stack one
        # extra colorbar onto the figure per alignment.
        if self._cbar is not None:
            try:
                self._cbar.remove()
            except Exception:
                pass
            self._cbar = None
        ax.clear()
        ax.set_facecolor("#ffffff")
        ax.set_title("Offset path (X vs Y)")
        ax.set_xlabel("X offset")
        ax.set_ylabel("Y offset")
        ax.grid(True, alpha=0.3)
        if n:
            ax.plot(x, y, "-", color="#bbbbbb", lw=1, zorder=1)
            sc = ax.scatter(x, y, c=tm, cmap="viridis", s=36, zorder=2)
            ax.scatter([x[-1]], [y[-1]], facecolors="none", edgecolors="red",
                       s=140, lw=1.5, zorder=3, label="latest")
            ax.axhline(0, color="#888888", lw=0.8)
            ax.axvline(0, color="#888888", lw=0.8)
            ax.legend(loc="best", fontsize=8)
            try:
                self._cbar = self.fig_xy.colorbar(sc, ax=ax)
                self._cbar.set_label("Minutes since origin reset")
            except Exception:
                self._cbar = None
            # Square box with equal data units: a drift of 1 in x looks the
            # same length as a drift of 1 in y, so the path is not distorted.
            pad  = 0.1
            half = max(np.nanmax(np.abs(x)), np.nanmax(np.abs(y)), 1e-9) * (1 + pad)
            ax.set_xlim(-half, half)
            ax.set_ylim(-half, half)
        ax.set_aspect("equal", adjustable="box")
        self.fig_xy.canvas.draw_idle()

        # Offset vs time
        ax = self.ax_off
        ax.clear()
        ax.set_facecolor("#ffffff")
        ax.set_title("Offset vs time")
        ax.set_ylabel("Offset")
        ax.grid(True, alpha=0.3)
        if n:
            if self.errbar_chk.isChecked():
                ax.errorbar(tm, x, yerr=np.where(np.isfinite(xe), xe, 0.0),
                            fmt="o-", ms=4, capsize=3, color="#1f77b4", label="X offset")
                ax.errorbar(tm, y, yerr=np.where(np.isfinite(ye), ye, 0.0),
                            fmt="s-", ms=4, capsize=3, color="#d62728", label="Y offset")
            else:
                ax.plot(tm, x, "o-", ms=4, color="#1f77b4", label="X offset")
                ax.plot(tm, y, "s-", ms=4, color="#d62728", label="Y offset")
            ax.axhline(0, color="#888888", lw=0.8)
            ax.legend(loc="best", fontsize=8)
        self.ax_off.tick_params(labelbottom=False)

        # Fit error vs time
        ax = self.ax_err
        ax.clear()
        ax.set_facecolor("#ffffff")
        ax.set_title("Fit error vs time")
        ax.set_xlabel("Minutes since origin reset")
        ax.set_ylabel("Fit error (1σ)")
        ax.grid(True, alpha=0.3)
        if n:
            ax.plot(tm, xe, "o-", ms=4, color="#1f77b4", label="X fit error")
            ax.plot(tm, ye, "s-", ms=4, color="#d62728", label="Y fit error")
            ax.legend(loc="best", fontsize=8)
        self.fig_t.canvas.draw_idle()

        self._update_labels()

    def _update_labels(self):
        n = len(self._t)
        if not n:
            self.status_lbl.setText("No alignments recorded since the last origin reset.")
            self.latest_lbl.setText("")
            return
        tm = self._minutes()
        self.status_lbl.setText(
            f"{n} alignment{'s' if n > 1 else ''} over {tm[-1]:.1f} min "
            f"since origin reset."
        )
        self.latest_lbl.setText(
            f"Latest: Δx = {self._x[-1]:.4f}, Δy = {self._y[-1]:.4f}\n"
            f"Fit error: σx = {self._xe[-1]:.4g}, σy = {self._ye[-1]:.4g}\n"
            f"Total drift = {np.hypot(self._x[-1], self._y[-1]):.4f}"
        )

    # ── Saving ────────────────────────────────────────────────────────────────

    def _browse(self):
        path = QFileDialog.getExistingDirectory(self, "Choose folder", self.folder.text())
        if path:
            self.folder.setText(path)

    def _unique_name(self, folder: str, base: str) -> str:
        if not os.path.exists(os.path.join(folder, f"{base}.txt")):
            return base
        i = 1
        while os.path.exists(os.path.join(folder, f"{base}_{i}.txt")):
            i += 1
        return f"{base}_{i}"

    def save_data(self, filename: str | None = None) -> str:
        """Save the tracking history. Returns the .txt path or raises RuntimeError."""
        if not self._t:
            raise RuntimeError("No tracking data to save — run an alignment first.")

        folder = self.folder.text().strip()
        if filename:
            self.filename.setText(filename.strip())
        base = self.filename.text().strip() or "drift"
        try:
            os.makedirs(folder, exist_ok=True)
        except Exception as e:
            raise RuntimeError(f"Could not create folder: {e}")

        safe = self._unique_name(folder, base)
        if safe != base:
            self.filename.setText(safe)

        txt_path = os.path.join(folder, f"{safe}.txt")
        png_xy   = os.path.join(folder, f"{safe}_xy.png")
        png_time = os.path.join(folder, f"{safe}_vs_time.png")
        self.fig_xy.savefig(png_xy,   dpi=200, bbox_inches="tight")
        self.fig_t.savefig(png_time,  dpi=200, bbox_inches="tight")

        t0 = self._t0 if self._t0 is not None else self._t[0]
        rows = np.column_stack([
            np.asarray(self._t, float),
            (np.asarray(self._t, float) - t0) / 60.0,
            np.asarray(self._x, float), np.asarray(self._y, float),
            np.asarray(self._xe, float), np.asarray(self._ye, float),
        ])

        header = "\n".join([
            "Alignment drift tracking",
            f"Timestamp: {time.strftime('%Y-%m-%d %H:%M:%S')}",
            f"Origin reset at: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(t0))}",
            f"Origin feature position: X={self._origin[0]}  Y={self._origin[1]}",
            f"Alignment sources: {', '.join(sorted(set(self._src)))}",
            f"N points: {len(self._t)}",
            "",
            *instrument_state_lines(self.controller),
            "",
            "Columns: [epoch_s, minutes_since_reset, x_offset, y_offset, "
            "x_fit_err, y_fit_err]",
            "",
            "Table begins below this header.",
        ])
        np.savetxt(txt_path, rows, delimiter="\t", fmt="%.10g",
                   header=header, comments="")
        self.controller.log(f"Tracking data saved: {txt_path}")
        return txt_path

    def _save(self):
        try:
            txt_path = self.save_data()
        except RuntimeError as e:
            QMessageBox.warning(self, "Save", str(e))
            return
        except Exception as e:
            QMessageBox.critical(self, "Save error", str(e))
            return
        QMessageBox.information(self, "Saved", f"Saved:\n{txt_path}")

    # ── Agent API ─────────────────────────────────────────────────────────────

    def get_settings(self) -> dict:
        return {
            "folder":   self.folder.text(),
            "filename": self.filename.text(),
            "n_points": len(self._t),
            "latest_offset": (self._x[-1], self._y[-1]) if self._t else None,
            "latest_fit_error": (self._xe[-1], self._ye[-1]) if self._t else None,
            "minutes_since_reset": float(self._minutes()[-1]) if self._t else None,
        }

    def set_filename(self, name: str):
        self.filename.setText(name.strip())
