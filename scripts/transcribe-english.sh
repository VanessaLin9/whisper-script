#!/usr/bin/env bash
# Transcribe an existing audio file with the configured multilingual model.
# Filename is historical; language/model come from .env (default: zh / small).
# Outputs are organized into a per-meeting folder under MEETING_RECORDS_DIR.
#
# Interactive UX stays in this shell. Workspace ownership / artifact defaults
# come from Output Manager; normalize / whisper-cli run via the transcription core.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
source "${SCRIPT_DIR}/lib/common.sh"

load_project_env "${REPO_ROOT}/.env"
resolve_workflow_paths

mkdir -p "$MEETING_RECORDS_DIR"

DEFAULT_OUTPUTS="$(
  PYTHONPATH="${REPO_ROOT}${PYTHONPATH:+:$PYTHONPATH}" \
    python3 -c 'from src.output_manager import default_outputs_arg; print(default_outputs_arg())'
)"

echo "[*] Configuration summary:"
echo "    Whisper root: $WHISPER_ROOT"
echo "    Meeting records dir: $MEETING_RECORDS_DIR"
echo "    Preferred model: $PREFERRED_MODEL"
echo "    Language: $DEFAULT_LANGUAGE"
echo "    Threads: $THREADS"
echo "    Default outputs: $DEFAULT_OUTPUTS"
echo
echo "[*] Using model: $(basename "$MODEL_FILE")"

echo
read -r -p "Enter path to your meeting audio (drag & drop or type path): " IN

IN="${IN//\'/}"
IN="${IN//\"/}"
IN="${IN/#\~/$HOME}"

if [ ! -f "$IN" ]; then
    echo "[!] File not found: $IN"
    exit 1
fi

ORIGINAL_IN="$IN"
echo "[*] Input file: $ORIGINAL_IN"

# Plan/create meeting workspace. Local sources are referenced in place (no copy).
ORGANIZER="$SCRIPT_DIR/organize_recording.py"
if [ ! -f "$ORGANIZER" ]; then
    echo "[!] Recording organizer not found: $ORGANIZER"
    exit 1
fi

set +e
ORGANIZED_JSON="$(
  PYTHONPATH="${REPO_ROOT}${PYTHONPATH:+:$PYTHONPATH}" \
    python3 "$ORGANIZER" "$ORIGINAL_IN" --records-dir "$MEETING_RECORDS_DIR"
)"
ORGANIZER_STATUS=$?
set -e
if [ "$ORGANIZER_STATUS" -ne 0 ] || [ -z "$ORGANIZED_JSON" ]; then
    echo "[!] Failed to organize recording into a meeting folder."
    exit 1
fi
if ! MEETING_DIR="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["meeting_dir"])' <<< "$ORGANIZED_JSON")"; then
    echo "[!] Organizer returned invalid JSON."
    exit 1
fi
IN="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["audio_file"])' <<< "$ORGANIZED_JSON")"
stem="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["stem"])' <<< "$ORGANIZED_JSON")"

echo "[*] Meeting folder: $MEETING_DIR"
echo "[*] Source audio (referenced): $IN"

if command -v ffprobe >/dev/null 2>&1; then
    echo "[*] Probing duration with ffprobe..."
    ffprobe -v error -show_entries format=duration -of default=noprint_wrappers=1:nokey=1 "$IN" || true
fi

NORM="${MEETING_DIR}/${stem}_norm16k.wav"
OUT_BASE="${MEETING_DIR}/${stem}_transcription"

echo "[*] Starting transcription with $(basename "$MODEL_FILE")..."
echo "    This may take a while for long recordings..."

set +e
PYTHONPATH="${REPO_ROOT}${PYTHONPATH:+:$PYTHONPATH}" python3 -m src.transcription.cli \
    --audio "$IN" \
    --output-dir "$MEETING_DIR" \
    --stem "$stem" \
    --language "$DEFAULT_LANGUAGE" \
    --model "$PREFERRED_MODEL" \
    --model-path "$MODEL_FILE" \
    --whisper-cli "$WHISPER_CLI" \
    --threads "$THREADS" \
    --outputs "$DEFAULT_OUTPUTS" \
    --normalize \
    --keep-normalized \
    --ffmpeg ffmpeg
STATUS=$?
set -e

if [ "$STATUS" -ne 0 ]; then
    echo "[!] Transcription failed (see stage details above)."
    exit "$STATUS"
fi

TXT="${OUT_BASE}.txt"

if [ -f "$TXT" ]; then
    if command -v pbcopy >/dev/null 2>&1; then
        cat "$TXT" | pbcopy
        echo "[*] Transcript copied to clipboard"
    fi
fi

echo
echo "[✓] Transcription completed successfully!"
echo
echo "=== Input ==="
echo "Original audio : $ORIGINAL_IN"
echo "Referenced audio: $IN"
echo "Normalized     : $NORM"
echo
echo "=== Output Files ==="
echo "Meeting folder : $MEETING_DIR"
echo "Text transcript: ${OUT_BASE}.txt"
echo "SRT subtitles  : ${OUT_BASE}.srt"
echo "JSON data      : ${OUT_BASE}.json"
echo "Config used    : ${REPO_ROOT}/.env"

if command -v open >/dev/null 2>&1; then
    echo
    echo "[*] Opening output folder..."
    open "$MEETING_DIR" >/dev/null 2>&1 || true
fi
