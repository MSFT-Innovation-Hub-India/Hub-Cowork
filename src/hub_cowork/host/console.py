"""
Meeting Agent — Console entry point.

This preserves the original terminal-based interaction for development / debugging.
For the background UI version, run meeting_agent.py instead.
"""

import logging
import sys

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(message)s",
    datefmt="%H:%M:%S",
)
logging.getLogger("azure.identity").setLevel(logging.ERROR)

from hub_cowork.core.agent_core import run_agent, reset_qa_history
from hub_cowork.core.outlook_helper import _resolve_organizer


def main():
    print("=" * 60)
    print("  Hub Cowork — Console")
    print("  Ask anything or describe a meeting to schedule.")
    print("  Type 'new' to start a fresh conversation, 'quit' to exit.")
    print("=" * 60)

    try:
        organizer_name, organizer_email = _resolve_organizer()
        print(f"\n  Logged in as: {organizer_name} <{organizer_email}>")
        print(f"  Invites will be sent on behalf of this identity.\n")
    except Exception as e:
        print(f"\n  [Warning] Could not resolve organizer identity: {e}")
        print(f"  Invites will use fallback organizer details.\n")

    while True:
        try:
            user_input = input("\nYou: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nGoodbye.")
            return

        if not user_input:
            continue

        if user_input.lower() in ("quit", "exit"):
            print("Goodbye.")
            return

        if user_input.lower() == "new":
            reset_qa_history()
            print("\n--- New conversation ---\n")
            continue

        try:
            reply = run_agent(user_input)
            print(f"\nAgent: {reply}")
        except Exception as e:
            print(f"\n  [Error] {e}")


if __name__ == "__main__":
    main()
