#!/usr/bin/env python3
"""Validate native capture tracks, mix for Whisper, and retain track provenance."""
from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import shutil
import struct
import subprocess
import wave


def validate_track(path: Path) -> dict:
    """Inspect actual PCM data, not just a nonempty WAV header (bounded memory)."""
    with wave.open(str(path), "rb") as audio:
        if audio.getsampwidth() != 2 or audio.getnchannels() != 1 or audio.getframerate() != 48000:
            raise ValueError(f"Unexpected capture format: {path}; expected 48 kHz mono PCM16")
        frames, peak, square_sum = 0, 0, 0
        while data := audio.readframes(48000):
            if len(data) % 2:
                raise ValueError(f"Truncated PCM sample: {path}")
            samples = struct.unpack(f"<{len(data) // 2}h", data)
            frames += len(samples)
            peak = max(peak, max(map(abs, samples), default=0))
            square_sum += sum(value * value for value in samples)
        if frames != audio.getnframes():
            raise ValueError(f"Truncated WAV data: {path}")
        if frames < 4800 or peak < 4:
            raise ValueError(f"No usable signal in {path}; raw tracks/log kept, transcription not started")
        return {"frames": frames, "duration_seconds": frames / 48000,
                "peak_dbfs": 20 * math.log10(peak / 32768),
                "rms_dbfs": 10 * math.log10(square_sum / frames / 32768**2)}


def prepare(system: Path, microphone: Path | None, output: Path,
            system_gain: float = 1.0, mic_gain: float = 1.0) -> dict:
    if output.exists():
        raise FileExistsError(f"Refusing to overwrite {output}")
    for gain in [system_gain, mic_gain]:
        if not math.isfinite(gain) or not 0 < gain <= 10:
            raise ValueError("Recording gains must be finite and in (0, 10]")
    tracks = {"system": {"path": str(system), **validate_track(system)}}
    if microphone is None:
        with output.open("xb") as destination, system.open("rb") as source:
            shutil.copyfileobj(source, destination)
    else:
        tracks["microphone"] = {"path": str(microphone), **validate_track(microphone)}
        # Capture already aligns each input to a common host timeline. Normalize
        # weights to prevent summing two full-scale signals into clipping.
        command = ["ffmpeg", "-nostdin", "-n", "-v", "error", "-i", str(system), "-i", str(microphone),
                   "-filter_complex", f"[0:a][1:a]amix=inputs=2:duration=longest:dropout_transition=0:weights='{system_gain} {mic_gain}':normalize=1[out]",
                   "-map", "[out]", "-ar", "48000", "-ac", "1", "-c:a", "pcm_s16le", str(output)]
        subprocess.run(command, check=True)
    mixed = validate_track(output)
    return {"source": "both" if microphone else "system", "tracks": tracks,
            "main_audio": mixed, "system_gain": system_gain, "microphone_gain": mic_gain}


def retain(manifest: Path, workspace: Path, stem: str, recorded_at: str) -> None:
    metadata_path = workspace / "source_meta.json"
    metadata = json.loads(metadata_path.read_text())
    capture = json.loads(manifest.read_text())
    for role, track in capture["tracks"].items():
        original = Path(track["path"])
        destination = workspace / f"{stem}_{role}.wav"
        # Exclusive creation protects sidecars too, including orphaned old files.
        with destination.open("xb") as target, original.open("rb") as source:
            shutil.copyfileobj(source, target)
        track["path"] = str(destination)
    capture["recorded_at"] = recorded_at
    metadata["recording"] = capture
    temporary = metadata_path.with_suffix(".json.recording-tmp")
    with temporary.open("x") as target:
        json.dump(metadata, target, ensure_ascii=False, indent=2)
        target.write("\n")
    os.replace(temporary, metadata_path)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    capture = commands.add_parser("prepare")
    capture.add_argument("--system", required=True, type=Path)
    capture.add_argument("--microphone", type=Path)
    capture.add_argument("--output", required=True, type=Path)
    capture.add_argument("--capture-info", type=Path)
    capture.add_argument("--manifest", required=True, type=Path)
    capture.add_argument("--system-gain", type=float, default=1)
    capture.add_argument("--mic-gain", type=float, default=1)
    attach = commands.add_parser("retain")
    attach.add_argument("--manifest", required=True, type=Path)
    attach.add_argument("--workspace", required=True, type=Path)
    attach.add_argument("--stem", required=True)
    attach.add_argument("--recorded-at", required=True)
    args = parser.parse_args()
    try:
        if args.command == "prepare":
            if args.manifest.exists():
                raise FileExistsError(f"Refusing to overwrite {args.manifest}")
            result = prepare(args.system, args.microphone, args.output, args.system_gain, args.mic_gain)
            if args.capture_info:
                result.update(json.loads(args.capture_info.read_text()))
            with args.manifest.open("x") as target:
                json.dump(result, target, indent=2)
        else:
            retain(args.manifest, args.workspace, args.stem, args.recorded_at)
    except (OSError, ValueError, wave.Error, subprocess.CalledProcessError) as error:
        parser.exit(1, f"[!] {error}\nCapture files and logs were kept for recovery.\n")


if __name__ == "__main__":
    main()
