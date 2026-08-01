"""Fail-closed entrypoint for the production-operated Demo stack."""

from __future__ import annotations

import os
import sys
from pathlib import PurePosixPath
from typing import Mapping, NoReturn, Sequence


class DemoStartupError(RuntimeError):
    pass


SENSITIVE_ENV = (
    "NOBITEX_API_TOKEN", "MYSQL_HOST", "MYSQL_USER", "MYSQL_PASSWORD",
    "MYSQL_DATABASE", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID",
)
ALLOWED_PATHS = {
    "LOB_RESULTS_DIR": PurePosixPath("/app/runtime/results"),
    "LOB_STATE_DIR": PurePosixPath("/app/runtime/state"),
    "DEMO_SESSION_FILE": PurePosixPath("/app/runtime/state"),
}


def _is_within(value: str, root: PurePosixPath) -> bool:
    path = PurePosixPath(value)
    return path == root or root in path.parents


def validate_demo_startup(command: Sequence[str], environ: Mapping[str, str]) -> None:
    if not command:
        raise DemoStartupError("an explicit command is required")
    if "run.py" in command:
        pairs = list(zip(command, command[1:]))
        if ("--mode", "demo") not in pairs or ("--mode", "real") in pairs:
            raise DemoStartupError("worker command must explicitly contain --mode demo")
    for name in SENSITIVE_ENV:
        if environ.get(name, "").strip():
            raise DemoStartupError(f"{name} must be empty in Demo deployment")
    for name, root in ALLOWED_PATHS.items():
        value = environ.get(name)
        if value and not _is_within(value, root):
            raise DemoStartupError(f"{name} must remain under {root}")


def main() -> NoReturn:
    command = sys.argv[1:]
    validate_demo_startup(command, os.environ)
    print("DEMO_STARTUP_GUARD_OK real_operations=disabled", flush=True)
    os.execvp(command[0], command)


if __name__ == "__main__":
    main()
