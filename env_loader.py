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


def is_english_only_model(model_name: str) -> bool:
    """Return True for whisper.cpp English-only model names such as small.en."""
    return model_name.endswith(".en")


def validate_model_language(model_name: str, language: str) -> None:
    """Reject English-only models for non-English language settings."""
    normalized_language = language.strip().lower()
    if is_english_only_model(model_name) and normalized_language != "en":
        raise ValueError(
            f"English-only model '{model_name}' cannot be used with "
            f"DEFAULT_LANGUAGE={language}. "
            "Use a multilingual model (e.g. 'small') or set DEFAULT_LANGUAGE=en."
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Load project .env values safely")
    parser.add_argument("env_file", type=Path, nargs="?", help="Path to the .env file")
    parser.add_argument(
        "--dump-shell",
        action="store_true",
        help="Emit NUL-delimited KEY/VALUE pairs for shell import",
    )
    parser.add_argument(
        "--validate-model-language",
        nargs=2,
        metavar=("MODEL", "LANGUAGE"),
        help="Validate PREFERRED_MODEL against DEFAULT_LANGUAGE and exit",
    )
    args = parser.parse_args(argv)

    if args.validate_model_language is not None:
        model_name, language = args.validate_model_language
        validate_model_language(model_name, language)
        return 0

    if args.env_file is None:
        parser.error("env_file is required unless --validate-model-language is used")

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
