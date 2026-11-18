# helpers/temperature.py
"""Blocking temperature-setpoint helper for use inside scan worker threads.

Setting a Lake Shore setpoint returns immediately, long before the sample
actually reaches that temperature. A scan axis therefore cannot simply write
the setpoint and move on — it has to wait until the reading has settled, or
every point is taken at the wrong temperature.
"""
from __future__ import annotations
import time

# Defaults, overridable per-call or via MicroscopeState settings.
DEFAULT_TOLERANCE_K = 0.5     # |T - setpoint| must be within this
DEFAULT_SETTLE_S    = 10.0    # ...continuously for this long
DEFAULT_TIMEOUT_S   = 900.0   # give up after this long
POLL_S              = 1.0


def set_temperature_blocking(
    target,
    lakeshore,
    tolerance_k: float = DEFAULT_TOLERANCE_K,
    settle_s: float = DEFAULT_SETTLE_S,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    status_cb=None,
    stop_flag=None,
) -> float:
    """Set the control setpoint and block until the temperature settles there.

    Parameters
    ----------
    target      : float  -- desired setpoint in K
    lakeshore   : LakeShore331 instance
    tolerance_k : reading counts as "at temperature" within this band
    settle_s    : reading must stay in the band continuously this long
    timeout_s   : raise TimeoutError after this long without settling
    status_cb   : optional callable(str) for progress messages
    stop_flag   : optional list/ref whose [0] going truthy aborts the wait

    Returns
    -------
    The final measured temperature in K.
    """
    if lakeshore is None:
        raise RuntimeError("Lakeshore331 is not connected — cannot set temperature.")

    target      = float(target)
    tolerance_k = max(1e-6, float(tolerance_k))
    settle_s    = max(0.0, float(settle_s))
    timeout_s   = max(0.0, float(timeout_s))

    def _say(msg):
        if status_cb:
            try:
                status_cb(msg)
            except Exception:
                pass

    def _stopped():
        try:
            return bool(stop_flag[0]) if stop_flag is not None else False
        except Exception:
            return False

    lakeshore.set_setpoint(target)
    _say(f"Setpoint → {target:.3f} K; waiting to settle…")

    t_start  = time.perf_counter()
    in_band_since = None
    last_temp = float("nan")

    while True:
        if _stopped():
            raise RuntimeError("Temperature wait aborted by user.")

        try:
            last_temp = float(lakeshore.read_temperature("K"))
        except Exception as e:
            # A transient invalid reading shouldn't kill a long scan; keep polling
            # until the overall timeout expires.
            _say(f"Temperature read failed ({e}); retrying…")
            in_band_since = None
            time.sleep(POLL_S)
            if time.perf_counter() - t_start > timeout_s:
                raise TimeoutError(
                    f"Timed out after {timeout_s:.0f}s waiting for {target:.3f} K "
                    f"(last read failed: {e})"
                )
            continue

        if abs(last_temp - target) <= tolerance_k:
            if in_band_since is None:
                in_band_since = time.perf_counter()
            held = time.perf_counter() - in_band_since
            if held >= settle_s:
                _say(f"Settled at {last_temp:.3f} K (target {target:.3f} K).")
                return last_temp
            _say(f"{last_temp:.3f} K in band, holding {held:.0f}/{settle_s:.0f}s…")
        else:
            in_band_since = None
            _say(f"{last_temp:.3f} K → target {target:.3f} K…")

        if time.perf_counter() - t_start > timeout_s:
            raise TimeoutError(
                f"Timed out after {timeout_s:.0f}s waiting for {target:.3f} K "
                f"(last reading {last_temp:.3f} K)."
            )

        time.sleep(POLL_S)
