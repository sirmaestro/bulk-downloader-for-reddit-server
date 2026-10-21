"""PostToolUse hook: sanity-check a file after Claude edits it.

- *.conf / *.xml / *.ps1 must be CRLF on disk (bare LF is an error); .gitattributes declares *.ps1 eol=crlf.
- *.yml / *.yaml must parse.
- *.py: run the offline test suite (the same selection CI runs; online tests are never run).

Reads the hook payload from stdin and checks only the edited file. Pass paths as arguments for a manual check.
Exit 2 + stderr reports problems back to Claude; exit 0 means clean or not applicable.
"""

import json
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
CRLF_EXTS = {".conf", ".xml", ".ps1"}
YAML_EXTS = {".yml", ".yaml"}
PYTEST_CMD = [
    sys.executable,
    "-m",
    "pytest",
    "tests",
    "-q",
    "-m",
    "not online and not slow",
]


def check_crlf(path):
    data = path.read_bytes()
    bare_lf = data.count(b"\n") - data.count(b"\r\n")
    if bare_lf:
        return f"{path}: {bare_lf} line(s) end in bare LF; {path.suffix} files must be CRLF"
    return None


def check_yaml(path):
    try:
        with path.open(encoding="utf-8-sig") as f:
            list(yaml.safe_load_all(f))
    except yaml.YAMLError as e:
        return f"{path}: YAML does not parse:\n{e}"
    return None


def run_tests():
    result = subprocess.run(PYTEST_CMD, cwd=ROOT, capture_output=True, text=True)
    if result.returncode != 0:
        tail = "\n".join((result.stdout + result.stderr).strip().splitlines()[-40:])
        return f"Offline tests failed:\n{tail}"
    return None


def paths_from_hook():
    try:
        payload = json.load(sys.stdin)
    except ValueError:
        return []
    file_path = (payload.get("tool_input") or {}).get("file_path")
    if not file_path:
        return []
    path = Path(file_path)
    return [path if path.is_absolute() else ROOT / path]


def main():
    paths = [Path(p) for p in sys.argv[1:]] or paths_from_hook()
    paths = [p for p in paths if p.is_file()]

    errors = []
    for path in paths:
        ext = path.suffix.lower()
        if ext in CRLF_EXTS:
            errors.append(check_crlf(path))
        elif ext in YAML_EXTS:
            errors.append(check_yaml(path))
    if any(p.suffix.lower() == ".py" for p in paths):
        errors.append(run_tests())

    errors = [e for e in errors if e]
    if errors:
        print("\n".join(errors), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
