"""
Main entry point — launch the GUI or run the agent.

Usage:
    python main.py                        # Launch GUI
    python main.py --agent                # Run agent demo (Anthropic/Claude)
    python main.py --agent "task here"
    python main.py --agent-local          # Run agent demo (local Ollama model)
    python main.py --agent-local "task"
"""

import sys


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--agent":
        from run_agent import run_agent
        task = " ".join(sys.argv[2:]) if len(sys.argv) > 2 else (
            "Find the strongest signal peak on the sample. Do a coarse survey first, "
            "then zoom in to precisely locate the peak. Report the peak position and intensity."
        )
        run_agent(task)
    elif len(sys.argv) > 1 and sys.argv[1] == "--agent-local":
        from run_agent_local import run_agent_local
        task = " ".join(sys.argv[2:]) if len(sys.argv) > 2 else (
            "Find the strongest signal peak on the sample. Do a coarse survey first, "
            "then zoom in to precisely locate the peak. Report the peak position and intensity."
        )
        run_agent_local(task)
    else:
        from gui import run_gui
        run_gui()


if __name__ == "__main__":
    main()
