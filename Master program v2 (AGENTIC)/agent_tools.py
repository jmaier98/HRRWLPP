"""
Agent tool definitions for the microscope controller.

dispatch() returns either:
  - dict  → plain JSON result (non-scan tools)
  - list  → multipart content [image_block, text_block] (scan tools)
"""

import json
from controller import MicroscopeController
import procedure_loader

TOOLS = [
    {
        "name": "list_procedures",
        "description": (
            "List the available measurement setup procedures (saved as PDF documents). "
            "Returns each procedure's name and a one-line description. Call this whenever "
            "the user asks to run, set up, or follow a named procedure or standard setup "
            "(e.g. 'standard photocurrent map')."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_procedure",
        "description": (
            "Load a measurement setup procedure by name. Returns the full written "
            "instructions (numbered steps and standard settings) as text PLUS a render "
            "of each page of the document, so you can see the reference images it "
            "contains — for example what a GOOD versus BAD alignment or focus cut looks "
            "like. Read the steps, study the reference images, then carry out the "
            "procedure end-to-end using your other tools, comparing each result against "
            "the reference images to decide whether a step succeeded or must be retried. "
            "Call list_procedures first to get valid names."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string",
                         "description": "Procedure name (from list_procedures)"},
            },
            "required": ["name"],
        },
    },
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
        "description": (
            "List the measurement function names available for run_1d_scan. "
            "NOTE: the 2D, 3D and 4D Scan tabs have their own measurement lists — use "
            "get_2d_scan_settings / get_3d_scan_settings / get_4d_scan_settings to "
            "discover those (they include options like 'PC and Offset' that are not in "
            "the 1D list)."
        ),
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
                "interleave": {"type": "boolean", "description": "Measure in interleaved order: every other point on the way out, then the skipped ones on the way back (-3,-1,1,3,2,0,-2 for -3..3 step 1). Gives a coarse pass over the full range first and leaves the axis near its start. Returned data stays in axis order.", "default": False},
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
                "pre_wait_ms":  {"type": "integer", "description": "Wait before alignment (ms): after moving to the first point of the horizontal cut, wait this long before scanning so the lock-in can settle (e.g. after a shutter opens or the power changes). Default 0."},
                "h_start_x":    {"type": "number",  "description": "Horizontal scan start X"},
                "h_start_y":    {"type": "number",  "description": "Horizontal scan start Y"},
                "h_distance":   {"type": "number",  "description": "Horizontal scan distance"},
                "h_n_points":   {"type": "integer"},
                "h_left_right": {"type": "boolean", "description": "True = left→right"},
                "h_fit_model":  {"type": "string",  "description": "'sigmoid', 'double_sigmoid', or 'template' "
                                                      "(the shape recorded when the origin was set)"},
                "v_start_x":    {"type": "number",  "description": "Vertical scan start X"},
                "v_start_y":    {"type": "number",  "description": "Vertical scan start Y"},
                "v_distance":   {"type": "number",  "description": "Vertical scan distance"},
                "v_n_points":   {"type": "integer"},
                "v_bottom_top": {"type": "boolean", "description": "True = bottom→top"},
                "track_feature": {"type": "boolean", "description":
                                  "Start each cut at its entered start point shifted by the "
                                  "offset from the last alignment, so a drifting feature stays "
                                  "in the scanned window (ignored when setting the origin)"},
                "move_to_target": {"type": "boolean", "description":
                                   "Return to target after alignment: the last step of every "
                                   "alignment (including set-origin and alignments during scans) "
                                   "moves the galvo to (target_x, target_y) plus the measured offset"},
                "target_x":     {"type": "number",  "description": "Target pos X (galvo units) used by move_to_target"},
                "target_y":     {"type": "number",  "description": "Target pos Y (galvo units) used by move_to_target"},
                "v_fit_model":  {"type": "string",  "description": "'sigmoid', 'double_sigmoid', or 'template' "
                                                      "(the shape recorded when the origin was set)"},
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
        "name": "get_webcam_settings",
        "description": (
            "Return the current state of the Webcam tab: whether the camera/joystick "
            "are running, LED brightness, galvo X/Y, delay-stage position and speed, "
            "the selected cryo-jog step size, and shutter states. Also lists the valid "
            "jog step options and the galvo limit."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "set_webcam_settings",
        "description": (
            "Set any subset of Webcam tab controls. Supplying galvo_x/galvo_y moves the "
            "galvo; stage_position_mm moves the delay stage; stage_speed_mm_s sets its "
            "speed; led_brightness sets the LED; jog_step selects the cryo-jog step size; "
            "pump/probe_shutter_open open or close shutters; camera_on starts/stops the "
            "live camera + joystick view. All fields optional."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "led_brightness":     {"type": "integer", "description": "0-255"},
                "galvo_x":            {"type": "number",  "description": "Galvo X (milliunits)"},
                "galvo_y":            {"type": "number",  "description": "Galvo Y (milliunits)"},
                "stage_position_mm":  {"type": "number"},
                "stage_speed_mm_s":   {"type": "number"},
                "jog_step":           {"type": "string",  "description": "rough | medium | fine"},
                "pump_shutter_open":  {"type": "boolean"},
                "probe_shutter_open": {"type": "boolean"},
                "camera_on":          {"type": "boolean", "description": "Start/stop live camera + joystick"},
            },
            "required": [],
        },
    },
    {
        "name": "webcam_jog",
        "description": (
            "Jog the cryo stage by one (or more) discrete steps from the Webcam tab, "
            "using the currently selected step size (or set it via the 'step' field). "
            "axis is x, y, or z; direction is '+' or '-'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "axis":      {"type": "string", "description": "x, y, or z"},
                "direction": {"type": "string", "description": "'+' or '-'"},
                "count":     {"type": "integer", "description": "Number of steps", "default": 1},
                "step":      {"type": "string", "description": "Optional: rough | medium | fine"},
            },
            "required": ["axis", "direction"],
        },
    },
    {
        "name": "capture_webcam_frame",
        "description": (
            "Capture the current frame from the sample webcam and return it as an image "
            "so you can see the sample. Works whether or not the live view is running "
            "(it opens the camera briefly if needed). Use this to visually check the "
            "sample, find features, or confirm alignment."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_2d_scan_settings",
        "description": (
            "Return the current settings of the 2D Scan tab: axis setters, ranges, "
            "measurement function, displayed data channel, averaging, alignment, and "
            "save folder/filename. Also lists ALL available setter names, measurement "
            "names (use this to discover valid 2D measurements like 'PC and Offset'), "
            "and display channels."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "set_2d_scan_settings",
        "description": (
            "Configure any subset of the 2D Scan tab settings. Includes 'display_channel' "
            "to choose which measured channel (data1…data6) is shown in the live image, "
            "and 'show_average' to display the running average across repeats. Call "
            "get_2d_scan_settings first to discover valid axis/measurement names. "
            "All fields optional."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "fast_axis":       {"type": "string",  "description": "'X fast' or 'Y fast'"},
                "x_setter":        {"type": "string",  "description": "X-axis setter name"},
                "x_start":         {"type": "number"},
                "x_end":           {"type": "number"},
                "x_step":          {"type": "number",  "description": "Must be > 0"},
                "y_setter":        {"type": "string",  "description": "Y-axis setter name"},
                "y_start":         {"type": "number"},
                "y_end":           {"type": "number"},
                "y_step":          {"type": "number",  "description": "Must be > 0"},
                "measurement":     {"type": "string",  "description": "Measurement function name"},
                "display_channel": {"type": "string",  "description": "'data1' … 'data6' — which channel to display"},
                "show_average":    {"type": "boolean", "description": "Display running average across repeats"},
                "wait_ms":         {"type": "integer", "description": "Wait between points (ms)"},
                "averages":        {"type": "integer", "description": "Number of full-scan repeats"},
                "align_enabled":   {"type": "boolean"},
                "align_every_n":   {"type": "integer"},
                "folder":          {"type": "string",  "description": "Save folder path"},
                "filename":        {"type": "string",  "description": "Base filename (no extension)"},
            },
            "required": [],
        },
    },
    {
        "name": "set_2d_display_channel",
        "description": (
            "Choose which measured data channel (data1…data6) is shown in the 2D Scan "
            "tab's live image, and redraw it immediately. Shortcut for the display "
            "control in set_2d_scan_settings."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "channel": {"type": "string", "description": "'data1' … 'data6'"},
            },
            "required": ["channel"],
        },
    },
    {
        "name": "get_3d_scan_settings",
        "description": (
            "Return the current settings of the 3D Scan tab: axis setters, ranges, "
            "measurement function, displayed data channel, wait times, alignment, and "
            "save folder/filename. Also lists ALL available setter and measurement "
            "names (use this to discover valid 3D measurements like 'PC and Offset')."
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
        "name": "get_4d_scan_settings",
        "description": (
            "Return the current settings of the 4D Scan tab. A 4D scan is a spatial map "
            "(outer X/Y axes) where a full inner 2D scan over two other parameters "
            "(axes A and B — e.g. delay time and a rotation mount) is taken at every "
            "spatial pixel. Also lists ALL available setter, measurement and map-reduction "
            "names — call this before set_4d_scan_settings."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "set_4d_scan_settings",
        "description": (
            "Configure any subset of the 4D Scan tab settings before running a scan. "
            "X/Y are the spatial (outer) axes; A/B are the inner 2D scan axes taken at "
            "every spatial pixel. Call get_4d_scan_settings first to discover valid "
            "axis/measurement names. All fields are optional."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "spatial_fast_axis": {"type": "string",  "description": "'X fast' or 'Y fast'"},
                "x_setter":          {"type": "string",  "description": "Spatial X setter name"},
                "x_start":           {"type": "number"},
                "x_end":             {"type": "number"},
                "x_step":            {"type": "number",  "description": "Must be > 0"},
                "y_setter":          {"type": "string",  "description": "Spatial Y setter name"},
                "y_start":           {"type": "number"},
                "y_end":             {"type": "number"},
                "y_step":            {"type": "number",  "description": "Must be > 0"},
                "inner_fast_axis":   {"type": "string",  "description": "'A fast' or 'B fast'"},
                "a_setter":          {"type": "string",  "description": "Inner axis A setter name"},
                "a_start":           {"type": "number"},
                "a_end":             {"type": "number"},
                "a_step":            {"type": "number",  "description": "Must be > 0"},
                "interleave_a":      {"type": "boolean", "description": "Measure axis A in interleaved order: every other point on the way out, then the skipped ones on the way back (-3,-1,1,3,2,0,-2 for -3..3 step 1). Gives a coarse pass over the full range first and leaves the axis near its start. Storage and plots stay in axis order."},
                "b_setter":          {"type": "string",  "description": "Inner axis B setter name"},
                "b_start":           {"type": "number"},
                "b_end":             {"type": "number"},
                "b_step":            {"type": "number",  "description": "Must be > 0"},
                "interleave_b":      {"type": "boolean", "description": "Same interleaved measurement order for axis B. A and B can be interleaved independently."},
                "measurement":       {"type": "string",  "description": "Measurement function name"},
                "display_channel":   {"type": "string",  "description": "'data1' … 'data6'"},
                "map_reduction":     {"type": "string",  "description": "How each pixel's inner 2D scan is reduced to one number on the spatial map: 'mean', 'max', 'min', 'peak-to-peak', 'std', or 'at (A idx, B idx)'"},
                "pick_a_index":      {"type": "integer", "description": "A index used by the 'at (A idx, B idx)' reduction"},
                "pick_b_index":      {"type": "integer", "description": "B index used by the 'at (A idx, B idx)' reduction"},
                "wait_ms":           {"type": "integer", "description": "Wait between inner points (ms)"},
                "inner_reset_ms":    {"type": "integer", "description": "Extra wait after the inner axes reset at each new spatial pixel (ms)"},
                "averages":          {"type": "integer", "description": "Number of times the whole 4D scan is repeated for averaging (default 1)"},
                "show_average":      {"type": "boolean", "description": "Display the average of the repeats run so far instead of the current one"},
                "align_enabled":     {"type": "boolean"},
                "align_every_n":     {"type": "integer", "description": "Re-align every N units, counted per align_basis"},
                "align_basis":       {"type": "string",  "description": "What align_every_n counts: 'spatial pixels' (outer X,Y loop, default) or 'inner points' (inner A,B points, counted across the whole scan, so alignment can happen mid-pixel)"},
                "folder":            {"type": "string",  "description": "Save folder path"},
                "filename":          {"type": "string",  "description": "Base filename (no extension)"},
            },
            "required": [],
        },
    },
    {
        "name": "run_4d_scan",
        "description": (
            "Start the 4D scan with the current 4D Scan tab settings and wait for it to "
            "finish. Returns statistics and a PNG of the spatial map (each pixel is the "
            "mean of its inner 2D scan, averaged over the repeats). This can take a long "
            "time — the total number of points is Nx*Ny*Na*Nb*averages. "
            "Call set_4d_scan_settings first."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "save_4d_scan",
        "description": (
            "Save the most recent 4D scan to disk (.png screenshot, .txt table and a "
            ".npz file holding the raw 4D cube). Optionally supply a filename "
            "(no extension); if omitted the current filename field is used."
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
        "name": "get_gate_settings",
        "description": (
            "Return the current state of the Gate Breakdown tab: the Keithley's "
            "present gate voltage and output state, current compliance, the sweep "
            "parameters (start/end/step V, dwell, abort current), the voltage soft "
            "and hard limits, and the breakdown voltage found by the last sweep."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "set_gate_settings",
        "description": (
            "Configure any subset of the Gate Breakdown tab settings before a sweep. "
            "compliance_a is pushed to the Keithley immediately (hardware protection); "
            "abort_current_a is the leakage current at which the sweep declares "
            "breakdown and stops. All fields are optional."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "v_start":         {"type": "number",  "description": "Sweep start voltage (V)"},
                "v_end":           {"type": "number",  "description": "Sweep end voltage (V)"},
                "v_step":          {"type": "number",  "description": "Step size (V), must be > 0"},
                "dwell_ms":        {"type": "integer", "description": "Settle time per step (ms)"},
                "abort_current_a": {"type": "number",  "description": "Stop the sweep when |I| reaches this (A)"},
                "ramp_down_v_per_s": {"type": "number", "description": "Rate the gate is walked back to 0 V at when the sweep ends, breaks down or is stopped (V/s)"},
                "compliance_a":    {"type": "number",  "description": "Keithley current compliance (A)"},
                "output_on":       {"type": "boolean", "description": "Turn the Keithley output on/off"},
                "log_scale":       {"type": "boolean", "description": "Plot |I| on a log axis"},
                "folder":          {"type": "string",  "description": "Save folder path"},
                "filename":        {"type": "string",  "description": "Base filename (no extension)"},
            },
            "required": [],
        },
    },
    {
        "name": "set_gate_voltage",
        "description": (
            "Ramp the gate (Keithley 2400) to a voltage in small steps and wait until "
            "it is there. Uses the global ramp parameters keithley_ramp_step_v and "
            "keithley_ramp_dwell_s from the microscope state. "
            "Never jumps the gate — always ramps."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "volts": {"type": "number", "description": "Target gate voltage (V)"},
            },
            "required": ["volts"],
        },
    },
    {
        "name": "run_gate_sweep",
        "description": (
            "Run a gate breakdown sweep with the current Gate Breakdown tab settings "
            "and wait for it to finish. Ramps to the start voltage, steps to the end, "
            "and stops early if the leakage current crosses the abort threshold or the "
            "source hits compliance. The gate is ALWAYS ramped back to 0 V afterwards "
            "at the tab's ramp-down rate, including on an abort. Returns an I-V plot "
            "plus the breakdown voltage "
            "(null if none was reached). Call set_gate_settings first to configure it."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "save_gate_sweep",
        "description": (
            "Save the most recent gate breakdown sweep to disk (.txt table + .png plot). "
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
        case "list_procedures":
            return {"procedures": procedure_loader.list_procedures()}

        case "get_procedure":
            try:
                images_b64, text = procedure_loader.load_procedure(tool_input["name"])
            except FileNotFoundError as e:
                return {"error": str(e)}
            except Exception as e:
                return {"error": f"Failed to load procedure: {e}"}
            blocks = [
                {"type": "image",
                 "source": {"type": "base64", "media_type": "image/png", "data": b}}
                for b in images_b64
            ]
            blocks.append({"type": "text", "text": text})
            return blocks

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
                interleave=bool(tool_input.get("interleave", False)),
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

            # Resolve the measurement from the 2D tab's own functions so the agent
            # can use every option the 2D Scan tab offers (e.g. 'PC and Offset').
            meas_key = tool_input["meas_key"]
            two_d    = (tabs or {}).get("2d_scan")
            measure_fn = None
            if two_d is not None:
                measure_fn = two_d.MEASUREMENT_FUNCS.get(meas_key)
                if measure_fn is None:
                    return {"error": f"Unknown measurement '{meas_key}'. "
                                     f"Available: {list(two_d.MEASUREMENT_FUNCS.keys())}"}

            result = controller.run_2d_scan_blocking(
                x_key=tool_input["x_key"],
                y_key=tool_input["y_key"],
                meas_key=meas_key,
                x_start=x_start, x_end=x_end, x_points=x_points, x_step=x_step,
                y_start=y_start, y_end=y_end, y_points=y_points, y_step=y_step,
                n_scans=tool_input.get("n_scans", 1),
                wait_ms=tool_input.get("wait_ms", 5),
                measure_fn=measure_fn,
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

        case "get_webcam_settings":
            tab = (tabs or {}).get("webcam")
            if tab is None:
                return {"error": "Webcam tab not available"}
            return tab.get_settings()

        case "set_webcam_settings":
            tab = (tabs or {}).get("webcam")
            if tab is None:
                return {"error": "Webcam tab not available"}
            warnings = tab.apply_settings(tool_input)
            result = {"status": "ok", "applied": tool_input}
            if warnings:
                result["warnings"] = warnings
            return result

        case "webcam_jog":
            tab = (tabs or {}).get("webcam")
            if tab is None:
                return {"error": "Webcam tab not available"}
            return tab.jog(
                axis=tool_input["axis"],
                direction=tool_input["direction"],
                count=tool_input.get("count", 1),
                step=tool_input.get("step"),
            )

        case "capture_webcam_frame":
            tab = (tabs or {}).get("webcam")
            if tab is None:
                return {"error": "Webcam tab not available"}
            b64 = tab.capture_frame_b64()
            if b64 is None:
                return {"error": "Could not capture a frame (no camera?)"}
            return [
                {"type": "image",
                 "source": {"type": "base64", "media_type": "image/png", "data": b64}},
                {"type": "text", "text": "Webcam frame captured."},
            ]

        case "get_2d_scan_settings":
            tab = (tabs or {}).get("2d_scan")
            if tab is None:
                return {"error": "2D scan tab not available"}
            return tab.get_settings()

        case "set_2d_scan_settings":
            tab = (tabs or {}).get("2d_scan")
            if tab is None:
                return {"error": "2D scan tab not available"}
            warnings = tab.apply_settings(tool_input)
            result = {"status": "ok", "applied": tool_input}
            if warnings:
                result["warnings"] = warnings
            return result

        case "set_2d_display_channel":
            tab = (tabs or {}).get("2d_scan")
            if tab is None:
                return {"error": "2D scan tab not available"}
            return tab.set_display_channel(tool_input["channel"])

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
            # Use the 3D tab's own measurement function so every option the 3D
            # Scan tab offers (e.g. 'PC and Offset') is usable by the agent.
            measure_fn = tab.MEASUREMENT_FUNCS.get(s["measurement"])
            if measure_fn is None:
                return {"error": f"Unknown measurement '{s['measurement']}'. "
                                 f"Available: {list(tab.MEASUREMENT_FUNCS.keys())}"}
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
                    measure_fn=measure_fn,
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

        case "get_4d_scan_settings":
            tab = (tabs or {}).get("4d_scan")
            if tab is None:
                return {"error": "4D scan tab not available"}
            return tab.get_settings()

        case "set_4d_scan_settings":
            tab = (tabs or {}).get("4d_scan")
            if tab is None:
                return {"error": "4D scan tab not available"}
            warnings = tab.apply_settings(tool_input)
            result = {"status": "ok", "applied": tool_input}
            if warnings:
                result["warnings"] = warnings
            return result

        case "run_4d_scan":
            tab = (tabs or {}).get("4d_scan")
            if tab is None:
                return {"error": "4D scan tab not available"}
            # The 4D tab owns the scan engine (it needs four axes plus the inner
            # raster), so it runs the scan in this thread and plots live via signals.
            try:
                result = tab.run_scan_blocking()
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

        case "save_4d_scan":
            tab = (tabs or {}).get("4d_scan")
            if tab is None:
                return {"error": "4D scan tab not available"}
            try:
                path = tab.save_data(tool_input.get("filename") or None)
                return {"status": "saved", "path": path}
            except RuntimeError as e:
                return {"error": str(e)}

        case "get_gate_settings":
            tab = (tabs or {}).get("gate_breakdown")
            if tab is None:
                return {"error": "Gate Breakdown tab not available"}
            return tab.get_settings()

        case "set_gate_settings":
            tab = (tabs or {}).get("gate_breakdown")
            if tab is None:
                return {"error": "Gate Breakdown tab not available"}
            warnings = tab.apply_settings(tool_input)
            result = {"status": "ok", "applied": tool_input}
            if warnings:
                result["warnings"] = warnings
            return result

        case "set_gate_voltage":
            tab = (tabs or {}).get("gate_breakdown")
            if tab is None:
                return {"error": "Gate Breakdown tab not available"}
            return tab.set_gate_voltage_blocking(float(tool_input["volts"]))

        case "run_gate_sweep":
            tab = (tabs or {}).get("gate_breakdown")
            if tab is None:
                return {"error": "Gate Breakdown tab not available"}
            try:
                result = tab.run_sweep_blocking()
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

        case "save_gate_sweep":
            tab = (tabs or {}).get("gate_breakdown")
            if tab is None:
                return {"error": "Gate Breakdown tab not available"}
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
