"""Safe .env loading shared by setup.py and shell workflows."""

from __future__ import annotations

import argparse
import os
import re
import shlex
import sys
from pathlib import Path

_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def expand_documented_vars(value: str) -> str:
    """Expand only documented path forms: $HOME, ${HOME}, and leading ~."""
    home = str(Path.home())
    value = value.replace("${HOME}", home).replace("$HOME", home)
    if value.startswith("~"):
        value = os.path.expanduser(value)
    return value


def load_env(env_path: Path) -> dict[str, str]:
    """Load key=value pairs from a .env file.

    Supports quoted values and inline comments via shlex, and expands only
    documented `$HOME` / `~` path forms (no shell evaluation).
    """
    if not env_path.exists():
        raise FileNotFoundError(f".env not found: {env_path}")

    env: dict[str, str] = {}
    for line_no, line in enumerate(env_path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line or line.strip().startswith("#") or "=" not in line:
            continue

        key, raw_value = line.split("=", 1)
        key = key.strip()
        if not key:
            raise ValueError(f"Invalid .env entry on line {line_no}: empty key")
        if not _KEY_RE.match(key):
            raise ValueError(f"Invalid .env key on line {line_no}: {key}")

        try:
            parsed = shlex.split(raw_value, comments=True, posix=True)
        except ValueError as exc:
            raise ValueError(f"Invalid .env value for {key}: {exc}") from exc

        env[key] = expand_documented_vars(" ".join(parsed))
    return env


def dump_env_for_shell(env: dict[str, str]) -> None:
    """Write KEY\\0VALUE\\0 pairs so bash can import values without eval."""
    out = sys.stdout.buffer
    for key, value in env.items():
        if not _KEY_RE.match(key):
            raise ValueError(f"Invalid .env key: {key}")
        out.write(key.encode("utf-8"))
        out.write(b"\0")
        out.write(value.encode("utf-8"))
        out.write(b"\0")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Load project .env values safely")
    parser.add_argument("env_file", type=Path, help="Path to the .env file")
    parser.add_argument(
        "--dump-shell",
        action="store_true",
        help="Emit NUL-delimited KEY/VALUE pairs for shell import",
    )
    args = parser.parse_args(argv)

    env = load_env(args.env_file)
    if args.dump_shell:
        dump_env_for_shell(env)
    else:
        for key, value in env.items():
            print(f"{key}={value}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # noqa: BLE001 - CLI boundary
        print(f"❌ {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
