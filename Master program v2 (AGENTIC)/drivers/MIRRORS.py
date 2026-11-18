"""Motorized mirror mounts on the BTT (Marlin) controller on COM5.

Mirror 1 — X and Y motors. Sets the x/y offset (exposed as a scan axis).
Mirror 2 — Z and E motors. Used for beam walk during alignment.

All positions are tracked in MICROSTEPS so every command resolves to whole
microsteps on the motor. Marlin takes moves in mm, so each axis' steps/mm is
read from the firmware at startup (M92) and used to convert. Positions are
persisted to the state file, because the controller forgets them on reset.

Microstepping on TMC (UART) drivers is set in the firmware (X_MICROSTEPS etc.
in Configuration_adv.h, usually 16 with 256x interpolation); the value the
firmware reports is printed at startup. The motors are kept energised
permanently (M84 S0): a stepper that is released snaps to its nearest full
step, which would throw away any fine adjustment.
"""
import re
import threading
import time

import serial

PORT = 'COM5'
BAUD = 115200
AXES = ("X", "Y", "Z", "E")

# State-file key for each motor's position (microsteps).
STATE_KEYS = {
    "X": "mirror_1_x_steps",
    "Y": "mirror_1_y_steps",
    "Z": "mirror_2_x_steps",
    "E": "mirror_2_y_steps",
}

# Soft travel limit per motor, in microsteps from the zero position. Keeps the
# adjuster screws from being driven off their ends. Tune to the mounts.
MIRROR_LIMIT_STEPS = 32000

# Fallback steps/mm if the firmware does not report them. Set this to a dict
# to override the firmware values, e.g. {"X": 80, "Y": 80, "Z": 400, "E": 93}.
MIRROR_STEPS_PER_MM = None
_DEFAULT_STEPS_PER_MM = {"X": 80.0, "Y": 80.0, "Z": 400.0, "E": 93.0}

# Speed used for absolute (scan / Go) moves, microsteps per second.
GOTO_SPEED_STEPS_S = 400.0

# Firmware speed/acceleration limits, applied identically to all four motors
# at startup. Marlin's defaults cap Z (and E) far below X/Y, and a move that
# includes Z/E is slowed to that axis' limit.
MAX_SPEED_STEPS_S  = 6000.0
MAX_ACCEL_STEPS_S2 = 60000.0


class Mirrors:
    def __init__(self, state, port=PORT):
        self.state = state
        self._lock = threading.Lock()
        self.d = serial.Serial(port, BAUD, timeout=1)
        time.sleep(2.0)                     # board resets when the port opens
        self.d.reset_input_buffer()

        self.steps_per_mm = self._read_steps_per_mm()
        self.microsteps = self._read_microsteps()

        self.pos = {ax: int(round(float(state.settings.get(STATE_KEYS[ax], 0))))
                    for ax in AXES}

        self._send('M302 S0')               # E moves without a hot end
        self._send('M211 S0')               # no software endstops (never homed)
        self._send('M84 S0')                # never idle-release the motors
        self._send('M17')                   # energise all motors
        self._send('G90')                   # absolute XYZ
        self._send('M82')                   # absolute E
        # Same speed/acceleration on every motor (converted to each axis' mm).
        spd = {ax: MAX_SPEED_STEPS_S / self.steps_per_mm[ax] for ax in AXES}
        acc = {ax: MAX_ACCEL_STEPS_S2 / self.steps_per_mm[ax] for ax in AXES}
        self._send('M203 ' + ' '.join(f'{ax}{spd[ax]:.4f}' for ax in AXES))
        self._send('M201 ' + ' '.join(f'{ax}{acc[ax]:.4f}' for ax in AXES))
        a = max(acc.values())               # per-axis M201 limits still apply
        self._send(f'M204 P{a:.4f} R{a:.4f} T{a:.4f}')
        # Restore the firmware position to the persisted one.
        self._send('G92 ' + ' '.join(f'{ax}{self._mm(ax, self.pos[ax]):.6f}' for ax in AXES))
        print(f"Mirror controller online ({port}) — steps/mm {self.steps_per_mm}, "
              f"microsteps {self.microsteps or 'unknown'}")

    # ── Serial helpers ────────────────────────────────────────────────────────

    def _send(self, cmd, wait=0.02):
        with self._lock:
            self.d.write((cmd + '\n').encode())
        time.sleep(wait)

    def _query(self, *cmds, timeout=3.0) -> list[str]:
        """Send one or more commands and wait for an 'ok' to each.

        Returns the non-'ok' reply lines. Raises TimeoutError if the board does
        not answer every command in time.
        """
        lines = []
        with self._lock:
            self.d.reset_input_buffer()
            self.d.write(''.join(c + '\n' for c in cmds).encode())
            oks = 0
            t_end = time.time() + timeout
            while oks < len(cmds):
                if time.time() > t_end:
                    raise TimeoutError(f"mirror controller did not answer {cmds}")
                raw = self.d.readline()
                if not raw:
                    continue
                line = raw.decode(errors='ignore').strip()
                if line.startswith('ok'):
                    oks += 1
                else:
                    lines.append(line)
        return lines

    def _read_steps_per_mm(self) -> dict:
        if MIRROR_STEPS_PER_MM:
            return {ax: float(MIRROR_STEPS_PER_MM[ax]) for ax in AXES}
        for cmd in ('M92', 'M503'):
            try:
                reply = self._query(cmd)
            except TimeoutError:
                continue
            for line in reply:
                if 'M92' in line:
                    found = dict(re.findall(r'([XYZE])\s*(-?\d+(?:\.\d+)?)', line.split('M92', 1)[1]))
                    if all(ax in found for ax in AXES):
                        return {ax: float(found[ax]) for ax in AXES}
        print("Warning: mirror controller did not report steps/mm — using defaults; "
              "microstep sizes may be wrong.")
        return dict(_DEFAULT_STEPS_PER_MM)

    def _read_microsteps(self):
        """Best-effort read of the TMC microstep setting (M122). None if unknown."""
        try:
            for line in self._query('M122', timeout=3.0):
                if line.lower().startswith('microsteps'):
                    return line.split(None, 1)[1].split()
        except Exception:
            pass
        return None

    def _mm(self, axis, steps):
        return steps / self.steps_per_mm[axis]

    def _clamp(self, steps):
        return max(-MIRROR_LIMIT_STEPS, min(MIRROR_LIMIT_STEPS, int(round(steps))))

    # ── Motion ────────────────────────────────────────────────────────────────

    def move_to(self, targets: dict, duration=None, wait=False):
        """Move motors to absolute microstep positions.

        duration: how long the move should take (s); defaults to
        GOTO_SPEED_STEPS_S on the longest axis. wait: block until the motion
        has physically finished (M400) — use this for scans.
        """
        new = {ax: self._clamp(v) for ax, v in targets.items()}
        delta = {ax: new[ax] - self.pos[ax] for ax in new if new[ax] != self.pos[ax]}
        if not delta:
            return
        if duration is None:
            duration = max(abs(d) for d in delta.values()) / GOTO_SPEED_STEPS_S
        duration = max(duration, 1e-3)

        # Marlin's F is the XYZ path speed; E follows it. With no XYZ motion
        # F applies to E alone. Either way every axis finishes in `duration`.
        xyz_mm = sum(self._mm(ax, d) ** 2 for ax, d in delta.items() if ax != 'E') ** 0.5
        path_mm = xyz_mm if xyz_mm > 0 else abs(self._mm('E', delta['E']))
        feed = max(path_mm / duration * 60.0, 0.01)

        words = ' '.join(f'{ax}{self._mm(ax, new[ax]):.6f}' for ax in delta)
        move = f'G1 {words} F{feed:.4f}'
        self.pos.update(new)               # once sent, the board will get there
        if wait:
            # Marlin answers M400 only once all motion has finished, so the
            # second 'ok' confirms the motors have reached the target.
            self._query(move, 'M400', timeout=duration * 2 + 2)
        else:
            self._send(move, wait=0)

    def move_by(self, deltas: dict, duration=None, wait=False):
        """Relative move in microsteps."""
        self.move_to({ax: self.pos[ax] + d for ax, d in deltas.items()},
                     duration=duration, wait=wait)

    # Scan-axis setters: absolute microsteps, block until settled, persist.
    def mirror1_x(self, steps):
        self.move_to({"X": steps}, wait=True)
        self.save_positions()

    def mirror1_y(self, steps):
        self.move_to({"Y": steps}, wait=True)
        self.save_positions()

    def zero(self):
        """Call the current position zero on all four motors."""
        self._query('M400')
        for ax in AXES:
            self.pos[ax] = 0
        self._send('G92 X0 Y0 Z0 E0')
        self.save_positions()

    def save_positions(self):
        for ax in AXES:
            self.state.settings[STATE_KEYS[ax]] = int(self.pos[ax])
        try:
            self.state.save()
        except Exception as e:
            print(f"Warning: could not persist mirror positions: {e}")

    def close(self):
        try:
            self.save_positions()
        finally:
            self.d.close()
