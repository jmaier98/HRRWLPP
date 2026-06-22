"""
LocalAgentWorker — runs the Ollama agent loop in a QThread so the GUI stays live.

Drop-in replacement for AgentWorker; emits identical signals.

Signals
-------
agent_message  : str            raw reasoning text (full log)
tool_called    : (str, str)     (tool_name, compact_result)  (full log)
turn_summary   : (int, str)     (turn_number, summary_text)  → chat box
finished       : str            final conclusion
error          : str            error message
"""

import json
import requests
from PyQt6.QtCore import QThread, pyqtSignal

from controller import ScannerController
from agent_tools import TOOLS, dispatch

# ── Ollama config ──────────────────────────────────────────────
OLLAMA_IP    = "10.82.5.167"
OLLAMA_PORT  = 11434
OLLAMA_MODEL = "gemma4:26b"
BASE_URL     = f"http://{OLLAMA_IP}:{OLLAMA_PORT}"


def _to_ollama_tools(anthropic_tools: list) -> list:
    return [
        {
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t["description"],
                "parameters": t["input_schema"],
            },
        }
        for t in anthropic_tools
    ]


OLLAMA_TOOLS = _to_ollama_tools(TOOLS)


class LocalAgentWorker(QThread):
    agent_message = pyqtSignal(str)
    tool_called   = pyqtSignal(str, str)
    turn_summary  = pyqtSignal(int, str)
    finished      = pyqtSignal(str)
    error         = pyqtSignal(str)

    def __init__(self, controller: ScannerController, task: str,
                 context: str = "", parent=None):
        super().__init__(parent)
        self.controller = controller
        self.task = task
        self.context = context.strip()
        self._stop_requested = False

    def stop(self):
        self._stop_requested = True

    # ── Main loop ─────────────────────────────────────────────

    def run(self):
        context_block = (
            f"\n\nOPERATOR CONTEXT & INSTRUMENT NORMS\n{self.context}"
            if self.context else ""
        )
        system_prompt = (
            "You are an AI agent controlling a 2D scanning instrument. "
            "The scanner has a motorized X-Y stage (range 0–100 mm each) and a "
            "detector that measures signal intensity at the current position.\n\n"
            "The sample contains several features (peaks) at unknown locations. "
            "Use the available tools to operate the scanner and accomplish the task. "
            "Think step by step. After every set of tool calls include a short "
            "(1–3 sentence) plain-English summary of what you just did and found. "
            "This summary is shown to the operator watching the instrument in real time."
            + context_block
        )

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user",   "content": self.task},
        ]
        max_turns = 20

        for turn_num in range(1, max_turns + 1):
            if self._stop_requested:
                self.finished.emit("Agent stopped by user.")
                return

            response = self._ollama_chat(messages)
            if response is None:
                return

            msg        = response.get("message", {})
            text       = (msg.get("content") or "").strip()
            tool_calls = msg.get("tool_calls") or []

            if text:
                self.agent_message.emit(text)

            # No tool calls → model is done
            if not tool_calls:
                conclusion = text if text else "Task complete."
                self.turn_summary.emit(turn_num, conclusion)
                self.finished.emit(conclusion)
                return

            # Record assistant turn
            assistant_record = {"role": "assistant", "content": text}
            if tool_calls:
                assistant_record["tool_calls"] = tool_calls
            messages.append(assistant_record)

            # ── Execute tool calls ─────────────────────────────
            tool_result_msgs = []
            pending_images   = []
            tool_names_called = []

            for tc in tool_calls:
                if self._stop_requested:
                    self.finished.emit("Agent stopped by user.")
                    return

                fn   = tc.get("function", {})
                name = fn.get("name", "")
                raw  = fn.get("arguments", {})
                args = raw if isinstance(raw, dict) else json.loads(raw)

                try:
                    result = dispatch(self.controller, name, args)
                except Exception as e:
                    result = {"error": str(e)}

                if isinstance(result, list):
                    text_part = next(
                        (b["text"] for b in result if b.get("type") == "text"), ""
                    )
                    for block in result:
                        if block.get("type") == "image":
                            pending_images.append(block["source"]["data"])
                    compact = text_part[:160] + ("…" if len(text_part) > 160 else "")
                    content = text_part
                else:
                    compact = json.dumps(result)
                    if len(compact) > 160:
                        compact = compact[:160] + "…"
                    content = json.dumps(result)

                self.tool_called.emit(name, compact)
                tool_names_called.append(name)
                tool_result_msgs.append({"role": "tool", "content": content})

            messages.extend(tool_result_msgs)

            # Attach scan images as a vision user message so the model can see them
            if pending_images:
                messages.append({
                    "role":    "user",
                    "content": (
                        f"Scan complete — {len(pending_images)} image(s) attached. "
                        "Use the image(s) and the JSON summary above to decide your next step."
                    ),
                    "images": pending_images,
                })

            # ── Emit turn summary ──────────────────────────────
            summary = text if text else f"Called: {', '.join(tool_names_called)}."
            self.turn_summary.emit(turn_num, summary)

        self.finished.emit("Reached max turns without completing the task.")

    # ── HTTP helper ───────────────────────────────────────────

    def _ollama_chat(self, messages: list) -> dict | None:
        try:
            r = requests.post(
                f"{BASE_URL}/api/chat",
                json={
                    "model":   OLLAMA_MODEL,
                    "messages": messages,
                    "tools":   OLLAMA_TOOLS,
                    "stream":  False,
                },
                timeout=300,
            )
            r.raise_for_status()
            return r.json()
        except requests.exceptions.ConnectionError:
            self.error.emit(
                f"Cannot reach Ollama at {BASE_URL}. "
                "Check that the server is running and the IP/port are correct."
            )
            return None
        except requests.exceptions.Timeout:
            self.error.emit("Ollama request timed out (>300 s). The model may be overloaded.")
            return None
        except Exception as e:
            self.error.emit(f"Ollama error: {e}")
            return None
