"""
Agent tool definitions for the microscope controller.

dispatch() returns either:
  - dict  → plain JSON result (non-scan tools)
  - list  → multipart content [image_block, text_block] (scan tools)
"""

import json
from controller import MicroscopeController

TOOLS = [
    {
        "name": "list_instrument_controls",
        "description": (
            "List all named instrument controls (entry + Go fields) available in the "
            "Dynamic Controls tab, e.g. 'Pump power (µW)', 'Delay stage pos (mm)', etc."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "list_instrument_buttons",
        "description": (
            "List all named command buttons in the Dynamic Controls tab, "
            "e.g. 'Open pump shutter', 'Home rails', 'Build probe power table'."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "set_instrument_control",
        "description": (
            "Set a named instrument control to a value (equivalent to typing a value "
            "and pressing Go in the Dynamic Controls tab). Use list_instrument_controls "
            "to discover valid names."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "control": {"type": "string", "description": "Control label (case-insensitive)"},
                "value":   {"type": "number", "description": "Value to set"},
            },
            "required": ["control", "value"],
        },
    },
    {
        "name": "press_instrument_button",
        "description": (
            "Press a named command button in the Dynamic Controls tab "
            "(e.g. open/close shutters, home motors, toggle Keithley output). "
            "Use list_instrument_buttons to discover valid names."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "button": {"type": "string", "description": "Button label (case-insensitive)"},
            },
            "required": ["button"],
        },
    },

    {
        "name": "get_state",
        "description": (
            "Get the current microscope state: open instruments and key settings "
            "(positions, powers, angles, etc.)."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "list_axes",
        "description": "List the available axis/setter names that can be used in run_1d_scan.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "list_measurements",
        "description": "List the available measurement function names that can be used in run_1d_scan.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "run_1d_scan",
        "description": (
            "Run a 1D scan along a named axis, collecting measurements at each point. "
            "Returns a PNG plot plus statistics (min, max, mean of d1). "
            "Use list_axes and list_measurements first if you are unsure of the names."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "axis_key":  {"type": "string", "description": "Axis setter name (from list_axes)"},
                "meas_key":  {"type": "string", "description": "Measurement name (from list_measurements)"},
                "x_start":   {"type": "number", "description": "Scan start value"},
                "x_end":     {"type": "number", "description": "Scan end value"},
                "n_points":  {"type": "integer", "description": "Number of points"},
                "n_scans":   {"type": "integer", "description": "Number of averages (scans to average)", "default": 1},
                "wait_ms":   {"type": "integer", "description": "Wait time between points in ms", "default": 10},
            },
            "required": ["axis_key", "meas_key", "x_start", "x_end", "n_points"],
        },
    },
    {
        "name": "run_2d_scan",
        "description": (
            "Run a 2D raster scan. Each axis is set independently using named setters "
            "(use list_axes to find valid names). Returns a PNG heatmap plus d1 statistics. "
            "X is the fast axis by default. "
            "Specify the range with x_step and y_step (preferred) OR x_points/y_points."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "x_key":    {"type": "string", "description": "X axis setter name"},
                "y_key":    {"type": "string", "description": "Y axis setter name"},
                "meas_key": {"type": "string", "description": "Measurement name"},
                "x_start":  {"type": "number"},
                "x_end":    {"type": "number"},
                "x_step":   {"type": "number", "description": "Step size along X (preferred over x_points)"},
                "y_start":  {"type": "number"},
                "y_end":    {"type": "number"},
                "y_step":   {"type": "number", "description": "Step size along Y (preferred over y_points)"},
                "x_points": {"type": "integer", "description": "Points along X (used only if x_step not given)"},
                "y_points": {"type": "integer", "description": "Points along Y (used only if y_step not given)"},
                "n_scans":  {"type": "integer", "description": "Averages (full scans)", "default": 1},
                "wait_ms":  {"type": "integer", "description": "Wait per point (ms)", "default": 5},
            },
            "required": ["x_key", "y_key", "meas_key", "x_start", "x_end", "y_start", "y_end"],
        },
    },
    {
        "name": "set_scan_filename",
        "description": (
            "Set the filename that will be used when saving the next scan. "
            "Call this before save_1d_scan or save_2d_scan when the user has "
            "specified a name for the file. Do not include a file extension."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "filename": {"type": "string", "description": "Base filename without extension"},
                "scan_type": {"type": "string", "enum": ["1d", "2d"],
                              "description": "Which scan tab to set the filename for"},
            },
            "required": ["filename", "scan_type"],
        },
    },
    {
        "name": "save_1d_scan",
        "description": (
            "Save the most recent 1D scan data to disk. "
            "Uses the folder already selected in the 1D Scan tab. "
            "Optionally accepts a filename (no extension); if omitted the current "
            "filename field is used. Will never overwrite an existing file — "
            "a numeric suffix is appended automatically if needed."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "filename": {"type": "string",
                             "description": "Optional base filename (no extension)"},
            },
            "required": [],
        },
    },
    {
        "name": "save_2d_scan",
        "description": (
            "Save the most recent 2D scan data to disk. "
            "Uses the folder already selected in the 2D Scan tab. "
            "Optionally accepts a filename (no extension); if omitted the current "
            "filename field is used. Will never overwrite an existing file — "
            "a numeric suffix is appended automatically if needed."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "filename": {"type": "string",
                             "description": "Optional base filename (no extension)"},
            },
            "required": [],
        },
    },
    {
        "name": "get_alignment_settings",
        "description": (
            "Return all current Alignment tab settings (scan ranges, fit models, "
            "measurement signal, current offsets and feature positions)."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "set_alignment_settings",
        "description": (
            "Configure any subset of Alignment tab settings before running. "
            "Call get_alignment_settings first to see valid measurement names and fit models. "
            "All fields are optional."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "measurement":  {"type": "string",  "description": "Signal to measure (e.g. 'Lock-in X')"},
                "wait_ms":      {"type": "integer"},
                "h_start_x":    {"type": "number",  "description": "Horizontal scan start X"},
                "h_start_y":    {"type": "number",  "description": "Horizontal scan start Y"},
                "h_distance":   {"type": "number",  "description": "Horizontal scan distance"},
                "h_n_points":   {"type": "integer"},
                "h_left_right": {"type": "boolean", "description": "True = left→right"},
                "h_fit_model":  {"type": "string",  "description": "'sigmoid' or 'double_sigmoid'"},
                "v_start_x":    {"type": "number",  "description": "Vertical scan start X"},
                "v_start_y":    {"type": "number",  "description": "Vertical scan start Y"},
                "v_distance":   {"type": "number",  "description": "Vertical scan distance"},
                "v_n_points":   {"type": "integer"},
                "v_bottom_top": {"type": "boolean", "description": "True = bottom→top"},
                "v_fit_model":  {"type": "string",  "description": "'sigmoid' or 'double_sigmoid'"},
            },
            "required": [],
        },
    },
    {
        "name": "run_alignment",
        "description": (
            "Run the alignment scan (horizontal + vertical galvo sweeps) and return "
            "the found feature center and offset from the original position. "
            "If set_origin=true, saves the found position as the new reference origin "
            "(use this the first time, or after physically moving the sample). "
            "Otherwise finds the drift offset relative to the stored origin."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "set_origin": {"type": "boolean",
                               "description": "If true, treat the found position as the new origin",
                               "default": False},
            },
            "required": [],
        },
    },
    {
        "name": "get_focus_settings",
        "description": (
            "Return all current Find Focus tab settings (galvo sweep range, "
            "Z scan parameters, measurement signal)."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "set_focus_settings",
        "description": (
            "Configure any subset of Find Focus tab settings before running. "
            "Call get_focus_settings to see available measurement names. "
            "All fields are optional."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "measurement":       {"type": "string",  "description": "Signal to measure"},
                "galvo_x_start":     {"type": "number"},
                "galvo_y_start":     {"type": "number"},
                "galvo_x_end":       {"type": "number"},
                "galvo_y_end":       {"type": "number"},
                "n_galvo_steps":     {"type": "integer", "description": "Points in galvo sweep"},
                "z_start":           {"type": "number",  "description": "Z start position"},
                "z_rough_steps":     {"type": "integer"},
                "z_rough_step_size": {"type": "number"},
                "z_fine_steps":      {"type": "integer"},
                "z_fine_step_size":  {"type": "number"},
                "wait_ms":           {"type": "integer"},
            },
            "required": [],
        },
    },
    {
        "name": "run_find_focus",
        "description": (
            "Run the focus-finding procedure (rough + fine Z scan) and move to the "
            "best-focus Z position. Returns the best Z value and spot size. "
            "Call set_focus_settings first if you need to adjust parameters."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_3d_scan_settings",
        "description": (
            "Return the current settings of the 3D Scan tab: axis setters, ranges, "
            "measurement function, wait times, alignment, and save folder/filename. "
            "Also lists all available setter and measurement names."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "set_3d_scan_settings",
        "description": (
            "Configure any subset of the 3D Scan tab settings before running a scan. "
            "Call get_3d_scan_settings first to discover valid axis/measurement names. "
            "All fields are optional — only the ones you supply will be changed."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "fast_axis":       {"type": "string",  "description": "'X fast', 'Y fast', or 'Z fast'"},
                "z_setter":        {"type": "string",  "description": "Z-axis setter name"},
                "z_start":         {"type": "number"},
                "z_end":           {"type": "number"},
                "z_step":          {"type": "number",  "description": "Must be > 0"},
                "x_setter":        {"type": "string",  "description": "X-axis setter name"},
                "x_start":         {"type": "number"},
                "x_end":           {"type": "number"},
                "x_step":          {"type": "number",  "description": "Must be > 0"},
                "y_setter":        {"type": "string",  "description": "Y-axis setter name"},
                "y_start":         {"type": "number"},
                "y_end":           {"type": "number"},
                "y_step":          {"type": "number",  "description": "Must be > 0"},
                "measurement":     {"type": "string",  "description": "Measurement function name"},
                "display_channel": {"type": "string",  "description": "'data1' … 'data6'"},
                "wait_ms":         {"type": "integer", "description": "Wait between points (ms)"},
                "z_reset_ms":      {"type": "integer", "description": "Extra wait after Z reset (ms)"},
                "align_enabled":   {"type": "boolean"},
                "align_every_n":   {"type": "integer"},
                "folder":          {"type": "string",  "description": "Save folder path"},
                "filename":        {"type": "string",  "description": "Base filename (no extension)"},
            },
            "required": [],
        },
    },
    {
        "name": "run_3d_scan",
        "description": (
            "Start the 3D scan with the current 3D Scan tab settings and wait for it "
            "to finish. Returns status and a PNG of the final image. "
            "Call set_3d_scan_settings first to configure the scan parameters."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "save_3d_scan",
        "description": (
            "Save the most recent 3D scan data to disk. "
            "Optionally supply a filename (no extension); if omitted the current "
            "filename field is used."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "filename": {"type": "string", "description": "Optional base filename (no extension)"},
            },
            "required": [],
        },
    },
    {
        "name": "get_log",
        "description": "Return the last N controller log entries.",
        "input_schema": {
            "type": "object",
            "properties": {
                "last_n": {"type": "integer", "description": "Number of log lines to return", "default": 20},
            },
            "required": [],
        },
    },
]


def dispatch(controller: MicroscopeController, tool_name: str, tool_input: dict,
             tabs: dict | None = None):
    match tool_name:
        case "get_state":
            return controller.get_state()

        case "list_axes":
            return {"axes": list(controller.AXIS_SETTERS.keys())}

        case "list_measurements":
            return {"measurements": list(controller.MEASUREMENT_FUNCS.keys())}

        case "run_1d_scan":
            result = controller.run_1d_scan_blocking(
                axis_key=tool_input["axis_key"],
                meas_key=tool_input["meas_key"],
                x_start=tool_input["x_start"],
                x_end=tool_input["x_end"],
                n_points=tool_input["n_points"],
                n_scans=tool_input.get("n_scans", 1),
                wait_ms=tool_input.get("wait_ms", 10),
            )
            if "error" in result:
                return result
            # Return as multipart: image + text stats
            return [
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/png",
                        "data": result["png_b64"],
                    },
                },
                {
                    "type": "text",
                    "text": json.dumps(result["stats"]),
                },
            ]

        case "run_2d_scan":
            x_start = tool_input["x_start"]
            x_end   = tool_input["x_end"]
            y_start = tool_input["y_start"]
            y_end   = tool_input["y_end"]

            # Prefer step-based specification; fall back to point count
            if "x_step" in tool_input and tool_input["x_step"]:
                x_step   = float(tool_input["x_step"])
                x_points = max(2, int(round(abs(x_end - x_start) / x_step)) + 1)
            else:
                x_points = tool_input.get("x_points", 20)
                x_step   = abs(x_end - x_start) / (x_points - 1) if x_points > 1 else 1.0

            if "y_step" in tool_input and tool_input["y_step"]:
                y_step   = float(tool_input["y_step"])
                y_points = max(2, int(round(abs(y_end - y_start) / y_step)) + 1)
            else:
                y_points = tool_input.get("y_points", 20)
                y_step   = abs(y_end - y_start) / (y_points - 1) if y_points > 1 else 1.0

            result = controller.run_2d_scan_blocking(
                x_key=tool_input["x_key"],
                y_key=tool_input["y_key"],
                meas_key=tool_input["meas_key"],
                x_start=x_start, x_end=x_end, x_points=x_points, x_step=x_step,
                y_start=y_start, y_end=y_end, y_points=y_points, y_step=y_step,
                n_scans=tool_input.get("n_scans", 1),
                wait_ms=tool_input.get("wait_ms", 5),
            )
            if "error" in result:
                return result
            return [
                {"type": "image",
                 "source": {"type": "base64", "media_type": "image/png",
                            "data": result["png_b64"]}},
                {"type": "text", "text": json.dumps(result["stats"])},
            ]

        case "set_scan_filename":
            scan_type = tool_input.get("scan_type", "1d")
            fname     = tool_input.get("filename", "")
            tab_key   = "1d_scan" if scan_type == "1d" else "2d_scan"
            tab       = (tabs or {}).get(tab_key)
            if tab is None:
                return {"error": f"{scan_type} scan tab not available"}
            tab.set_filename(fname)
            return {"status": "ok", "filename": fname, "scan_type": scan_type}

        case "save_1d_scan":
            tab = (tabs or {}).get("1d_scan")
            if tab is None:
                return {"error": "1D scan tab not available"}
            try:
                path = tab.save_data(tool_input.get("filename") or None)
                return {"status": "saved", "path": path}
            except RuntimeError as e:
                return {"error": str(e)}

        case "save_2d_scan":
            tab = (tabs or {}).get("2d_scan")
            if tab is None:
                return {"error": "2D scan tab not available"}
            try:
                path = tab.save_data(tool_input.get("filename") or None)
                return {"status": "saved", "path": path}
            except RuntimeError as e:
                return {"error": str(e)}

        case "get_alignment_settings":
            tab = (tabs or {}).get("alignment")
            if tab is None:
                return {"error": "Alignment tab not available"}
            return tab.get_settings()

        case "set_alignment_settings":
            tab = (tabs or {}).get("alignment")
            if tab is None:
                return {"error": "Alignment tab not available"}
            warnings = tab.apply_settings(tool_input)
            result = {"status": "ok", "applied": tool_input}
            if warnings:
                result["warnings"] = warnings
            return result

        case "run_alignment":
            tab = (tabs or {}).get("alignment")
            if tab is None:
                return {"error": "Alignment tab not available"}
            set_origin = bool(tool_input.get("set_origin", False))
            return tab.run_find_feature_blocking(set_origin=set_origin)

        case "get_focus_settings":
            tab = (tabs or {}).get("find_focus")
            if tab is None:
                return {"error": "Find Focus tab not available"}
            return tab.get_settings()

        case "set_focus_settings":
            tab = (tabs or {}).get("find_focus")
            if tab is None:
                return {"error": "Find Focus tab not available"}
            warnings = tab.apply_settings(tool_input)
            result = {"status": "ok", "applied": tool_input}
            if warnings:
                result["warnings"] = warnings
            return result

        case "run_find_focus":
            tab = (tabs or {}).get("find_focus")
            if tab is None:
                return {"error": "Find Focus tab not available"}
            return tab.run_find_focus_blocking()

        case "get_3d_scan_settings":
            tab = (tabs or {}).get("3d_scan")
            if tab is None:
                return {"error": "3D scan tab not available"}
            return tab.get_settings()

        case "set_3d_scan_settings":
            tab = (tabs or {}).get("3d_scan")
            if tab is None:
                return {"error": "3D scan tab not available"}
            warnings = tab.apply_settings(tool_input)
            result = {"status": "ok", "applied": tool_input}
            if warnings:
                result["warnings"] = warnings
            return result

        case "run_3d_scan":
            tab = (tabs or {}).get("3d_scan")
            if tab is None:
                return {"error": "3D scan tab not available"}
            s = tab.get_settings()
            try:
                result = controller.run_3d_scan_blocking(
                    z_key=s["z_setter"],
                    x_key=s["x_setter"],
                    y_key=s["y_setter"],
                    meas_key=s["measurement"],
                    z_start=float(s["z_start"]),
                    z_end=float(s["z_end"]),
                    z_step=float(s["z_step"]),
                    x_start=float(s["x_start"]),
                    x_end=float(s["x_end"]),
                    x_step=float(s["x_step"]),
                    y_start=float(s["y_start"]),
                    y_end=float(s["y_end"]),
                    y_step=float(s["y_step"]),
                    fast_axis=s["fast_axis"].split()[0],
                    wait_ms=int(s["wait_ms"]),
                    z_reset_ms=int(s["z_reset_ms"]),
                )
            except Exception as e:
                return {"error": str(e)}
            if "error" in result:
                return result
            return [
                {"type": "image",
                 "source": {"type": "base64", "media_type": "image/png",
                            "data": result["png_b64"]}},
                {"type": "text", "text": json.dumps(result["stats"])},
            ]

        case "save_3d_scan":
            tab = (tabs or {}).get("3d_scan")
            if tab is None:
                return {"error": "3D scan tab not available"}
            try:
                path = tab.save_data(tool_input.get("filename") or None)
                return {"status": "saved", "path": path}
            except RuntimeError as e:
                return {"error": str(e)}

        case "get_log":
            last_n = tool_input.get("last_n", 20)
            return {"log": controller.get_log(last_n)}

        case "list_instrument_controls":
            dyn = (tabs or {}).get("dynamic_controls")
            if dyn is None:
                return {"error": "Dynamic Controls tab not available"}
            return {"controls": dyn.list_controls()}

        case "list_instrument_buttons":
            dyn = (tabs or {}).get("dynamic_controls")
            if dyn is None:
                return {"error": "Dynamic Controls tab not available"}
            return {"buttons": dyn.list_buttons()}

        case "set_instrument_control":
            dyn = (tabs or {}).get("dynamic_controls")
            if dyn is None:
                return {"error": "Dynamic Controls tab not available"}
            try:
                dyn.set_control(tool_input["control"], float(tool_input["value"]))
                return {"status": "ok", "control": tool_input["control"],
                        "value": tool_input["value"]}
            except Exception as e:
                return {"error": str(e)}

        case "press_instrument_button":
            dyn = (tabs or {}).get("dynamic_controls")
            if dyn is None:
                return {"error": "Dynamic Controls tab not available"}
            try:
                dyn.press_button(tool_input["button"])
                return {"status": "ok", "button": tool_input["button"]}
            except Exception as e:
                return {"error": str(e)}

        case _:
            return {"error": f"Unknown tool: {tool_name}"}
