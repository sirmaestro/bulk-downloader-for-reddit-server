"""PreToolUse hook (Bash, PowerShell): asks before a command that would contact the live Arctic Shift API.

Matches the API's host in the command itself, or in a .py/.sh/.ps1 file the command names, and commands that run
bdfr with an archive listing (--user, serve). The user then approves or denies it; see AGENTS.md, "Arctic Shift".
Reads the hook payload from stdin; prints a permission decision as JSON, or nothing (no opinion).
"""

import json
import re
import sys
from pathlib import Path

HOST = "arctic-shift.photon-reddit.com"
SCRIPT = re.compile(r"""[^\s'"]+\.(?:py|sh|ps1)\b""")
BDFR_RUN = re.compile(r"\b(?:bdfr|python3?\s+-m\s+bdfr)\s+(?:serve\b|download\b.*--user\b)")


def reason(command: str) -> str:
    if HOST in command:
        return "the command contacts the live Arctic Shift API"
    if BDFR_RUN.search(command):
        return "the command runs bdfr against the live Arctic Shift API"
    for name in SCRIPT.findall(command):
        try:
            if HOST in Path(name).read_text(encoding="utf-8", errors="ignore"):
                return f"{name} contacts the live Arctic Shift API"
        except OSError:
            continue
    return ""


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except ValueError:
        return 0
    command = str((payload.get("tool_input") or {}).get("command", ""))
    why = reason(command)
    if why:
        print(
            json.dumps(
                {
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "ask",
                        "permissionDecisionReason": f"Arctic Shift guard: {why}. AGENTS.md: avoid it; tests use the "
                        "mock. Approve only if this request is really needed.",
                    }
                }
            )
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
