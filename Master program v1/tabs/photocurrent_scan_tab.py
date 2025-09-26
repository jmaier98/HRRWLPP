# photocurrent_scan_tab.py
from __future__ import annotations
import os, time, numpy as np
from PyQt6.QtCore import QThread, pyqtSignal, Qt
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QFormLayout, QPushButton, QLineEdit,
    QMessageBox, QFileDialog, QLabel, QGridLayout
)
from PyQt6.QtGui import QDoubleValidator, QIntValidator
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg as FigureCanvas
from matplotlib.figure import Figure
from mpl_toolkits.axes_grid1.inset_locator import inset_axes


class _ScanWorker(QThread):
    """Runs the XY raster scan on a background thread."""
    progress = pyqtSignal(int, int, float)    # ix, iy, value
    finished = pyqtSignal(bool, str)          # user_abort, message

    def __init__(self, galvo, lockin, xs, ys, wait_s: float, serpentine: bool = False):
        super().__init__()
        self._galvo       = galvo
        self._lockin      = lockin
        self._xs          = xs
        self._ys          = ys
        self._wait_s      = wait_s
        self._serpentine  = serpentine
        self._stop        = False

    def stop(self):
        self._stop = True

    def run(self):
        try:
            for iy, y in enumerate(self._ys):
                if self._stop: 
                    self.finished.emit(True, "Scan stopped by user.")
                    return

                # choose x order (serpentine keeps travel short)
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

                    # Convert local index to absolute ix for consistent indexing
                    ix = (len(self._xs)-1 - ix_local) if reverse else ix_local

                    # Move, wait, read
                    try:
                        self._galvo.move(float(x), float(y))
                    except Exception as e:
                        self.finished.emit(True, f"Galvo error at (x={x}, y={y}): {e}")
                        return

                    # settle & read
                    if self._wait_s > 0:
                        time.sleep(self._wait_s)

                    try:
                        val = float(self._lockin.read_x())  # change here if you want .read_r()
                    except Exception as e:
                        self.finished.emit(True, f"SR830 read error at (x={x}, y={y}): {e}")
                        return

                    self.progress.emit(ix, iy, val)

            self.finished.emit(False, "Scan complete.")
        except Exception as e:
            self.finished.emit(True, f"Unexpected error: {e}")


class PhotocurrentScanTab(QWidget):
    """
    2D photocurrent microscopy tab.
    Required InstrumentManager keys:
      • "GALVO" → exposes .movexy(x, y)
      • "SR830" → exposes .read_x()
    """
    def __init__(self, instrument_manager, state=None):
        super().__init__()
        self.IM    = instrument_manager
        self.state = state

        # --- Instruments ----------------------------------------------------
        try:
            self.galvo  = self.IM.get("Galvo")
            self.lockin = self.IM.get("SR830")
        except KeyError as err:
            raise RuntimeError(
                f"InstrumentManager is missing “{err.args[0]}”. "
                "Add it before constructing PhotocurrentScanTab."
            )

        # --- UI -------------------------------------------------------------
        outer = QHBoxLayout(self)

        # Left: controls
        form_box = QVBoxLayout()
        form = QFormLayout()

        dv = QDoubleValidator(-1e9, 1e9, 6, self)
        iv = QIntValidator(2, 5000, self)  # at least 2 points per axis

        self.x_start  = QLineEdit("-0.01");  self.x_start.setValidator(dv)
        self.x_end    = QLineEdit("0.01");   self.x_end.setValidator(dv)
        self.x_steps  = QLineEdit("30");   self.x_steps.setValidator(iv)

        self.y_start  = QLineEdit("-0.01");  self.y_start.setValidator(dv)
        self.y_end    = QLineEdit("0.01");   self.y_end.setValidator(dv)
        self.y_steps  = QLineEdit("30");   self.y_steps.setValidator(iv)

        self.wait_ms  = QLineEdit("10");    self.wait_ms.setValidator(QIntValidator(0, 100000, self))

        self.folder   = QLineEdit(os.path.expanduser("~/photocurrent_scans"))
        self.filename = QLineEdit("scan")

        self._cbar = None
        self._cax  = None

        form.addRow("X start:", self.x_start)
        form.addRow("X end:",   self.x_end)
        form.addRow("X steps:", self.x_steps)
        form.addRow("Y start:", self.y_start)
        form.addRow("Y end:",   self.y_end)
        form.addRow("Y steps:", self.y_steps)
        form.addRow("Wait (ms):", self.wait_ms)
        form_box.addLayout(form)
        form_box.addWidget(QLabel("Folder:"))
        form_box.addWidget(self.folder)

        form_box.addWidget(QLabel("Filename (no ext):"))
        form_box.addWidget(self.filename)

        # Buttons row
        btns = QGridLayout()
        self.start_btn = QPushButton("Start"); self.start_btn.clicked.connect(self._start)
        self.stop_btn  = QPushButton("Stop");  self.stop_btn.clicked.connect(self._stop); self.stop_btn.setEnabled(False)
        self.save_btn  = QPushButton("Save");  self.save_btn.clicked.connect(self._save)
        self.browse_btn= QPushButton("Browse…"); self.browse_btn.clicked.connect(self._browse)

        btns.addWidget(self.start_btn, 0, 0)
        btns.addWidget(self.stop_btn,  0, 1)
        btns.addWidget(self.save_btn,  1, 0)
        btns.addWidget(self.browse_btn,1, 1)
        form_box.addLayout(btns)

        # Small status line
        self.status_lbl = QLabel("Idle.")
        form_box.addWidget(self.status_lbl)
        form_box.addStretch(1)

        # Right: live plot
        self.fig = Figure(figsize=(6, 5), tight_layout=True)
        self.ax  = self.fig.add_subplot(111)
        self.ax.set_box_aspect(1)  # <- keep axes box square
        self.canvas = FigureCanvas(self.fig)

        outer.addLayout(form_box, stretch=1)
        outer.addWidget(self.canvas, stretch=5)

        # runtime
        self.worker: _ScanWorker | None = None
        self._img = None
        self._data = None
        self._xs = None
        self._ys = None

        # initial placeholder plot
        self.ax.set_title("Photocurrent (lock-in X)")
        self.ax.set_xlabel("X")
        self.ax.set_ylabel("Y")
        self.ax.grid(True, alpha=0.2)
        self.canvas.draw_idle()

    # ------------------------------------------------------------------ #
    # Actions                                                            #
    # ------------------------------------------------------------------ #
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

        # allocate data (row=y, col=x)
        self._data = np.full((ny, nx), np.nan, dtype=float)

        # configure plot image
        self.ax.clear()
        self.ax.set_box_aspect(1)  # <- re-apply after clear
        self.ax.set_title("Photocurrent (lock-in X)")
        self.ax.set_xlabel("X")
        self.ax.set_ylabel("Y")
        extent = [self._xs.min(), self._xs.max(), self._ys.min(), self._ys.max()]
        self._img = self.ax.imshow(
            self._data,
            origin="lower",
            extent=extent,
            aspect="auto",
            interpolation="nearest"
        )

        # Create a single inset colorbar axis once, then reuse it
        if self._cax is None:
            # an inset axis positioned just to the right of the main axes,
            # without changing the main axes size/aspect
            self._cax = inset_axes(
                self.ax,
                width="3%", height="100%",
                loc="lower left",
                bbox_to_anchor=(1.02, 0., 1, 1),
                bbox_transform=self.ax.transAxes,
                borderpad=0
            )

        if self._cbar is None:
            self._cbar = self.fig.colorbar(self._img, cax=self._cax, label="Lock-in X (Amps)")
        else:
            # update the existing colorbar to use the new image
            self._cbar.update_normal(self._img)
            self._cbar.set_label("Lock-in X (Amps)")
        self.canvas.draw_idle()

        # buttons / status
        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.status_lbl.setText("Scanning…")

        # start worker
        self.worker = _ScanWorker(self.galvo, self.lockin, self._xs, self._ys, wait_s, serpentine=False)
        self.worker.progress.connect(self._on_progress, Qt.ConnectionType.QueuedConnection)
        self.worker.finished.connect(self._on_finished, Qt.ConnectionType.QueuedConnection)
        self.worker.start()

    def _stop(self):
        if self.worker and self.worker.isRunning():
            self.worker.stop()
            self.status_lbl.setText("Stopping…")

    def _on_progress(self, ix: int, iy: int, val: float):
        # Update data and image
        if self._data is None:
            return
        self._data[iy, ix] = val

        if self._img is not None:
            self._img.set_data(self._data)

            # Only autoscale if some real values exist (not all NaN)
            if np.isfinite(self._data).any():
                vmin, vmax = np.nanmin(self._data), np.nanmax(self._data)
                self._img.set_clim(vmin, vmax)

                if self._cbar is not None:
                    self._cbar.update_normal(self._img)

        self.canvas.draw_idle()

    def _on_finished(self, user_abort: bool, message: str):
        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.status_lbl.setText(message)
        self.worker = None

    def _save(self):
        if self._data is None or self._xs is None or self._ys is None:
            QMessageBox.information(self, "Nothing to save", "Run a scan first.")
            return

        folder = self.folder.text().strip()
        fname  = (self.filename.text().strip() or "scan")

        try:
            os.makedirs(folder, exist_ok=True)
        except Exception as e:
            QMessageBox.critical(self, "Folder error", f"Could not create folder:\n{e}")
            return

        # --- Save figure screenshot ---
        png_path = os.path.join(folder, f"{fname}.png")
        try:
            # Use tight layout for a clean export with colorbar
            self.fig.savefig(png_path, dpi=200, bbox_inches="tight")
        except Exception as e:
            QMessageBox.warning(self, "Image save error", f"PNG failed:\n{e}")

        # --- Save data as TXT with header (scan params) ---
        txt_path = os.path.join(folder, f"{fname}.txt")
        try:
            # Build the same “axes in header row/col” table as before
            '''header_row = np.concatenate(([np.nan], self._xs))
            table      = np.column_stack((self._ys.reshape(-1, 1), self._data))
            out        = np.vstack((header_row, self._data))'''

            # Rich header describing parameters & stats
            hdr = self._build_header()

            # np.savetxt adds a leading comment by default ('# '), so set comments=''
            np.savetxt(txt_path, self._data, delimiter="\t", fmt="%.10g", header=hdr, comments="")
        except Exception as e:
            QMessageBox.critical(self, "Save error", f"Failed to save TXT:\n{e}")
            # If PNG succeeded, still report that
            if os.path.exists(png_path):
                QMessageBox.information(self, "Partial save", f"Saved plot only:\n{png_path}")
            return



    def _build_header(self) -> str:
        """Build a human-readable header summarizing the scan and data."""
        if self._xs is None or self._ys is None or self._data is None:
            return "Photocurrent scan (no data)\n"

        ny, nx = self._data.shape
        vmin = np.nanmin(self._data) if np.isfinite(self._data).any() else np.nan
        vmax = np.nanmax(self._data) if np.isfinite(self._data).any() else np.nan
        lines = [
            "Photocurrent scan",
            f"Timestamp: {time.strftime('%Y-%m-%d %H:%M:%S')}",
            "",
            "Parameters:",
            f"  X start: {self.x_start.text()}",
            f"  X end:   {self.x_end.text()}",
            f"  X steps: {self.x_steps.text()} (nx={nx})",
            f"  Y start: {self.y_start.text()}",
            f"  Y end:   {self.y_end.text()}",
            f"  Y steps: {self.y_steps.text()} (ny={ny})",
            f"  Wait (ms): {self.wait_ms.text()}",
            "",
            "Axes (for the table below):",
            "  First row:   NaN, then X coordinates",
            "  First col.:  Y coordinates",
            "",
            "Data stats (lock-in X, Amps):",
            f"  min: {vmin}",
            f"  max: {vmax}",
            "",
            "Table begins below this header."
        ]
        return "\n".join(lines)
    # Ensure the worker is stopped on close
    def closeEvent(self, event):
        if self.worker and self.worker.isRunning():
            self.worker.stop()
            self.worker.wait(2000)
        event.accept()
