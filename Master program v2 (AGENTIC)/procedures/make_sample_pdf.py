"""
make_sample_pdf.py — generate a complete sample procedure PDF.

This is a DEMO/fixture generator. Normally you author procedures in Word and
export to PDF; this script just produces one realistic example so the parser has
something to read and you can see what a finished procedure looks like.

Run:  python procedures/make_sample_pdf.py
Output: procedures/standard_photocurrent_map.pdf
"""

from __future__ import annotations
import io
from pathlib import Path

import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import fitz  # PyMuPDF

OUT = Path(__file__).parent / "standard_photocurrent_map.pdf"

# ── Reference figures ────────────────────────────────────────────────────────


def _sigmoid(x, x0, w):
    return 1.0 / (1.0 + np.exp(-(x - x0) / w))


def fig_focus_cuts() -> bytes:
    """Good vs bad focus cut (galvo sweep across an edge)."""
    rng = np.random.default_rng(1)
    x = np.linspace(-300, 300, 60)
    good = _sigmoid(x, -10, 18) + rng.normal(0, 0.015, x.size)
    bad  = _sigmoid(x, 40, 130) + rng.normal(0, 0.08, x.size)

    fig, axes = plt.subplots(1, 2, figsize=(7.0, 2.6), dpi=150)
    for ax, y, title, color in (
        (axes[0], good, "GOOD focus cut:\none sharp edge", "#1a7f37"),
        (axes[1], bad,  "BAD focus cut:\nshallow & noisy — refocus", "#c0392b"),
    ):
        ax.plot(x, y, "o-", ms=3, lw=1.2, color=color, alpha=0.85)
        ax.set_title(title, fontsize=9)
        ax.set_xlabel("Galvo position", fontsize=8)
        ax.set_ylabel("Signal", fontsize=8)
        ax.tick_params(labelsize=7)
        ax.grid(True, alpha=0.3)
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight")
    plt.close(fig)
    return buf.getvalue()


def fig_align_cuts() -> bytes:
    """Good vs bad alignment cut across an electrode/wire (double-sigmoid hat)."""
    rng = np.random.default_rng(2)
    x = np.linspace(-100, 100, 80)
    hat = _sigmoid(x, -30, 6) - _sigmoid(x, 30, 6)
    good = hat + rng.normal(0, 0.02, x.size)
    bad  = _sigmoid(x, -30, 6) - _sigmoid(x, 10, 22) + rng.normal(0, 0.07, x.size)

    fig, axes = plt.subplots(1, 2, figsize=(7.0, 2.6), dpi=150)
    for ax, y, title, color in (
        (axes[0], good, "GOOD alignment cut:\nclean symmetric feature", "#1a7f37"),
        (axes[1], bad,  "BAD alignment cut:\nasymmetric / noisy — redo", "#c0392b"),
    ):
        ax.plot(x, y, "o-", ms=3, lw=1.2, color=color, alpha=0.85)
        ax.set_title(title, fontsize=9)
        ax.set_xlabel("Galvo position", fontsize=8)
        ax.set_ylabel("Signal", fontsize=8)
        ax.tick_params(labelsize=7)
        ax.grid(True, alpha=0.3)
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight")
    plt.close(fig)
    return buf.getvalue()


# ── PDF assembly ─────────────────────────────────────────────────────────────

PAGE1 = """\
Standard photocurrent map: coarse spatial map, focus on an electrode edge, align \
to electrode features, then a 3D scan with alignment.

Goal: produce a clean, in-focus, drift-corrected photocurrent map of the device,
ending with a 3D scan (e.g. gate-voltage or delay stacked maps).

Default measurement: "PC and Reflectivity".  Default fast axis: Galvo X.

Step 1 - Coarse spatial map.
  Run a 2D scan to locate the device.
  X = Galvo X (mu), Y = Galvo Y (mu).
  Range: X -400..400, Y -400..400.  Step: 20 (both).  Wait: 5 ms.
  Goal: see the electrodes and channel. If off-frame, recentre and repeat.

Step 2 - Focus on an electrode edge.
  Pick a horizontal line cut near the BOTTOM electrode (a sharp reflectivity
  edge). Run Find Focus along that cut.
  Galvo sweep: Start (-300, 0) to End (300, 0), 50 steps.
  Z: 5 rough steps x 0.1, then 5 fine steps x 0.02.  Wait: 50 ms.
  PASS if the cut looks like the GOOD example below (one sharp edge). If it is
  shallow or noisy (BAD example), move to a cleaner edge and refocus before
  continuing.
"""

PAGE2 = """\
Step 3 - Second spatial map (in focus).
  Repeat the Step 1 map now that focus is set. Confirm the electrodes look
  sharper than the first map.

Step 4 - Set up alignment on electrode features.
  Use the Alignment tab to lock onto a distinct electrode feature so later scans
  are drift-corrected.
  Horizontal: Start X 0, Start Y 0, distance 100, 50 points, fit = double
  sigmoid (wire/edge). Vertical: same, distance 100, 50 points.
  Run "set origin" first to store the reference, using a clean feature.
  PASS if both cuts look like the GOOD alignment example below (clean, roughly
  symmetric). If asymmetric or noisy (BAD example), choose a different feature.

Step 5 - Finer spatial map.
  Run a higher-resolution 2D scan over the region of interest.
  Range: X -150..150, Y -150..150.  Step: 5 (both).  Wait: 5 ms.

Step 6 - 3D scan with alignment.
  Configure the 3D Scan tab: X = Galvo X (mu), Y = Galvo Y (mu), and the third
  (Z) axis per the experiment (e.g. Keithley V or Delay (ps)).
  Enable "Align during scan", align every 50 px. Use the finer-map ranges from
  Step 5 for X and Y. Then run the 3D scan and save.
"""


def build():
    doc = fitz.open()

    # Page 1
    p1 = doc.new_page(width=612, height=792)
    p1.insert_textbox(fitz.Rect(54, 50, 558, 470), PAGE1,
                      fontname="helv", fontsize=10.5, lineheight=1.25)
    img1 = fig_focus_cuts()
    p1.insert_image(fitz.Rect(74, 500, 538, 700), stream=img1)

    # Page 2
    p2 = doc.new_page(width=612, height=792)
    p2.insert_textbox(fitz.Rect(54, 50, 558, 470), PAGE2,
                      fontname="helv", fontsize=10.5, lineheight=1.25)
    img2 = fig_align_cuts()
    p2.insert_image(fitz.Rect(74, 500, 538, 700), stream=img2)

    doc.save(OUT)
    doc.close()
    print(f"Wrote {OUT}")


if __name__ == "__main__":
    build()
