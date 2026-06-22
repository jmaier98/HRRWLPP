"""
Agent runner — uses a local Ollama model as the backend instead of Anthropic.

Communicates with Gemma 4 (or any vision-capable Ollama model) running on a
remote Ollama server via HTTP.  Images from scan results are forwarded to the
model as base64 vision inputs, matching the way the original agent uses them.

Usage:
    python run_agent_local.py
    python run_agent_local.py "your custom task here"

Configuration (edit the block below or set env vars):
    OLLAMA_IP   — IP address of the machine running Ollama
    OLLAMA_PORT — port (default 11434)
    OLLAMA_MODEL — model tag (default gemma4:26b)
"""

import json
import sys
import requests
from pathlib import Path
from controller import ScannerController
from agent_tools import TOOLS, dispatch

# ── Configuration ─────────────────────────────────────────────
OLLAMA_IP   = "10.82.5.167"
OLLAMA_PORT = 11434
OLLAMA_MODEL = "gemma4:26b"

BASE_URL = f"http://{OLLAMA_IP}:{OLLAMA_PORT}"
CONTEXT_FILE = Path(__file__).parent / "context.txt"


# ── Tool format conversion ─────────────────────────────────────

def _to_ollama_tools(anthropic_tools: list) -> list:
    """Convert Anthropic tool definitions to Ollama/OpenAI function format."""
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


# ── Ollama HTTP call ──────────────────────────────────────────

def _ollama_chat(messages: list, tools: list | None = None, timeout: int = 300) -> dict:
    payload = {
        "model": OLLAMA_MODEL,
        "messages": messages,
        "stream": False,
    }
    if tools:
        payload["tools"] = tools

    r = requests.post(f"{BASE_URL}/api/chat", json=payload, timeout=timeout)
    r.raise_for_status()
    return r.json()


# ── Message building helpers ──────────────────────────────────

def _make_tool_result_messages(
    tool_calls: list,
    controller: ScannerController,
) -> tuple[list, list]:
    """
    Execute each tool call and build the tool-result messages for Ollama.

    Ollama tool result format:
        {"role": "tool", "content": "<text>"}

    Vision images cannot go inside a tool result in Ollama's protocol.
    Instead we collect them and the caller appends a follow-up user message
    that carries the images so the model can see them.

    Returns:
        tool_result_msgs  — list of {"role": "tool", ...} messages
        pending_images    — list of base64 PNG strings extracted from results
    """
    tool_result_msgs = []
    pending_images = []

    for tc in tool_calls:
        fn = tc.get("function", {})
        name = fn.get("name", "")
        raw_args = fn.get("arguments", {})
        args = raw_args if isinstance(raw_args, dict) else json.loads(raw_args)

        print(f"  → Tool: {name}({json.dumps(args)})")
        result = dispatch(controller, name, args)

        if isinstance(result, list):
            # Multipart result: image + text blocks
            text_part = ""
            for block in result:
                if block.get("type") == "text":
                    text_part = block["text"]
                elif block.get("type") == "image":
                    pending_images.append(block["source"]["data"])
            print(f"    Result: [image] + {text_part}")
            content = text_part
        else:
            content = json.dumps(result)
            print(f"    Result: {content}")

        tool_result_msgs.append({"role": "tool", "content": content})

    return tool_result_msgs, pending_images


# ── Main agent loop ───────────────────────────────────────────

def run_agent_local(task: str, max_turns: int = 20):
    """Run a local-model agent that controls the scanner to accomplish a task."""
    controller = ScannerController()
    ollama_tools = _to_ollama_tools(TOOLS)

    context = CONTEXT_FILE.read_text(encoding="utf-8").strip() if CONTEXT_FILE.exists() else ""
    context_block = (
        f"\n\nOPERATOR CONTEXT & INSTRUMENT NORMS\n{context}"
        if context else ""
    )

    system_prompt = (
        "You are an AI agent controlling a 2D scanning instrument. "
        "The scanner has a motorized X-Y stage (range 0–100 mm each) and a "
        "detector that measures signal intensity at the current position.\n\n"
        "The sample contains several features (peaks) at unknown locations. "
        "Use the available tools to operate the scanner and accomplish the task. "
        "Think step by step. After each scan result you will receive a summary "
        "and possibly an image — use them to decide your next action."
        + context_block
    )

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user",   "content": task},
    ]

    print(f"\n{'='*60}")
    print(f"TASK: {task}")
    print(f"MODEL: {OLLAMA_MODEL}  @  {BASE_URL}")
    print(f"{'='*60}\n")

    for turn in range(max_turns):
        print(f"[Turn {turn + 1}]")
        try:
            response = _ollama_chat(messages, tools=ollama_tools)
        except requests.RequestException as e:
            print(f"  ERROR contacting Ollama: {e}")
            break

        msg = response.get("message", {})
        content_text = msg.get("content", "") or ""
        tool_calls   = msg.get("tool_calls") or []

        if content_text:
            print(f"Agent: {content_text}\n")

        # No tool calls → model is done
        if not tool_calls:
            print("\n[Agent finished — no further tool calls]")
            break

        # Record assistant turn (preserve tool_calls so history is coherent)
        assistant_record = {"role": "assistant", "content": content_text}
        if tool_calls:
            assistant_record["tool_calls"] = tool_calls
        messages.append(assistant_record)

        # Execute tools
        tool_result_msgs, pending_images = _make_tool_result_messages(
            tool_calls, controller
        )
        messages.extend(tool_result_msgs)

        # If scan tools produced images, append them as a vision user message
        # so the model can see the spatial data on the next turn.
        if pending_images:
            messages.append({
                "role":    "user",
                "content": (
                    f"Scan complete. {len(pending_images)} image(s) attached. "
                    "Use the image(s) and the JSON summary above to decide your next step."
                ),
                "images": pending_images,
            })

    else:
        print("\n[Agent reached max_turns limit]")

    print(f"\n{'='*60}")
    print(f"Final state: {json.dumps(controller.get_state(), indent=2)}")
    print(f"{'='*60}\n")

    return controller


# ── Entry point ───────────────────────────────────────────────

if __name__ == "__main__":
    task = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else (
        "Find the strongest signal peak on the sample. Do a coarse survey first, "
        "then zoom in to precisely locate the peak. Report the peak position and intensity."
    )
    run_agent_local(task)
