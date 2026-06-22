"""
Agent runner — demonstrates an AI agent controlling the scanner.

Uses the Anthropic Python SDK to run a Claude agent that autonomously
decides how to operate the scanner via tool use.

Usage:
    export ANTHROPIC_API_KEY=sk-ant-...
    python run_agent.py
"""

import json
import time
from pathlib import Path
import anthropic
from controller import ScannerController
from agent_tools import TOOLS, dispatch

CONTEXT_FILE = Path(__file__).parent / "context.txt"


def _load_context() -> str:
    """Load operator context from context.txt if it exists."""
    return CONTEXT_FILE.read_text(encoding="utf-8").strip() if CONTEXT_FILE.exists() else ""


def _api_call_with_retry(client, **kwargs):
    """Call the API, retrying on 429 rate-limit errors with exponential backoff."""
    for attempt in range(5):
        try:
            return client.messages.create(**kwargs)
        except anthropic.RateLimitError as e:
            wait = 2 ** attempt  # 1s, 2s, 4s, 8s, 16s
            print(f"  [Rate limited — waiting {wait}s before retry {attempt+1}/5]")
            time.sleep(wait)
    raise RuntimeError("Exceeded retry limit after repeated rate-limit errors")


def run_agent(task: str, max_turns: int = 20):
    """Run a Claude agent that controls the scanner to accomplish a task."""
    client = anthropic.Anthropic()
    controller = ScannerController()

    context = _load_context()
    context_block = (
        f"\n\n---\nOPERATOR CONTEXT & INSTRUMENT NORMS\n{context}\n---"
        if context else ""
    )

    system_prompt = (
        "You are an AI agent controlling a 2D scanning instrument. "
        "The scanner has a motorized X-Y stage (range 0-100 mm each) and a detector "
        "that measures signal intensity at the current position.\n\n"
        "The sample contains several features (peaks) at unknown locations. "
        "Use the available tools to operate the scanner and accomplish the task."
        + context_block
    )

    messages = [{"role": "user", "content": task}]

    print(f"\n{'='*60}")
    print(f"TASK: {task}")
    print(f"{'='*60}\n")

    for turn in range(max_turns):
        response = _api_call_with_retry(
            client,
            model="claude-haiku-4-5-20251001",
            max_tokens=1024,
            system=system_prompt,
            tools=TOOLS,
            messages=messages,
        )

        # Process response
        assistant_content = response.content
        messages.append({"role": "assistant", "content": assistant_content})

        # Print any text blocks
        for block in assistant_content:
            if block.type == "text":
                print(f"Agent: {block.text}\n")

        # If the model stopped without tool use, we're done
        if response.stop_reason == "end_turn":
            print("\n[Agent finished]")
            break

        # Execute tool calls
        tool_results = []
        for block in assistant_content:
            if block.type == "tool_use":
                print(f"  → Tool: {block.name}({json.dumps(block.input)})")
                result = dispatch(controller, block.name, block.input)

                if isinstance(result, list):
                    # Scan result: image + text — show only the text part in CLI
                    text_part = next(
                        (b["text"] for b in result if b.get("type") == "text"), ""
                    )
                    print(f"    Result: [image] + {text_part}")
                    content = result
                else:
                    print(f"    Result: {json.dumps(result)}")
                    content = json.dumps(result)

                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": content,
                })

        messages.append({"role": "user", "content": tool_results})

    # Print final state
    print(f"\n{'='*60}")
    print(f"Final state: {json.dumps(controller.get_state(), indent=2)}")
    print(f"{'='*60}\n")

    return controller


if __name__ == "__main__":
    task = (
        "Find the strongest signal peak on the sample. Do a coarse survey first, "
        "then zoom in to precisely locate the peak. Report the peak position and intensity."
    )
    run_agent(task)
