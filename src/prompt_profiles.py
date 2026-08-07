"""Load prompt-profile metadata from private local notes.

The prompt bodies themselves are intentionally kept outside git.  This module
only needs their small YAML-like front matter to build the interactive choice
list and to record which local note was selected in pipeline state.
"""

from __future__ import annotations

import os
from pathlib import Path


DEFAULT_PROMPT_PROFILES: dict[str, dict[str, str]] = {
    "inno": {
        "label": "inno（Inno Group／AI Team）",
        "source": "39ddf30c-c71c-81b6-a658-f0ec07fd7e36",
    },
    "whisper": {
        "label": "whisper（土木相關）",
        "source": "whisper-3a3df30c-c71c-8151-95bad39aeb1eb6e1",
    },
    "new": {
        "label": "尚未建立（只產生待補提示詞的 state）",
        "source": "not-registered",
    },
}


def default_prompt_notes_dir() -> Path:
    """Return the private prompt-note directory for this checkout."""
    configured = os.getenv("PROMPT_NOTES_DIR")
    if configured:
        return Path(configured).expanduser().resolve()
    return Path(__file__).resolve().parents[1] / ".local" / "prompt_notes"


def _front_matter(text: str) -> dict[str, str]:
    """Parse the intentionally small ``key: value`` front matter contract."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    values: dict[str, str] = {}
    for line in lines[1:]:
        if line.strip() == "---":
            break
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        values[key.strip()] = value.strip()
    return values


def load_prompt_profiles(notes_dir: Path | None = None) -> dict[str, dict[str, str]]:
    """Load default profiles and override/extend them from local ``.md`` notes."""
    directory = (notes_dir or default_prompt_notes_dir()).expanduser().resolve()
    profiles = {key: dict(value) for key, value in DEFAULT_PROMPT_PROFILES.items()}
    if not directory.is_dir():
        return profiles

    for path in sorted(directory.glob("*.md")):
        try:
            metadata = _front_matter(path.read_text(encoding="utf-8"))
        except OSError:
            continue
        key = metadata.get("key", path.stem).strip()
        if not key:
            continue
        label = metadata.get("title") or metadata.get("label") or key
        source = metadata.get("source") or metadata.get("page_id") or "local"
        profiles[key] = {
            "label": label,
            "title": label,
            "source": source,
            "local_path": str(path),
        }
    return profiles


PROMPT_PROFILES = load_prompt_profiles()
