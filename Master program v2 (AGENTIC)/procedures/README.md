# Measurement procedures

Drop one **PDF per procedure** in this folder. The agent discovers them with the
`list_procedures` tool and loads them with `get_procedure`. When you ask the agent
to *"run the standard photocurrent map"* (or any procedure name), it reads the
document — both the written steps and the reference images — and carries it out
end-to-end with its instrument tools.

## How to author a procedure

1. Write it in **Word** (or Google Docs, LaTeX, anything that exports a real PDF).
2. **Save As / Export → PDF.** Don't scan a printout — the agent reads the PDF's
   text layer for exact settings, so the text must be selectable, not a photo.
3. Drop the `.pdf` here. That's it — no restart needed; `list_procedures` re-scans
   the folder each time.

## Recommended structure

- **First line = a one-sentence summary.** This is what shows up in
  `list_procedures`, so make it descriptive, e.g.
  *"Standard photocurrent map — spatial map, focus on an electrode edge, align to
  electrode features, then a 3D scan with alignment."*
- **Numbered steps**, each with the **standard settings** spelled out (axis names,
  ranges, step sizes, measurement, wait times). Use the exact names the agent's
  tools expect where you can — it will verify with `list_axes` / `list_measurements`.
- **Reference images inline**, right next to the step they belong to, each with a
  caption that says what it shows — e.g. *"GOOD focus cut: one sharp edge"* and
  *"BAD focus cut: shallow and noisy — refocus."* The agent compares its live
  results against these to decide whether to proceed or retry.

## Tips

- Keep procedures short (a few pages). Long PDFs are capped at the first 15 pages
  and large image counts slow the local model.
- The agent runs **fully autonomously** — be explicit about pass/fail criteria so
  it knows when a step is good enough to move on.
- See `standard_photocurrent_map.pdf` in this folder for a complete worked example,
  and `make_sample_pdf.py` for how it was generated.
