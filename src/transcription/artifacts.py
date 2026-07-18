"""Artifact path planning, conflict checks, and validation."""

from __future__ import annotations

from pathlib import Path

from .types import ArtifactKind, Stage, TranscriptionError, TranscribeRequest


def assert_safe_stem(stem: str) -> None:
    """Reject stems that are not a single relative filename component."""
    if not stem or stem != stem.strip():
        raise TranscriptionError(
            Stage.VALIDATE_INPUT,
            "stem must be a non-empty filename component without surrounding whitespace",
        )
    if "\x00" in stem:
        raise TranscriptionError(Stage.VALIDATE_INPUT, "stem must not contain NUL bytes")

    path = Path(stem)
    if path.is_absolute() or bool(path.anchor):
        raise TranscriptionError(
            Stage.VALIDATE_INPUT,
            f"stem must not be an absolute path: {stem!r}",
        )
    if len(path.parts) != 1 or path.parts[0] in {".", ".."}:
        raise TranscriptionError(
            Stage.VALIDATE_INPUT,
            f"stem must be a single safe filename component: {stem!r}",
        )


def normalized_audio_path(request: TranscribeRequest) -> Path:
    return request.output_dir / f"{request.stem}_norm16k.wav"


def output_base(request: TranscribeRequest) -> Path:
    return request.output_dir / f"{request.stem}_transcription"


def artifact_path(request: TranscribeRequest, kind: ArtifactKind) -> Path:
    return Path(f"{output_base(request)}.{kind.value}")


def planned_artifact_paths(request: TranscribeRequest) -> dict[ArtifactKind, Path]:
    return {kind: artifact_path(request, kind) for kind in sorted(request.outputs, key=lambda k: k.value)}


def conflict_candidates(request: TranscribeRequest) -> list[Path]:
    paths: list[Path] = []
    if request.normalize:
        paths.append(normalized_audio_path(request))
    paths.extend(planned_artifact_paths(request).values())
    return paths


def assert_outputs_within_output_dir(request: TranscribeRequest) -> None:
    """Ensure every planned output resolves under ``output_dir``."""
    assert_safe_stem(request.stem)
    root = request.output_dir.expanduser().resolve()
    for path in conflict_candidates(request):
        resolved = path.expanduser().resolve()
        try:
            resolved.relative_to(root)
        except ValueError as exc:
            raise TranscriptionError(
                Stage.VALIDATE_INPUT,
                f"Planned output escapes output_dir: {path}",
            ) from exc


def assert_no_output_conflicts(request: TranscribeRequest) -> None:
    for path in conflict_candidates(request):
        if path.exists():
            raise TranscriptionError(
                Stage.CHECK_OUTPUTS,
                f"Output already exists; refusing to overwrite: {path}",
            )


def validate_requested_artifacts(request: TranscribeRequest) -> dict[ArtifactKind, Path]:
    artifacts = planned_artifact_paths(request)
    missing = [path for path in artifacts.values() if not path.is_file() or path.stat().st_size <= 0]
    if missing:
        joined = ", ".join(str(path) for path in missing)
        raise TranscriptionError(
            Stage.VALIDATE_ARTIFACTS,
            f"Requested transcript artifacts missing or empty after whisper-cli: {joined}",
        )
    return artifacts


def remove_paths(paths: list[Path]) -> None:
    """Best-effort cleanup of files created during a failed attempt."""
    for path in paths:
        try:
            if path.is_file():
                path.unlink()
        except OSError:
            continue
