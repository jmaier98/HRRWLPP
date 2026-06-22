"""
AgentWorker — runs the Claude agent loop in a QThread so the GUI stays live.

Signals
-------
agent_message  : str            raw reasoning text from Claude (full log)
tool_called    : (str, str)     (tool_name, compact_result)  (full log)
turn_summary   : (int, str)     (turn_number, summary_text)  → chat box
finished       : str            final conclusion
error          : str            error message
"""

import json
import time
import anthropic
from PyQt6.QtCore import QThread, pyqtSignal

from controller import ScannerController
from agent_tools import TOOLS, dispatch


class AgentWorker(QThread):
    agent_message = pyqtSignal(str)       # verbose: goes to full log
    tool_called   = pyqtSignal(str, str)  # (tool_name, result) → full log
    turn_summary  = pyqtSignal(int, str)  # (turn#, summary)    → chat box
    finished      = pyqtSignal(str)       # conclusion          → both
    error         = pyqtSignal(str)       # error               → both

    def __init__(self, controller: ScannerController, task: str,
                 context: str = "", parent=None):
        super().__init__(parent)
        self.controller = controller
        self.task = task
        self.context = context.strip()
        self._stop_requested = False

    def stop(self):
        self._stop_requested = True

    def run(self):
        client = anthropic.Anthropic()

        context_block = (
            f"\n\n---\nOPERATOR CONTEXT & INSTRUMENT NORMS\n{self.context}\n---"
            if self.context else ""
        )
        system_prompt = (
            "You are an AI agent controlling a 2D scanning instrument. "
            "The scanner has a motorized X-Y stage (range 0-100 mm each) and a detector "
            "that measures signal intensity at the current position.\n\n"
            "The sample contains several features (peaks) at unknown locations. "
            "Use the available tools to operate the scanner and accomplish the task."
            + context_block +
            "\n\nAfter every set of tool calls, ALWAYS include a short (1-3 sentence) plain-English "
            "summary of what you just did and what you found. This summary will be shown to the "
            "operator watching the instrument in real time."
        )

        messages = [{"role": "user", "content": self.task}]
        max_turns = 20

        for turn_num in range(1, max_turns + 1):
            if self._stop_requested:
                self.finished.emit("Agent stopped by user.")
                return

            response = self._call_with_retry(
                client,
                model="claude-haiku-4-5-20251001",
                max_tokens=1024,
                system=system_prompt,
                tools=TOOLS,
                messages=messages,
            )
            if response is None:
                return

            assistant_content = response.content
            messages.append({"role": "assistant", "content": assistant_content})

            # ── Collect text and tool calls from this turn ────
            texts_this_turn = [
                b.text.strip() for b in assistant_content
                if b.type == "text" and b.text.strip()
            ]
            tools_this_turn = [
                b for b in assistant_content if b.type == "tool_use"
            ]

            # Send raw text to the full log
            for t in texts_this_turn:
                self.agent_message.emit(t)

            # ── Final turn (no more tool calls) ───────────────
            if response.stop_reason == "end_turn":
                conclusion = texts_this_turn[0] if texts_this_turn else "Task complete."
                self.turn_summary.emit(turn_num, conclusion)
                self.finished.emit(conclusion)
                return

            # ── Execute tool calls ─────────────────────────────
            tool_results = []
            tool_names_called = []

            for block in tools_this_turn:
                if self._stop_requested:
                    self.finished.emit("Agent stopped by user.")
                    return

                try:
                    result = dispatch(self.controller, block.name, block.input)
                except Exception as e:
                    result = {"error": str(e)}

                # result is either a dict (plain tools) or a list (scan: image + text)
                if isinstance(result, list):
                    # Pull out the text block for the log; skip the raw image bytes
                    text_part = next(
                        (b["text"] for b in result if b.get("type") == "text"), ""
                    )
                    compact = text_part[:160] + ("…" if len(text_part) > 160 else "")
                    content = result          # pass list directly to API
                else:
                    compact = json.dumps(result)
                    if len(compact) > 160:
                        compact = compact[:160] + "…"
                    content = json.dumps(result)

                self.tool_called.emit(block.name, compact)
                tool_names_called.append(block.name)

                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": content,
                })

            messages.append({"role": "user", "content": tool_results})

            # ── Emit turn summary for the chat box ────────────
            # Prefer Claude's own text; fall back to synthesized description
            if texts_this_turn:
                summary = texts_this_turn[-1]          # last text block of the turn
            else:
                summary = f"Called: {', '.join(tool_names_called)}."
            self.turn_summary.emit(turn_num, summary)

        self.finished.emit("Reached max turns without completing the task.")

    def _call_with_retry(self, client, **kwargs):
        for attempt in range(5):
            if self._stop_requested:
                return None
            try:
                return client.messages.create(**kwargs)
            except anthropic.RateLimitError:
                wait = 2 ** attempt
                self.agent_message.emit(f"[Rate limited — waiting {wait}s…]")
                time.sleep(wait)
            except Exception as e:
                self.error.emit(f"API error: {e}")
                return None
        self.error.emit("Exceeded retry limit after repeated rate-limit errors.")
        return None
