"""
AgentWorker — runs the local Ollama agent loop in a QThread so the GUI stays live.

Connects to an Ollama server over HTTP (no Anthropic API key required).
Configure OLLAMA_IP, OLLAMA_PORT, and OLLAMA_MODEL below.

Signals
-------
agent_message  : str            raw reasoning text (→ full log)
tool_called    : (str, str)     (tool_name, compact_result) (→ full log)
turn_summary   : (int, str)     (turn_number, summary) (→ chat box)
finished       : str            final conclusion
error          : str            error message
"""

import json
import requests
from PyQt6.QtCore import QThread, pyqtSignal

from controller import MicroscopeController
from agent_tools import TOOLS, dispatch

# ── Configuration — edit these to match your Ollama server ───────────────────
OLLAMA_IP    = "10.82.5.167"
OLLAMA_PORT  = 11434
OLLAMA_MODEL = "gemma4:26b"
BASE_URL     = f"http://{OLLAMA_IP}:{OLLAMA_PORT}"

SYSTEM_PROMPT = """\
You are an AI agent controlling a pump-probe microscope. You have access to tools \
to read instrument state, list available scan axes and measurements, and run 1D scans.

After every set of tool calls, include a short (1-3 sentence) plain-English summary \
of what you just did and what you found. This is shown live to the operator.

Be methodical: check available axes and measurements before scanning if unsure. \
When a scan is complete, report the key result (range, shape, notable features)."""


def _to_ollama_tools(anthropic_tools: list) -> list:
    """Convert Anthropic-format tool definitions to Ollama/OpenAI function format."""
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


class AgentWorker(QThread):
    agent_message = pyqtSignal(str)
    tool_called   = pyqtSignal(str, str)   # (tool_name, compact_result)
    turn_summary  = pyqtSignal(int, str)   # (turn#, summary)
    finished      = pyqtSignal(str)
    error         = pyqtSignal(str)

    def __init__(self, controller: MicroscopeController, task: str,
                 context: str = "", tabs: dict | None = None, parent=None):
        super().__init__(parent)
        self.controller = controller
        self.task = task
        self.context = context.strip()
        self.tabs = tabs or {}
        self._stop_requested = False

    def stop(self):
        self._stop_requested = True

    def run(self):
        ollama_tools = _to_ollama_tools(TOOLS)

        system = SYSTEM_PROMPT
        if self.context:
            system += f"\n\n---\nOPERATOR CONTEXT & NORMS\n{self.context}\n---"

        messages = [
            {"role": "system", "content": system},
            {"role": "user",   "content": self.task},
        ]
        max_turns = 30

        for turn_num in range(1, max_turns + 1):
            if self._stop_requested:
                self.finished.emit("Agent stopped by user.")
                return

            # ── Call Ollama ───────────────────────────────────────────────────
            try:
                resp = requests.post(
                    f"{BASE_URL}/api/chat",
                    json={"model": OLLAMA_MODEL, "messages": messages,
                          "tools": ollama_tools, "stream": False},
                    timeout=300,
                )
                resp.raise_for_status()
                data = resp.json()
            except requests.RequestException as e:
                self.error.emit(f"Ollama connection error: {e}")
                return

            msg         = data.get("message", {})
            text        = (msg.get("content") or "").strip()
            tool_calls  = msg.get("tool_calls") or []

            if text:
                self.agent_message.emit(text)

            # ── No tool calls → agent is done ─────────────────────────────────
            if not tool_calls:
                conclusion = text or "Task complete."
                self.turn_summary.emit(turn_num, conclusion)
                self.finished.emit(conclusion)
                return

            # ── Record assistant turn ─────────────────────────────────────────
            assistant_record = {"role": "assistant", "content": text}
            if tool_calls:
                assistant_record["tool_calls"] = tool_calls
            messages.append(assistant_record)

            # ── Execute tools ─────────────────────────────────────────────────
            tool_results_msgs = []
            pending_images: list[str] = []
            tool_names: list[str] = []

            for tc in tool_calls:
                if self._stop_requested:
                    self.finished.emit("Agent stopped by user.")
                    return

                fn   = tc.get("function", {})
                name = fn.get("name", "")
                raw  = fn.get("arguments", {})
                args = raw if isinstance(raw, dict) else json.loads(raw)

                try:
                    result = dispatch(self.controller, name, args, tabs=self.tabs)
                except Exception as e:
                    result = {"error": str(e)}

                if isinstance(result, list):
                    # Multipart (image + text): extract pieces
                    text_part = next(
                        (b["text"] for b in result if b.get("type") == "text"), ""
                    )
                    for b in result:
                        if b.get("type") == "image":
                            pending_images.append(b["source"]["data"])
                    compact = text_part[:200] + ("…" if len(text_part) > 200 else "")
                    content = text_part
                else:
                    content = json.dumps(result)
                    compact = content[:200] + ("…" if len(content) > 200 else "")

                self.tool_called.emit(name, compact)
                tool_names.append(name)
                tool_results_msgs.append({"role": "tool", "content": content})

            messages.extend(tool_results_msgs)

            # Send images as a follow-up user message so the model can see them
            if pending_images:
                messages.append({
                    "role": "user",
                    "content": (
                        f"Scan complete. {len(pending_images)} image(s) attached. "
                        "Use the image(s) and the JSON summary above to decide your next step."
                    ),
                    "images": pending_images,
                })

            # ── Turn summary for the chat box ─────────────────────────────────
            summary = text if text else f"Called: {', '.join(tool_names)}."
            self.turn_summary.emit(turn_num, summary)

        self.finished.emit("Reached max turns without completing the task.")
