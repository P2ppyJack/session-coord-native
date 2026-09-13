#!/usr/bin/env python3
"""Use the canonical session-coord installer for optional plugin lifecycle changes.

This entry point never copies board/plugin files itself. It delegates the
explanatory prompt, standalone install, pinned upgrade and plugin-only removal.
"""

from __future__ import annotations

import argparse
import os
import subprocess  # nosec B404
import sys
from pathlib import Path


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, add_help=False)
    parser.add_argument(
        "--board-installer",
        type=Path,
        default=Path(__file__).resolve().parent.parent / "session-coord/install.py",
    )
    parser.add_argument("--hermes-home", type=Path)
    args, remaining = parser.parse_known_args(argv)
    installer = args.board_installer.expanduser().resolve()
    if not installer.is_file() or installer == Path(__file__).resolve():
        print(
            "ACTION NEEDED: supply the canonical session-coord install.py with "
            "--board-installer /path/to/session-coord/install.py. Nothing changed.",
            file=sys.stderr,
        )
        return 2
    if "--enable" in remaining or "--force" in remaining or "--json" in remaining:
        print(
            "The legacy copy installer is retired. Use --plugin install, keep or remove; "
            "use hermes_setup.py for JSON reports. No force-overwrite bypass is supported.",
            file=sys.stderr,
        )
        return 2
    command = [sys.executable, str(installer), *remaining]
    if not any(
        argument == "--plugin-path" or argument.startswith("--plugin-path=")
        for argument in remaining
    ):
        command.extend(["--plugin-path", str(Path(__file__).resolve().parent)])
    env = dict(os.environ)
    if args.hermes_home is not None:
        env["HERMES_HOME"] = str(args.hermes_home.expanduser().resolve())
    try:
        # The operator selects a reviewed local installer; arguments are not shell text.
        return subprocess.run(command, env=env, check=False).returncode  # nosec B603
    except (OSError, KeyboardInterrupt) as exc:
        print("Installer did not complete: " + str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
