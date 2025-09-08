# four_map_scan_tab.py
from __future__ import annotations
import os, time, numpy as np
from PyQt6.QtCore import QThread, pyqtSignal, Qt
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QFormLayout, QPushButton, QLineEdit,
    QMessageBox, QFileDialog, QLabel, QGridLayout, QComboBox
)
from PyQt6.QtGui import QDoubleValidator, QIntValidator
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg as FigureCanvas
from matplotlib.figure import Figure
from mpl_toolkits.axes_grid1.inset_locator import inset_axes


# ============================
# Worker: XY raster, 4 channels
# ============================
class _ScanWorker4Map(QThread):
    """
    Runs an XY raster scan and reads 4 data channels per point:
      (L1X, L1Y) from _lockin.read_xy()
      (L2X, L2Y) from _lockin.readxy2()
    Emits indices + the four values.
    """
    progress = pyqtSignal(int, int, float, float, float, float)  # ix, iy, L1X, L1Y, L2X, L2Y
    finished = pyqtSignal(bool, str)  # user_abort, message

    def __init__(self, galvo, lockin, xs, ys, wait_s: float, serpentine: bool = False):
        super().__init__()
        self._galvo      = galvo
        self._lockin     = lockin
        self._xs         = xs
        self._ys         = ys
        self._wait_s     = wait_s
        self._serpentine = serpentine
        self._stop       = False

    def stop(self):
        self._stop = True

    def _move_galvo(self, x: float, y: float):
        # Support either .movexy(x, y) or .move(x, y)
        if hasattr(self._galvo, "movexy"):
            self._galvo.movexy(float(x), float(y))
        elif hasattr(self._galvo, "move"):
            self._galvo.move(float(x), float(y))
        else:
            raise AttributeError("Galvo object exposes neither .movexy nor .move")

    def run(self):
        try:
            for iy, y in enumerate(self._ys):
                if self._stop:
                    self.finished.emit(True, "Scan stopped by user.")
                    return

                if self._serpentine and (iy % 2 == 1):
                    x_iter = enumerate(self._xs[::-1])
                    reverse = True
                else:
                    x_iter = enumerate(self._xs)
                    reverse = False

                for ix_local, x in x_iter:
                    if self._stop:
                        self.finished.emit(True, "Scan stopped by user.")
                        return

                    ix = (len(self._xs) - 1 - ix_local) if reverse else ix_local

                    # Move, wait
                    try:
                        self._move_galvo(x, y)
                    except Exception as e:
                        self.finished.emit(True, f"Galvo error at (x={x}, y={y}): {e}")
                        return

                    if self._wait_s > 0:
                        time.sleep(self._wait_s)

                    # Read two lock-ins: (L1X, L1Y), (L2X, L2Y)
                    try:
                        l1x, l1y = self._lockin.read_xy()
                    except Exception as e:
                        self.finished.emit(True, f"read_xy() error at (x={x}, y={y}): {e}")
                        return

                    try:
                        l2x, l2y = self._lockin.read_xy2()
                    except Exception as e:
                        self.finished.emit(True, f"read_xy2() error at (x={x}, y={y}): {e}")
                        return

                    self.progress.emit(ix, iy, float(l1x), float(l1y), float(l2x), float(l2y))

            self.finished.emit(False, "Scan complete.")
        except Exception as e:
            self.finished.emit(True, f"Unexpected error: {e}")


# ============================
# UI Tab: 2×2 maps + controls
# ============================
class FourMapScanTab(QWidget):
    """
    2D scan tab with four spatial maps (right) and controls (left).

    InstrumentManager keys:
      • "Galvo" → exposes .movexy(x, y) or .move(x, y)
      • "SR830" (or your lock-in wrapper) → exposes:
            .read_xy()  -> (L1X, L1Y)
            .readxy2()  -> (L2X, L2Y)
    """
    def __init__(self, instrument_manager, state=None):
        super().__init__()
        self.IM    = instrument_manager
        self.state = state

        # --- Instruments ---
        try:
            self.galvo  = self.IM.get("Galvo")
            self.lockin = self.IM.get("SR830")
        except KeyError as err:
            raise RuntimeError(
                f"InstrumentManager is missing “{err.args[0]}”. "
                "Add it before constructing FourMapScanTab."
            )

        # --- Layout scaffold ---
        outer = QHBoxLayout(self)

        # Left: controls
        controls_col = QVBoxLayout()
        form = QFormLayout()

        dv = QDoubleValidator(-1e9, 1e9, 6, self)
        iv = QIntValidator(2, 5000, self)

        self.x_start  = QLineEdit("-0.01"); self.x_start.setValidator(dv)
        self.x_end    = QLineEdit("0.01");  self.x_end.setValidator(dv)
        self.x_steps  = QLineEdit("30");    self.x_steps.setValidator(iv)

        self.y_start  = QLineEdit("-0.01"); self.y_start.setValidator(dv)
        self.y_end    = QLineEdit("0.01");  self.y_end.setValidator(dv)
        self.y_steps  = QLineEdit("30");    self.y_steps.setValidator(iv)

        self.wait_ms  = QLineEdit("10");    self.wait_ms.setValidator(QIntValidator(0, 100000, self))

        self.folder   = QLineEdit(os.path.expanduser("~/photocurrent_scans"))
        self.filename = QLineEdit("scan")

        form.addRow("X start:", self.x_start)
        form.addRow("X end:",   self.x_end)
        form.addRow("X steps:", self.x_steps)
        form.addRow("Y start:", self.y_start)
        form.addRow("Y end:",   self.y_end)
        form.addRow("Y steps:", self.y_steps)
        form.addRow("Wait (ms):", self.wait_ms)

        controls_col.addLayout(form)
        controls_col.addWidget(QLabel("Folder:"))
        controls_col.addWidget(self.folder)
        controls_col.addWidget(QLabel("Filename (no ext):"))
        controls_col.addWidget(self.filename)

        btns = QGridLayout()
        self.start_btn  = QPushButton("Start");   self.start_btn.clicked.connect(self._start)
        self.stop_btn   = QPushButton("Stop");    self.stop_btn.clicked.connect(self._stop); self.stop_btn.setEnabled(False)
        self.save_btn   = QPushButton("Save");    self.save_btn.clicked.connect(self._save)
        self.browse_btn = QPushButton("Browse…"); self.browse_btn.clicked.connect(self._browse)

        btns.addWidget(self.start_btn,  0, 0)
        btns.addWidget(self.stop_btn,   0, 1)
        btns.addWidget(self.save_btn,   1, 0)
        btns.addWidget(self.browse_btn, 1, 1)
        controls_col.addLayout(btns)

        self.status_lbl = QLabel("Idle.")
        controls_col.addWidget(self.status_lbl)
        controls_col.addStretch(1)

        # Right: 2×2 maps + per-map dropdowns
        right_col = QVBoxLayout()
        self.fig = Figure(figsize=(8, 6), tight_layout=True)
        self.canvas = FigureCanvas(self.fig)

        # create 2x2 subplots
        self.axes = self.fig.subplots(2, 2)
        self.axes = [ax for row in self.axes for ax in row]  # flatten to [0..3]
        for i, ax in enumerate(self.axes):
            ax.set_box_aspect(1)
            ax.set_xlabel("X")
            ax.set_ylabel("Y")
            ax.grid(True, alpha=0.2)

        # per-plot images and colorbars
        self.images = [None, None, None, None]
        self.cax    = [None, None, None, None]
        self.cbars  = [None, None, None, None]

        right_col.addWidget(self.canvas)

        # dropdowns for each map
        self.chan_labels = ["L1 X", "L1 Y", "L2 X", "L2 Y"]
        self.map_selects: list[QComboBox] = []
        sel_grid = QGridLayout()
        for i in range(4):
            lab = QLabel(f"Map {i+1}:")
            box = QComboBox()
            box.addItems(self.chan_labels)
            box.setCurrentIndex(i)  # Map1->L1X, Map2->L1Y, Map3->L2X, Map4->L2Y
            box.currentIndexChanged.connect(lambda _=None, idx=i: self._on_map_select_changed(idx))
            self.map_selects.append(box)
            sel_grid.addWidget(lab, i // 2, (i % 2) * 2 + 0)
            sel_grid.addWidget(box, i // 2, (i % 2) * 2 + 1)
        right_col.addLayout(sel_grid)

        # Assemble
        outer.addLayout(controls_col, stretch=1)
        outer.addLayout(right_col,    stretch=5)

        # runtime / data holders
        self.worker: _ScanWorker4Map | None = None
        self._xs = None
        self._ys = None

        # channel grids: 4 arrays shaped (ny, nx)
        self._ch_data = [None, None, None, None]  # L1X, L1Y, L2X, L2Y

        # flattened 6-col table per sample row: [x, y, L1X, L1Y, L2X, L2Y]
        self._table6 = None

        # initial titles
        for i, ax in enumerate(self.axes):
            ax.set_title(f"Map {i+1} — {self.chan_labels[i]}")

        self.canvas.draw_idle()

    # -------------
    # UI handlers
    # -------------
    def _browse(self):
        path = QFileDialog.getExistingDirectory(self, "Choose folder", self.folder.text())
        if path:
            self.folder.setText(path)

    def _start(self):
        # parse inputs
        try:
            x0 = float(self.x_start.text())
            x1 = float(self.x_end.text())
            nx = int(self.x_steps.text())

            y0 = float(self.y_start.text())
            y1 = float(self.y_end.text())
            ny = int(self.y_steps.text())

            wait_s = max(0.0, int(self.wait_ms.text()) / 1000.0)
        except ValueError:
            QMessageBox.warning(self, "Input error", "Please enter valid numbers.")
            return

        if nx < 2 or ny < 2:
            QMessageBox.warning(self, "Input error", "Steps must be at least 2 in each axis.")
            return

        # axis arrays
        self._xs = np.linspace(x0, x1, nx)
        self._ys = np.linspace(y0, y1, ny)

        # allocate channel grids and sample table
        self._ch_data = [np.full((ny, nx), np.nan, dtype=float) for _ in range(4)]
        self._table6  = np.full((nx * ny, 6), np.nan, dtype=float)

        # configure each subplot's image & colorbar
        for i, ax in enumerate(self.axes):
            ax.clear()
            ax.set_box_aspect(1)
            ax.set_xlabel("X")
            ax.set_ylabel("Y")
            ax.grid(True, alpha=0.2)
            ax.set_title(f"Map {i+1} — {self.chan_labels[self.map_selects[i].currentIndex()]}")

            extent = [self._xs.min(), self._xs.max(), self._ys.min(), self._ys.max()]
            # start with the chosen channel for this map
            chan_idx = self.map_selects[i].currentIndex()
            img = ax.imshow(
                self._ch_data[chan_idx],
                origin="lower",
                extent=extent,
                aspect="auto",
                interpolation="nearest"
            )
            self.images[i] = img

            # inset colorbar axis (create once)
            if self.cax[i] is None:
                self.cax[i] = inset_axes(
                    ax,
                    width="4%", height="100%",
                    loc="lower left",
                    bbox_to_anchor=(1.02, 0., 1, 1),
                    bbox_transform=ax.transAxes,
                    borderpad=0
                )

            # create or update colorbar
            if self.cbars[i] is None:
                self.cbars[i] = self.fig.colorbar(img, cax=self.cax[i], label=self.chan_labels[chan_idx])
            else:
                self.cbars[i].update_normal(img)
                self.cbars[i].set_label(self.chan_labels[chan_idx])

        self.canvas.draw_idle()

        # buttons / status
        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.status_lbl.setText("Scanning…")

        # start worker
        self.worker = _ScanWorker4Map(self.galvo, self.lockin, self._xs, self._ys, wait_s, serpentine=False)
        self.worker.progress.connect(self._on_progress, Qt.ConnectionType.QueuedConnection)
        self.worker.finished.connect(self._on_finished, Qt.ConnectionType.QueuedConnection)
        self.worker.start()

    def _stop(self):
        if self.worker and self.worker.isRunning():
            self.worker.stop()
            self.status_lbl.setText("Stopping…")

    def _on_progress(self, ix: int, iy: int, l1x: float, l1y: float, l2x: float, l2y: float):
        # update 2D grids
        self._ch_data[0][iy, ix] = l1x
        self._ch_data[1][iy, ix] = l1y
        self._ch_data[2][iy, ix] = l2x
        self._ch_data[3][iy, ix] = l2y

        # update 6-col table (row-major)
        nx = self._ch_data[0].shape[1]
        row = iy * nx + ix
        self._table6[row, 0] = self._xs[ix]
        self._table6[row, 1] = self._ys[iy]
        self._table6[row, 2] = l1x
        self._table6[row, 3] = l1y
        self._table6[row, 4] = l2x
        self._table6[row, 5] = l2y

        # refresh each displayed map from its selected channel, autoscale, update colorbar
        for i in range(4):
            chan_idx = self.map_selects[i].currentIndex()
            arr = self._ch_data[chan_idx]
            img = self.images[i]
            if img is None:
                continue
            img.set_data(arr)
            if np.isfinite(arr).any():
                vmin, vmax = np.nanmin(arr), np.nanmax(arr)
                if vmin == vmax:
                    # avoid zero range
                    pad = 1e-12 if vmax == 0 else abs(vmax) * 1e-6
                    vmin, vmax = vmin - pad, vmax + pad
                img.set_clim(vmin, vmax)
                if self.cbars[i] is not None:
                    self.cbars[i].update_normal(img)

        self.canvas.draw_idle()

    def _on_finished(self, user_abort: bool, message: str):
        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.status_lbl.setText(message)
        self.worker = None

    def _on_map_select_changed(self, which_map: int):
        """When a dropdown changes, switch that subplot to the new channel."""
        if self.images[which_map] is None or self._ch_data[0] is None:
            # nothing plotted yet
            ax = self.axes[which_map]
            ax.set_title(f"Map {which_map+1} — {self.chan_labels[self.map_selects[which_map].currentIndex()]}")
            self.canvas.draw_idle()
            return

        chan_idx = self.map_selects[which_map].currentIndex()
        arr = self._ch_data[chan_idx]
        self.axes[which_map].set_title(f"Map {which_map+1} — {self.chan_labels[chan_idx]}")
        self.images[which_map].set_data(arr)

        if np.isfinite(arr).any():
            vmin, vmax = np.nanmin(arr), np.nanmax(arr)
            if vmin == vmax:
                pad = 1e-12 if vmax == 0 else abs(vmax) * 1e-6
                vmin, vmax = vmin - pad, vmax + pad
            self.images[which_map].set_clim(vmin, vmax)

        if self.cbars[which_map] is not None:
            self.cbars[which_map].update_normal(self.images[which_map])
            self.cbars[which_map].set_label(self.chan_labels[chan_idx])

        self.canvas.draw_idle()

    def _save(self):
        if self._table6 is None or self._xs is None or self._ys is None:
            QMessageBox.information(self, "Nothing to save", "Run a scan first.")
            return

        folder = self.folder.text().strip()
        fname  = (self.filename.text().strip() or "scan")

        try:
            os.makedirs(folder, exist_ok=True)
        except Exception as e:
            QMessageBox.critical(self, "Folder error", f"Could not create folder:\n{e}")
            return

        # Save figure (PNG)
        png_path = os.path.join(folder, f"{fname}.png")
        try:
            self.fig.savefig(png_path, dpi=200, bbox_inches="tight")
        except Exception as e:
            QMessageBox.warning(self, "Image save error", f"PNG failed:\n{e}")

        # Save 6-column data (TXT)
        txt_path = os.path.join(folder, f"{fname}.txt")
        try:
            hdr = self._build_header6()
            np.savetxt(
                txt_path,
                self._table6,
                delimiter="\t",
                fmt="%.10g",
                header=hdr,
                comments=""
            )
        except Exception as e:
            QMessageBox.critical(self, "Save error", f"Failed to save TXT:\n{e}")
            if os.path.exists(png_path):
                QMessageBox.information(self, "Partial save", f"Saved plot only:\n{png_path}")
            return

        QMessageBox.information(self, "Saved", f"Saved:\n{png_path}\n{txt_path}")

    def _build_header6(self) -> str:
        # Stats for the four channels
        stats_lines = []
        for i, name in enumerate(self.chan_labels):
            arr = self._ch_data[i]
            if arr is None or not np.isfinite(arr).any():
                stats_lines.append(f"  {name}: min=NaN, max=NaN")
            else:
                stats_lines.append(f"  {name}: min={np.nanmin(arr)}, max={np.nanmax(arr)}")

        lines = [
            "Four-Map XY Scan",
            f"Timestamp: {time.strftime('%Y-%m-%d %H:%M:%S')}",
            "",
            "Parameters:",
            f"  X start: {self.x_start.text()}",
            f"  X end:   {self.x_end.text()}",
            f"  X steps: {self.x_steps.text()}",
            f"  Y start: {self.y_start.text()}",
            f"  Y end:   {self.y_end.text()}",
            f"  Y steps: {self.y_steps.text()}",
            f"  Wait (ms): {self.wait_ms.text()}",
            "",
            "Columns (6):",
            "  1: Galvo X",
            "  2: Galvo Y",
            "  3: L1 X",
            "  4: L1 Y",
            "  5: L2 X",
            "  6: L2 Y",
            "",
            "Channel stats:",
            *stats_lines,
            "",
            "Table begins below this header (row-major, one row per (x,y) sample)."
        ]
        return "\n".join(lines)

    def closeEvent(self, event):
        if self.worker and self.worker.isRunning():
            self.worker.stop()
            self.worker.wait(2000)
        event.accept()
