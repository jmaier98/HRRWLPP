"""Instrument-state block for the header of saved scan data files.

Every scan tab writes the same block so that data taken on different days can
be compared parameter-by-parameter without opening the state file.
"""


def instrument_state_lines(controller, indent="  ") -> list[str]:
    """Return header lines describing the current instrument state.

    Never raises: a missing controller/state must not lose a scan on save.
    """
    lines = ["Instrument state:"]
    try:
        settings = dict(getattr(controller, "state").settings)
    except Exception as e:
        lines.append(f"{indent}<unavailable: {e}>")
        return lines

    if not settings:
        lines.append(f"{indent}<empty>")
        return lines

    width = max(len(k) for k in settings)
    for key in sorted(settings):
        lines.append(f"{indent}{key:<{width}} : {_fmt_value(settings[key])}")
    return lines


def _fmt_value(val, max_len: int = 120) -> str:
    """Scalars verbatim; bulky containers (e.g. alignment templates) summarised.

    A recorded feature shape is a few hundred numbers — printing it would bury
    the parameters this header exists to show.
    """
    if isinstance(val, (list, tuple)):
        text = str(val)
        return text if len(text) <= max_len else f"<{type(val).__name__}, {len(val)} items>"
    if isinstance(val, dict):
        text = str(val)
        return text if len(text) <= max_len else f"<dict, keys: {', '.join(map(str, val))}>"
    return str(val)


def instrument_state_block(controller, indent="  ") -> str:
    return "\n".join(instrument_state_lines(controller, indent))
