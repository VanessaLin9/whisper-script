"""Bash-free command-line workflows for imported audio and split segments."""

from __future__ import annotations

import contextlib
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from env_loader import load_env, validate_model_language
from scripts.organize_recording import prepare_recording
from src.output_manager import default_outputs_arg
from src.platform_support import copy_to_clipboard, find_executable, show_in_folder, whisper_cli_candidates
from src.transcription.cli import main as transcribe_main

REPO_ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class WorkflowConfig:
    whisper_root: Path
    records_dir: Path
    model: str
    language: str
    threads: str
    model_file: Path
    whisper_cli: Path
    prompt: str


def load_workflow_config(repo_root: Path | None = None) -> WorkflowConfig:
    repo_root = repo_root or REPO_ROOT
    env_file = repo_root / '.env'
    if not env_file.is_file():
        raise ValueError(f'[!] Environment file not found: {env_file}\n    Please copy .env.example to .env and configure your paths:\n    cp .env.example .env')
    try:
        values = load_env(env_file)
    except (OSError, ValueError) as exc:
        raise ValueError(f'[!] Failed to load environment file: {env_file}') from exc
    # The old shell exported every .env key; keep explicit .env values ahead of
    # inherited settings for this documented command-line workflow.
    print(f'[*] Loaded configuration from: {env_file}')
    root_text = values.get('WHISPER_ROOT') or os.getenv('WHISPER_ROOT')
    if not root_text:
        raise ValueError('[!] WHISPER_ROOT not set in .env file')
    root = Path(root_text).expanduser()
    model = values.get('PREFERRED_MODEL') or os.getenv('PREFERRED_MODEL') or 'small'
    language = values.get('DEFAULT_LANGUAGE') or os.getenv('DEFAULT_LANGUAGE') or 'zh'
    try:
        validate_model_language(model, language)
    except ValueError as exc:
        raise ValueError(f'{exc}\n[!] Invalid model/language combination.\n    English-only models (*.en) are only allowed when DEFAULT_LANGUAGE=en.') from exc
    preferred_cli = root / 'build' / 'bin' / 'whisper-cli'
    configured_cli = values.get('WHISPER_CLI') or os.getenv('WHISPER_CLI')
    rooted_cli = next((str(p) for p in whisper_cli_candidates(root) if p.is_file()), None)
    try:
        # Preserve the historical whisper root before considering PATH.
        whisper_cli = find_executable('whisper-cli', explicit=configured_cli or rooted_cli, whisper_root=root)
    except FileNotFoundError as exc:
        raise ValueError(f'[!] whisper-cli not found at {preferred_cli}\n    Please build whisper.cpp first:\n      cd {root}\n      cmake -B build && cmake --build build -j --config Release\n    Or run: python3 setup.py install') from exc
    model_file = root / 'models' / f'ggml-{model}.bin'
    if not model_file.is_file() or not model_file.stat().st_size:
        raise ValueError(f'[!] Preferred model not found: {model_file}\n    Missing configured model(s). English-only files (*.en.bin) do not satisfy a multilingual setting.\n    Download the configured multilingual model:\n      cd {root}\n      bash ./models/download-ggml-model.sh {model}\n    Or run: python3 setup.py install')
    records_dir = Path(values.get('MEETING_RECORDS_DIR') or os.getenv('MEETING_RECORDS_DIR') or Path.home() / 'MeetingRecords').expanduser()
    threads = values.get('THREADS') or os.getenv('THREADS') or str(os.cpu_count() or 8)
    return WorkflowConfig(root, records_dir, model, language, threads, model_file, whisper_cli, values.get('WHISPER_PROMPT') or os.getenv('WHISPER_PROMPT') or '')


def clean_input_path(raw: str) -> Path:
    return Path(raw.replace("'", '').replace('"', '')).expanduser()


def _transcription_args(config: WorkflowConfig, audio: Path, output_dir: Path, stem: str, outputs: str) -> list[str]:
    return ['--audio', str(audio), '--output-dir', str(output_dir), '--stem', stem,
            '--language', config.language, '--model', config.model,
            '--model-path', str(config.model_file), '--whisper-cli', str(config.whisper_cli),
            '--threads', config.threads, '--outputs', outputs]


def transcribe_english(argv: list[str] | None = None) -> int:
    config = load_workflow_config()
    config.records_dir.mkdir(parents=True, exist_ok=True)
    outputs = default_outputs_arg()
    print('[*] Configuration summary:')
    print(f'    Whisper root: {config.whisper_root}\n    Meeting records dir: {config.records_dir}\n    Preferred model: {config.model}\n    Language: {config.language}\n    Threads: {config.threads}\n    Default outputs: {outputs}')
    print(f'    Initial prompt: configured ({len(config.prompt)} chars)' if config.prompt else '    Initial prompt: none')
    print(f'\n[*] Using model: {config.model_file.name}\n')
    try:
        audio = clean_input_path(input('Enter path to your meeting audio (drag & drop or type path): '))
    except EOFError:
        audio = Path('')
    if not audio.is_file():
        print(f'[!] File not found: {audio}')
        return 1
    print(f'[*] Input file: {audio}')
    try:
        organized = prepare_recording(audio, config.records_dir)
    except (OSError, ValueError) as exc:
        print(f'[!] {exc}', file=sys.stderr)
        print('[!] Failed to organize recording into a meeting folder.')
        return 1
    meeting_dir = Path(organized['meeting_dir'])
    source = Path(organized['audio_file'])
    stem = organized['stem']
    print(f'[*] Meeting folder: {meeting_dir}\n[*] Source audio (referenced): {source}')
    try:
        ffprobe = find_executable('ffprobe')
    except FileNotFoundError:
        pass
    else:
        print('[*] Probing duration with ffprobe...')
        subprocess.run([str(ffprobe), '-v', 'error', '-show_entries', 'format=duration', '-of', 'default=noprint_wrappers=1:nokey=1', str(source)], check=False)
    print(f'[*] Starting transcription with {config.model_file.name}...\n    This may take a while for long recordings...')
    try:
        ffmpeg = find_executable('ffmpeg')
    except FileNotFoundError as exc:
        print(f'[!] {exc}', file=sys.stderr)
        return 1
    args = _transcription_args(config, source, meeting_dir, stem, outputs)
    if config.prompt:
        args += ['--prompt', config.prompt]
    status = transcribe_main(args + ['--normalize', '--keep-normalized', '--ffmpeg', str(ffmpeg)])
    if status:
        print('[!] Transcription failed (see stage details above).')
        return status
    out_base = meeting_dir / f'{stem}_transcription'
    txt = Path(f'{out_base}.txt')
    if txt.is_file():
        try:
            copy_to_clipboard(txt.read_text(encoding='utf-8'))
        except (OSError, subprocess.CalledProcessError):
            pass
        else:
            print('[*] Transcript copied to clipboard')
    print(f'\n[✓] Transcription completed successfully!\n\n=== Input ===\nOriginal audio : {audio}\nReferenced audio: {source}\nNormalized     : {meeting_dir / (stem + "_norm16k.wav")}\n\n=== Output Files ===\nMeeting folder : {meeting_dir}\nText transcript: {out_base}.txt\nSRT subtitles  : {out_base}.srt\nJSON data      : {out_base}.json\nConfig used    : {REPO_ROOT / ".env"}')
    try:
        print('\n[*] Opening output folder...')
        show_in_folder(meeting_dir)
    except (OSError, subprocess.CalledProcessError):
        pass
    return 0


def _version_key(path: Path) -> list[tuple[int, object]]:
    # GNU sort -V's important segment behavior: numeric runs compare by value,
    # then by leading zero count. Names without digits stay lexical.
    parts = re.findall(r'\d+|\D+', str(path))
    return [(1, (int(p), -len(p))) if p.isdigit() else (0, p) for p in parts]


def multi_lang(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    config = load_workflow_config()
    if not args:
        name = os.getenv('MEETING_WORKFLOW_INVOKED_AS') or sys.argv[0]
        print(f'Usage: {name} <segments_folder>\n\nExample:\n  {name} ~/MeetingRecords/meeting_20251009_172145/\n\nThe folder should contain pre-split audio files:\n  segment_001.wav\n  segment_002.wav\n  ...')
        return 1
    segments_dir = clean_input_path(args[0])
    if not segments_dir.is_dir():
        print(f'[!] Error: Segments folder not found: {segments_dir}')
        return 1
    print(f'[*] Configuration:\n    Whisper root: {config.whisper_root}\n    Model: {config.model_file.name}\n    Language: {config.language}\n    Threads: {config.threads}\n    Segments folder: {segments_dir}\n')
    segments = sorted((p for p in segments_dir.glob('segment_*.wav') if p.is_file()), key=_version_key)
    if not segments:
        print(f'[!] No segment files found in {segments_dir}\n    Looking for files matching: segment_*.wav')
        return 1
    print(f'[*] Found {len(segments)} segment(s) to transcribe\n')
    out_dir = segments_dir / 'transcripts'
    out_dir.mkdir(parents=True, exist_ok=True)
    failure_log = segments_dir / 'failed_segments.log'
    failure_log.write_text('', encoding='utf-8')
    print(f'[*] Transcripts will be saved to: {out_dir}\n')
    failures = []
    for index, segment in enumerate(segments, 1):
        stem = segment.stem
        segment_log = out_dir / f'{stem}.whisper.log'
        print(f'[{index}/{len(segments)}] Transcribing: {stem}')
        for ext in ('.txt', '.srt', '.vtt', '.json', '.whisper.log'):
            (out_dir / f'{stem}{ext}').unlink(missing_ok=True)
        args = _transcription_args(config, segment, out_dir, stem, 'txt,srt')
        args += ['--artifact-basename', stem, '--no-normalize', '--quiet-progress', '--stream-subprocess', '--ffmpeg', 'ffmpeg']
        with segment_log.open('w', encoding='utf-8') as log, contextlib.redirect_stdout(log), contextlib.redirect_stderr(log):
            status = transcribe_main(args)
        if status:
            failures.append(stem)
            print(f'    ✗ Failed (exit {status})')
            with failure_log.open('a', encoding='utf-8') as log:
                log.write(stem + '\n')
            for ext in ('.txt', '.srt', '.vtt', '.json'):
                (out_dir / f'{stem}{ext}').unlink(missing_ok=True)
        else:
            print('    ✓ Success')
        print()
    print(f'========================================\nTranscription Complete\n========================================\nTotal segments: {len(segments)}\nSuccess: {len(segments)-len(failures)}\nFailed: {len(failures)}\n\nOutput location: {out_dir}\n')
    if failures:
        print(f'[!] Some segments failed. Kept failed audio inputs and wrote:\n    {failure_log}\nFailed segments:')
        print('\n'.join(failures))
        return 1
    failure_log.unlink(missing_ok=True)
    print('[✓] All segments transcribed successfully.')
    return 0


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if not args or args[0] not in {'transcribe-english', 'multi-lang'}:
        print('Usage: python -m src.workflow_cli {transcribe-english|multi-lang} [segments_folder]')
        return 1
    try:
        return transcribe_english(args[1:]) if args[0] == 'transcribe-english' else multi_lang(args[1:])
    except (OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
