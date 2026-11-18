"""
procedure_loader.py — load measurement setup procedures from PDF documents.

Each procedure is a single self-contained PDF in the ``procedures/`` folder
(authored in Word → Save As PDF, or any tool that produces a real text layer).

The agent reads a procedure two ways at once:
  * the exact text layer  → unambiguous numeric settings
  * a render of each page  → the vision model sees reference photos
                             (e.g. "good cut" vs "bad cut") in their layout

Public API
----------
list_procedures() -> list[{"name", "description"}]
load_procedure(name) -> (images_b64: list[str], text: str)
"""

from __future__ import annotations
import base64
from pathlib import Path

import fitz  # PyMuPDF

PROC_DIR   = Path(__file__).parent / "procedures"
RENDER_DPI = 130    # letter page ≈ 1100 px wide — readable without being huge
MAX_PAGES  = 15     # safety cap so a giant PDF can't flood the context window


def _dir() -> Path:
    return PROC_DIR


def list_procedures() -> list[dict]:
    """Return [{'name': stem, 'description': first text line}] for every PDF."""
    d = _dir()
    if not d.is_dir():
        return []
    out: list[dict] = []
    for pdf in sorted(d.glob("*.pdf")):
        desc = ""
        try:
            with fitz.open(pdf) as doc:
                if doc.page_count:
                    for line in doc[0].get_text("text").splitlines():
                        if line.strip():
                            desc = line.strip()
                            break
                    if not desc:
                        desc = "(no text layer — scanned PDF?)"
        except Exception as e:
            desc = f"(could not read: {e})"
        out.append({"name": pdf.stem, "description": desc})
    return out


def _resolve(name: str) -> Path:
    """Find a PDF by name (with/without extension, case-insensitive, partial)."""
    d = _dir()
    if not d.is_dir():
        raise FileNotFoundError("procedures folder not found")
    raw = (name or "").strip()
    stem = raw.lower()
    if stem.endswith(".pdf"):
        stem = stem[:-4]

    # exact filename or stem.pdf
    for cand in (raw, f"{raw}.pdf"):
        p = d / cand
        if p.is_file():
            return p
    # case-insensitive exact stem
    for pdf in d.glob("*.pdf"):
        if pdf.stem.lower() == stem:
            return pdf
    # unique partial match
    matches = [pdf for pdf in d.glob("*.pdf") if stem in pdf.stem.lower()]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        names = ", ".join(m.stem for m in matches)
        raise FileNotFoundError(f"'{name}' is ambiguous — matches: {names}")
    raise FileNotFoundError(f"No procedure matching '{name}'")


def load_procedure(name: str) -> tuple[list[str], str]:
    """
    Load a procedure PDF.

    Returns (images_b64, text):
      images_b64 — one base64-encoded PNG per page (page render)
      text       — extracted text layer, page by page
    """
    path = _resolve(name)
    images_b64: list[str] = []
    text_parts: list[str] = []
    zoom = RENDER_DPI / 72.0
    mat  = fitz.Matrix(zoom, zoom)

    with fitz.open(path) as doc:
        total = doc.page_count
        n = min(total, MAX_PAGES)
        for i in range(n):
            page = doc[i]
            t = page.get_text("text").strip()
            text_parts.append(f"--- Page {i + 1} ---\n{t}" if t
                              else f"--- Page {i + 1} (no text) ---")
            pix = page.get_pixmap(matrix=mat, alpha=False)
            images_b64.append(base64.b64encode(pix.tobytes("png")).decode())

    text = "\n\n".join(text_parts)
    if total > n:
        text += f"\n\n[NOTE: only the first {n} of {total} pages were included.]"
    return images_b64, text
