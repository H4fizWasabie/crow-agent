"""Thin CLI entry point: crow-agent chat [options]."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys

# Load .env before anything else reads env vars
try:
    from dotenv import load_dotenv
    _PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    load_dotenv(os.path.join(_PROJECT_ROOT, ".env"))
except ImportError:
    pass

from .run_agent import AIAgent, Trigger, TriggerSource
from .toolsets import ToolRegistry
from .config_check import check_or_exit

# Validate config before anything else
check_or_exit()
from .model_tools import register_builtins
from .skills_system import scan_skills_dirs


def _setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="crow-agent", description="Crow Agent CLI")
    sub = p.add_subparsers(dest="command")

    chat = sub.add_parser("chat", help="Start an interactive chat session")
    chat.add_argument("--session", default="default", help="Session ID")
    chat.add_argument("--provider", default="opencode-go", help="Provider name")
    chat.add_argument("--model", default=None, help="Model override")
    chat.add_argument("--verbose", "-v", action="store_true")

    return p


def cmd_chat(args: argparse.Namespace) -> None:
    _setup_logging(args.verbose)
    logger = logging.getLogger("crow_agent.cli")

    from crow_agent.provider_manager import ProviderManager
    from crow_agent.agent_profiles import load_all_profiles, run_child_task
    from crow_agent.providers import resolve_provider
    from crow_agent.crow_state import CrowState

    pm = ProviderManager()
    pm.seed_from_env()

    tools = ToolRegistry()
    _cli_profiles = load_all_profiles()

    def _cli_retrieve(output_id: str) -> str:
        db = CrowState()
        try:
            result = db.get_tool_output(output_id)
            return result or f"Output {output_id} not found"
        finally:
            db.close()

    def _cli_spawn(role: str, task: str) -> str:
        profile = _cli_profiles.get(role)
        if not profile:
            return f"Error: unknown agent role '{role}'. Available: {', '.join(_cli_profiles)}"
        try:
            prov = resolve_provider(args.provider)
            return run_child_task(profile, task, prov, tools)
        except Exception as exc:
            return f"Error spawning agent '{role}': {exc}"

    register_builtins(tools, spawn_fn=_cli_spawn, retrieve_fn=_cli_retrieve)
    skills = scan_skills_dirs()

    agent = AIAgent(
        session_id=args.session,
        provider_name=args.provider,
        model=args.model,
        tool_registry=tools,
        skills_index=skills,
        provider_manager=pm,
    )

    print(f"Crow Agent [{args.provider}] session={args.session}")
    print("=" * 50)
    print("What I can do:")
    print("  • Answer questions (web search, document lookup)")
    print("  • Read & write files")
    print("  • Run commands & scripts")
    print("  • Remember things for later")
    print("  • Delegate background tasks")
    print("  • Generate images")
    print("")
    print("Type /help for commands, or just start chatting.")
    print("")

    try:
        while True:
            try:
                user_input = input("you> ").strip()
            except (EOFError, KeyboardInterrupt):
                break
            if user_input.lower() in ("quit", "exit", "q"):
                break
            if not user_input:
                continue

            try:
                trigger = Trigger(source=TriggerSource.USER, prompt=user_input)
                response = agent.run(trigger)
                print(f"agent> {response}\n")

                # Drain any delegated tasks
                import asyncio
                from crow_agent.task_registry import drain_and_execute

                async def _cli_deliver(task_id: str, result: str, error: str | None) -> None:
                    status = "FAILED" if error else "DONE"
                    print(f"[Task {task_id}] {status}")
                    print(error or result)
                    print()

                asyncio.run(drain_and_execute(deliver=_cli_deliver, background=False))
            except Exception as exc:
                print(f"error> {exc}\n")
                logger.exception("Turn failed")
    finally:
        agent.close()
        print("Session ended.")

        # Inline extraction handles real-time learning (memory_tracker.py).
        # Vault maintenance (archive + index rebuild) runs via weekly cron.


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()

    if args.command == "chat":
        cmd_chat(args)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
