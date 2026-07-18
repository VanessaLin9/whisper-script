"""FFmpeg normalization to 16 kHz mono PCM WAV."""

from __future__ import annotations

import logging
from pathlib import Path

from .subprocess_runner import SubprocessRunner
from .types import Stage, TranscriptionError

logger = logging.getLogger(__name__)


def normalize_audio(
    *,
    ffmpeg: Path,
    audio_path: Path,
    output_path: Path,
    runner: SubprocessRunner,
) -> Path:
    """Write a normalized WAV next to other outputs. Never modifies ``audio_path``."""
    command = [
        str(ffmpeg),
        "-y",
        "-i",
        str(audio_path),
        "-ac",
        "1",
        "-ar",
        "16000",
        "-c:a",
        "pcm_s16le",
        str(output_path),
    ]
    logger.info("normalize stage starting: %s", output_path)
    try:
        result = runner.run(command)
    except TranscriptionError:
        raise
    except Exception as exc:
        logger.error("normalize failed to start: %s", exc)
        raise TranscriptionError(
            Stage.NORMALIZE,
            f"FFmpeg normalization failed to start: {exc}",
            cause=exc,
        ) from exc
    if result.returncode != 0:
        logger.error(
            "normalize failed exit=%s stderr=%s",
            result.returncode,
            (result.stderr or result.stdout).strip(),
        )
        raise TranscriptionError(
            Stage.NORMALIZE,
            "FFmpeg normalization failed",
            exit_code=result.returncode,
        )
    if not output_path.is_file() or output_path.stat().st_size <= 0:
        raise TranscriptionError(
            Stage.NORMALIZE,
            f"Normalized audio missing or empty after FFmpeg: {output_path}",
            exit_code=result.returncode,
        )
    return output_path
