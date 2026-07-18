"""Workspace naming and path planning helpers."""

from __future__ import annotations

import re
from pathlib import Path

from src.transcription.types import ArtifactKind

from .types import WorkspaceError, WorkspaceStage

_UNSAFE_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_WHITESPACE = re.compile(r"\s+")


def sanitize_stem(raw: str, *, max_length: int = 80) -> str:
    """Return a single safe filename component derived from a display name."""
    name = (raw or "").strip().replace("\x00", "")
    # Only the final path component; callers may pass an absolute path.
    name = name.replace("\\", "/").split("/")[-1]
    stem = Path(name).stem if name else ""
    stem = _UNSAFE_CHARS.sub("-", stem)
    stem = _WHITESPACE.sub("_", stem)
    stem = stem.strip("._-")
    while "__" in stem:
        stem = stem.replace("__", "_")
    while "--" in stem:
        stem = stem.replace("--", "-")
    if not stem or stem in {".", ".."}:
        stem = "meeting"
    if len(stem) > max_length:
        stem = stem[:max_length].rstrip("._-") or "meeting"
    assert_safe_stem(stem)
    return stem


def assert_safe_stem(stem: str) -> None:
    if not stem or stem != stem.strip():
        raise WorkspaceError(
            WorkspaceStage.VALIDATE,
            "stem must be a non-empty filename component without surrounding whitespace",
        )
    if "\x00" in stem:
        raise WorkspaceError(WorkspaceStage.VALIDATE, "stem must not contain NUL bytes")
    path = Path(stem)
    if path.is_absolute() or bool(path.anchor):
        raise WorkspaceError(
            WorkspaceStage.VALIDATE,
            f"stem must not be an absolute path: {stem!r}",
        )
    if len(path.parts) != 1 or path.parts[0] in {".", ".."}:
        raise WorkspaceError(
            WorkspaceStage.VALIDATE,
            f"stem must be a single safe filename component: {stem!r}",
        )


def workspace_dirname(meeting_time_label: str, safe_stem: str) -> str:
    assert_safe_stem(safe_stem)
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}_\d{4}", meeting_time_label):
        raise WorkspaceError(
            WorkspaceStage.VALIDATE,
            f"invalid meeting time label: {meeting_time_label!r}",
        )
    return f"{meeting_time_label}_{safe_stem}"


def transcript_base(workspace_dir: Path, transcript_stem: str) -> Path:
    return workspace_dir / f"{transcript_stem}_transcription"


def planned_artifact_paths(
    workspace_dir: Path,
    transcript_stem: str,
    outputs: frozenset[ArtifactKind],
) -> dict[ArtifactKind, Path]:
    base = transcript_base(workspace_dir, transcript_stem)
    return {
        kind: Path(f"{base}.{kind.value}")
        for kind in sorted(outputs, key=lambda item: item.value)
    }


def normalized_audio_path(workspace_dir: Path, transcript_stem: str) -> Path:
    return workspace_dir / f"{transcript_stem}_norm16k.wav"


def metadata_path(workspace_dir: Path) -> Path:
    return workspace_dir / "source_meta.json"
