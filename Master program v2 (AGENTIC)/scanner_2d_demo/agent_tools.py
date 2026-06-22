"""
Agent tool definitions — maps controller methods to Claude tool-use format.

Scan results are returned as a TWO-PART tool result:
  1. A PNG image of the scan rendered with matplotlib  (~300-1000 tokens)
  2. A short JSON text block with stats + top-N peaks  (~50 tokens)

This is far cheaper than sending the raw point array (which grows as N²)
while giving the agent richer spatial context than a number summary alone.

dispatch() returns either:
  - a dict   → caller JSON-serialises it as a plain text tool result
  - a list   → caller passes it directly as the tool result content
                (list of {"type":"image",...} and {"type":"text",...} blocks)
"""

import io
import json
import base64

import numpy as np
import matplotlib
matplotlib.use("Agg")           # headless — no Qt window
import matplotlib.pyplot as plt

from controller import ScannerController


# ── Image rendering ───────────────────────────────────────────

def _render_scan_png(pts: list[dict], title: str = "Scan") -> str:
    """
    Render scan data as a compact viridis PNG and return it as a base64 string.
    Output is always ≤ 400×400 px to keep token cost low.
    """
    xs   = [p["x"]     for p in pts]
    ys   = [p["y"]     for p in pts]
    vals = [p["value"] for p in pts]

    fig, ax = plt.subplots(figsize=(3.5, 3.0), dpi=90)
    fig.patch.set_facecolor("#1e1e1e")
    ax.set_facecolor("#1e1e1e")

    ux, uy = sorted(set(xs)), sorted(set(ys))
    if len(ux) > 1 and len(uy) > 1 and len(pts) == len(ux) * len(uy):
        # Grid → imshow
        grid = np.zeros((len(uy), len(ux)))
        for p in pts:
            grid[uy.index(p["y"]), ux.index(p["x"])] = p["value"]
        im = ax.imshow(
            grid,
            extent=[min(xs), max(xs), min(ys), max(ys)],
            origin="lower", aspect="auto", cmap="viridis",
        )
        plt.colorbar(im, ax=ax).ax.yaxis.set_tick_params(color="white", labelcolor="white")
    else:
        sc = ax.scatter(xs, ys, c=vals, cmap="viridis", s=8)
        plt.colorbar(sc, ax=ax).ax.yaxis.set_tick_params(color="white", labelcolor="white")

    ax.set_title(title, color="white", fontsize=9)
    ax.set_xlabel("X (mm)", color="grey", fontsize=8)
    ax.set_ylabel("Y (mm)", color="grey", fontsize=8)
    ax.tick_params(colors="grey", labelsize=7)
    for spine in ax.spines.values():
        spine.set_edgecolor("#555")

    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight", dpi=90,
                facecolor=fig.get_facecolor())
    plt.close(fig)
    buf.seek(0)
    return base64.standard_b64encode(buf.read()).decode("utf-8")


# ── Peak finding ──────────────────────────────────────────────

def _find_top_peaks(pts: list[dict], n: int = 5, min_separation: float = 10.0) -> list[dict]:
    """
    Return up to n spatially-separated local maxima, sorted by value descending.
    min_separation (mm) prevents multiple results on the same broad peak.
    """
    sorted_pts = sorted(pts, key=lambda p: p["value"], reverse=True)
    accepted = []
    for pt in sorted_pts:
        too_close = any(
            np.hypot(pt["x"] - a["x"], pt["y"] - a["y"]) < min_separation
            for a in accepted
        )
        if not too_close:
            accepted.append(pt)
        if len(accepted) >= n:
            break
    return [
        {"x": round(p["x"], 2), "y": round(p["y"], 2), "value": round(p["value"], 4)}
        for p in accepted
    ]


# ── Scan result → multipart tool result ──────────────────────

def _scan_to_content(result: dict, title: str = "Scan") -> list[dict]:
    """
    Convert a scan result (with raw 'data' array) into a two-part content list:
      [image_block, text_block]
    The image gives full spatial context; the text block gives machine-readable
    stats and peak coordinates for follow-up moves.
    """
    pts  = result.get("data", [])
    if not pts:
        return [{"type": "text", "text": json.dumps({"error": "no data"})}]

    vals      = [p["value"] for p in pts]
    top_peaks = _find_top_peaks(pts, n=5, min_separation=10.0)
    png_b64   = _render_scan_png(pts, title=title)

    summary = {
        "num_points": len(pts),
        "value_stats": {
            "min":  round(min(vals), 4),
            "max":  round(max(vals), 4),
            "mean": round(float(np.mean(vals)), 4),
        },
        "top_peaks": top_peaks,   # up to 5 spatially-distinct peaks, strongest first
    }

    return [
        {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/png",
                "data": png_b64,
            },
        },
        {
            "type": "text",
            "text": json.dumps(summary),
        },
    ]


# ── Pump-probe trace rendering ────────────────────────────────

# Cycle of colours for overlaid traces
_TRACE_COLOURS = ["#2ecc71", "#3498db", "#e67e22", "#e74c3c",
                  "#9b59b6", "#1abc9c", "#f1c40f", "#e91e63"]

def _render_trace_png(traces: list[dict], title: str = "Time Traces") -> str:
    """
    Render one or more pump-probe time traces as a PNG (base64).
    Each trace dict must have 'delays', 'signals', 'x', 'y'.
    """
    fig, ax = plt.subplots(figsize=(4.0, 3.0), dpi=90)
    fig.patch.set_facecolor("#1e1e1e")
    ax.set_facecolor("#1e1e1e")

    for i, tr in enumerate(traces):
        colour = _TRACE_COLOURS[i % len(_TRACE_COLOURS)]
        label  = f"({tr['x']:.1f}, {tr['y']:.1f}) mm"
        ax.plot(tr["delays"], tr["signals"], color=colour,
                linewidth=1.2, label=label)

    ax.axhline(0, color="#555", linewidth=0.7, linestyle="--")
    ax.axvline(0, color="#555", linewidth=0.7, linestyle=":")
    ax.set_title(title, color="white", fontsize=9)
    ax.set_xlabel("Delay (ps)", color="grey", fontsize=8)
    ax.set_ylabel("Signal (mOD)", color="grey", fontsize=8)
    ax.tick_params(colors="grey", labelsize=7)
    for spine in ax.spines.values():
        spine.set_edgecolor("#555")
    if len(traces) <= 6:
        ax.legend(fontsize=7, facecolor="#2b2b2b", labelcolor="white",
                  edgecolor="#555", loc="upper right")

    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight", dpi=90,
                facecolor=fig.get_facecolor())
    plt.close(fig)
    buf.seek(0)
    return base64.standard_b64encode(buf.read()).decode("utf-8")


def _trace_to_content(trace_dict: dict) -> list[dict]:
    """Convert a single captured trace result into [image_block, text_block]."""
    delays  = trace_dict.get("delays",  [])
    signals = trace_dict.get("signals", [])
    if not delays:
        return [{"type": "text", "text": json.dumps({"error": "no data"})}]

    peak_idx  = int(np.argmax(signals))
    min_idx   = int(np.argmin(signals))
    summary = {
        "position_mm":    {"x": trace_dict["x"], "y": trace_dict["y"]},
        "pump_power_mw":  trace_dict["pump_power_mw"],
        "probe_power_mw": trace_dict["probe_power_mw"],
        "num_points":     len(delays),
        "delay_range_ps": [round(min(delays), 3), round(max(delays), 3)],
        "signal_stats": {
            "max":       round(max(signals), 4),
            "min":       round(min(signals), 4),
            "at_t0":     round(signals[min(range(len(delays)),
                                         key=lambda i: abs(delays[i]))], 4),
        },
        "peak": {"delay_ps": round(delays[peak_idx], 3),
                 "signal_mOD": round(signals[peak_idx], 4)},
    }
    png_b64 = _render_trace_png(
        [{"delays": delays, "signals": signals,
          "x": trace_dict["x"], "y": trace_dict["y"]}],
        title=f"Trace @ ({trace_dict['x']:.1f}, {trace_dict['y']:.1f}) mm",
    )
    return [
        {"type": "image",
         "source": {"type": "base64", "media_type": "image/png", "data": png_b64}},
        {"type": "text", "text": json.dumps(summary)},
    ]


def _all_traces_to_content(traces_result: dict) -> list[dict]:
    """Convert get_pp_traces result into [image_block, text_block]."""
    traces = traces_result.get("traces", [])
    if not traces:
        return [{"type": "text", "text": json.dumps({"num_traces": 0})}]

    png_b64 = _render_trace_png(traces, title=f"All PP Traces ({len(traces)})")
    summary = {
        "num_traces": len(traces),
        "positions":  [{"x": t["x"], "y": t["y"]} for t in traces],
    }
    return [
        {"type": "image",
         "source": {"type": "base64", "media_type": "image/png", "data": png_b64}},
        {"type": "text", "text": json.dumps(summary)},
    ]


# ── Tool definitions (Claude API format) ──────────────────────

TOOLS = [
    {
        "name": "get_state",
        "description": (
            "Get the current scanner state: stage position, dwell time, "
            "whether a scan is running, and how many data points have been collected."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "move_to",
        "description": (
            "Move the stage to an absolute position (x, y) in mm. "
            "Values are clamped to [0, 100]."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "x": {"type": "number", "description": "X position in mm"},
                "y": {"type": "number", "description": "Y position in mm"},
            },
            "required": ["x", "y"],
        },
    },
    {
        "name": "move_relative",
        "description": "Move the stage by a relative offset (dx, dy) in mm.",
        "input_schema": {
            "type": "object",
            "properties": {
                "dx": {"type": "number", "description": "X offset in mm"},
                "dy": {"type": "number", "description": "Y offset in mm"},
            },
            "required": ["dx", "dy"],
        },
    },
    {
        "name": "measure",
        "description": (
            "Take a single detector measurement at the current stage position. "
            "Returns x, y, and the measured value."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "set_dwell_time",
        "description": "Set the detector dwell time in seconds. Clamped to [0.01, 10.0].",
        "input_schema": {
            "type": "object",
            "properties": {
                "seconds": {"type": "number", "description": "Dwell time in seconds"},
            },
            "required": ["seconds"],
        },
    },
    {
        "name": "run_line_scan",
        "description": (
            "Scan along a straight line from (x_start, y_start) to (x_end, y_end), "
            "collecting num_points measurements. Returns a PNG image of the scan "
            "plus stats and the top spatially-separated peak coordinates."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "x_start": {"type": "number"},
                "y_start": {"type": "number"},
                "x_end":   {"type": "number"},
                "y_end":   {"type": "number"},
                "num_points": {"type": "integer", "description": "Number of points"},
            },
            "required": ["x_start", "y_start", "x_end", "y_end", "num_points"],
        },
    },
    {
        "name": "run_2d_scan",
        "description": (
            "Run a 2D raster scan over a rectangular region. "
            "Scans x_points columns × y_points rows. Returns a PNG image of the scan "
            "plus stats and the top spatially-separated peak coordinates."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "x_start":  {"type": "number"},
                "y_start":  {"type": "number"},
                "x_end":    {"type": "number"},
                "y_end":    {"type": "number"},
                "x_points": {"type": "integer", "description": "Points along X"},
                "y_points": {"type": "integer", "description": "Points along Y"},
            },
            "required": ["x_start", "y_start", "x_end", "y_end", "x_points", "y_points"],
        },
    },
    {
        "name": "get_data",
        "description": (
            "Retrieve a PNG image and stats summary of all data collected so far."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "clear_data",
        "description": "Discard all collected CW scan data.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },

    # ── Pump-probe tools ───────────────────────────────────────
    {
        "name": "set_pump_power",
        "description": "Set the pump beam power in mW. Clamped to [0.01, 200] mW.",
        "input_schema": {
            "type": "object",
            "properties": {
                "power_mw": {"type": "number", "description": "Pump power in mW"},
            },
            "required": ["power_mw"],
        },
    },
    {
        "name": "set_probe_power",
        "description": "Set the probe beam power in mW. Clamped to [0.001, 10] mW.",
        "input_schema": {
            "type": "object",
            "properties": {
                "power_mw": {"type": "number", "description": "Probe power in mW"},
            },
            "required": ["power_mw"],
        },
    },
    {
        "name": "measure_at_delay",
        "description": (
            "Take a single pump-probe measurement at a specific time delay (ps) "
            "at the current stage position. Returns delay and signal in mOD."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "delay_ps": {"type": "number", "description": "Time delay in picoseconds"},
            },
            "required": ["delay_ps"],
        },
    },
    {
        "name": "capture_time_trace",
        "description": (
            "Sweep the time delay from t_start to t_end (ps) collecting num_points "
            "measurements at the current stage position. Returns a PNG image of the "
            "transient trace plus peak delay, signal stats, and position."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "t_start":   {"type": "number", "description": "Start delay (ps)"},
                "t_end":     {"type": "number", "description": "End delay (ps)"},
                "num_points":{"type": "integer","description": "Number of delay points"},
            },
            "required": ["t_start", "t_end", "num_points"],
        },
    },
    {
        "name": "get_pp_traces",
        "description": (
            "Retrieve a PNG image showing all captured pump-probe traces overlaid, "
            "plus a list of their positions."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "clear_pp_data",
        "description": "Discard all captured pump-probe traces.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
]


# ── Dispatch ──────────────────────────────────────────────────

def dispatch(controller: ScannerController, tool_name: str, tool_input: dict):
    """
    Execute a tool call. Returns either:
      - dict  → plain JSON result (non-scan tools)
      - list  → multipart content [image_block, text_block] (scan tools)
    """
    match tool_name:
        case "get_state":
            return controller.get_state()
        case "move_to":
            return controller.move_to(tool_input["x"], tool_input["y"])
        case "move_relative":
            return controller.move_relative(tool_input["dx"], tool_input["dy"])
        case "measure":
            return controller.measure()
        case "set_dwell_time":
            return controller.set_dwell_time(tool_input["seconds"])
        case "run_line_scan":
            raw = controller.run_line_scan(
                tool_input["x_start"], tool_input["y_start"],
                tool_input["x_end"],   tool_input["y_end"],
                tool_input["num_points"],
            )
            return _scan_to_content(raw, title=f"Line Scan ({tool_input['num_points']} pts)")
        case "run_2d_scan":
            raw = controller.run_2d_scan(
                tool_input["x_start"], tool_input["y_start"],
                tool_input["x_end"],   tool_input["y_end"],
                tool_input["x_points"], tool_input["y_points"],
            )
            return _scan_to_content(
                raw,
                title=f"2D Scan {tool_input['x_points']}×{tool_input['y_points']}"
            )
        case "get_data":
            return _scan_to_content(controller.get_data(), title="All Collected Data")
        case "clear_data":
            return controller.clear_data()
        case "set_pump_power":
            return controller.set_pump_power(tool_input["power_mw"])
        case "set_probe_power":
            return controller.set_probe_power(tool_input["power_mw"])
        case "measure_at_delay":
            return controller.measure_at_delay(tool_input["delay_ps"])
        case "capture_time_trace":
            raw = controller.capture_time_trace(
                tool_input["t_start"], tool_input["t_end"], tool_input["num_points"]
            )
            return _trace_to_content(raw)
        case "get_pp_traces":
            return _all_traces_to_content(controller.get_pp_traces())
        case "clear_pp_data":
            return controller.clear_pp_data()
        case _:
            return {"error": f"Unknown tool: {tool_name}"}
