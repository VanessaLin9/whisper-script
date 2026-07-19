"""whisper-cli invocation for a single local audio file."""

from __future__ import annotations

import logging
from pathlib import Path

from .subprocess_runner import SubprocessRunner, bounded_tail
from .types import ArtifactKind, Stage, TranscriptionError

logger = logging.getLogger(__name__)


def build_whisper_command(
    *,
    whisper_cli: Path,
    model_path: Path,
    audio_path: Path,
    language: str,
    threads: int,
    output_base: Path,
    outputs: frozenset[ArtifactKind],
) -> list[str]:
    command = [
        str(whisper_cli),
        "-m",
        str(model_path),
        "-f",
        str(audio_path),
        "--language",
        language,
        "--threads",
        str(threads),
        "--output-file",
        str(output_base),
    ]
    flag_map = {
        ArtifactKind.TXT: "--output-txt",
        ArtifactKind.SRT: "--output-srt",
        ArtifactKind.VTT: "--output-vtt",
        ArtifactKind.JSON: "--output-json",
    }
    for kind in sorted(outputs, key=lambda item: item.value):
        command.append(flag_map[kind])
    return command


def run_whisper(
    *,
    whisper_cli: Path,
    model_path: Path,
    audio_path: Path,
    language: str,
    threads: int,
    output_base: Path,
    outputs: frozenset[ArtifactKind],
    runner: SubprocessRunner,
) -> None:
    command = build_whisper_command(
        whisper_cli=whisper_cli,
        model_path=model_path,
        audio_path=audio_path,
        language=language,
        threads=threads,
        output_base=output_base,
        outputs=outputs,
    )
    logger.info("transcribe stage starting: %s", output_base)
    try:
        result = runner.run(command)
    except TranscriptionError:
        raise
    except Exception as exc:
        logger.error("whisper-cli failed to start: %s", exc)
        raise TranscriptionError(
            Stage.TRANSCRIBE,
            f"whisper-cli failed to start: {exc}",
            cause=exc,
        ) from exc
    if result.returncode != 0:
        diagnostic = bounded_tail((result.stderr or result.stdout).strip()) or None
        logger.error(
            "whisper-cli failed exit=%s stderr=%s",
            result.returncode,
            diagnostic or "",
        )
        raise TranscriptionError(
            Stage.TRANSCRIBE,
            "whisper-cli transcription failed",
            exit_code=result.returncode,
            diagnostic=diagnostic,
        )
