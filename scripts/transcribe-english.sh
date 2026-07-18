#!/usr/bin/env bash
# Transcribe an existing audio file with the configured multilingual model.
# Filename is historical; language/model come from .env (default: zh / small).
# Outputs are organized into a per-meeting folder under MEETING_RECORDS_DIR.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
source "${SCRIPT_DIR}/lib/common.sh"

load_project_env "${REPO_ROOT}/.env"
resolve_workflow_paths

mkdir -p "$MEETING_RECORDS_DIR"

echo "[*] Configuration summary:"
echo "    Whisper root: $WHISPER_ROOT"
echo "    Meeting records dir: $MEETING_RECORDS_DIR"
echo "    Preferred model: $PREFERRED_MODEL"
echo "    Language: $DEFAULT_LANGUAGE"
echo "    Threads: $THREADS"
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

# Organize into a per-meeting folder before transcription. The original input
# file is preserved; the helper copies a timestamped version into the folder.
ORGANIZER="$SCRIPT_DIR/organize_recording.py"
if [ ! -f "$ORGANIZER" ]; then
    echo "[!] Recording organizer not found: $ORGANIZER"
    exit 1
fi

ORGANIZED_JSON="$(python3 "$ORGANIZER" "$ORIGINAL_IN" --records-dir "$MEETING_RECORDS_DIR")"
MEETING_DIR="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["meeting_dir"])' <<< "$ORGANIZED_JSON")"
IN="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["audio_file"])' <<< "$ORGANIZED_JSON")"
stem="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["stem"])' <<< "$ORGANIZED_JSON")"

echo "[*] Meeting folder: $MEETING_DIR"
echo "[*] Standard audio: $IN"

if command -v ffprobe >/dev/null 2>&1; then
    echo "[*] Probing duration with ffprobe..."
    ffprobe -v error -show_entries format=duration -of default=noprint_wrappers=1:nokey=1 "$IN" || true
fi

NORM="${MEETING_DIR}/${stem}_norm16k.wav"
OUT_BASE="${MEETING_DIR}/${stem}_transcription"

# Refuse to overwrite existing artifacts before ffmpeg uses -y.
for output in "$NORM" "${OUT_BASE}.txt" "${OUT_BASE}.srt" "${OUT_BASE}.vtt" "${OUT_BASE}.json"; do
    if [ -e "$output" ]; then
        echo "[!] Output already exists; refusing to overwrite: $output"
        exit 1
    fi
done

echo "[*] Normalizing audio to 16kHz mono WAV -> $NORM"
ffmpeg -y -i "$IN" -ac 1 -ar 16000 -c:a pcm_s16le "$NORM"

echo "[*] Starting transcription with $(basename "$MODEL_FILE")..."
echo "    This may take a while for long recordings..."

"$WHISPER_CLI" \
    -m "$MODEL_FILE" \
    -f "$NORM" \
    --language "$DEFAULT_LANGUAGE" \
    --threads "$THREADS" \
    --output-txt --output-srt --output-vtt --output-json \
    --output-file "$OUT_BASE"

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
echo "Meeting audio  : $IN"
echo "Normalized     : $NORM"
echo
echo "=== Output Files ==="
echo "Meeting folder : $MEETING_DIR"
echo "Text transcript: ${OUT_BASE}.txt"
echo "SRT subtitles  : ${OUT_BASE}.srt"
echo "VTT subtitles  : ${OUT_BASE}.vtt"
echo "JSON data      : ${OUT_BASE}.json"
echo "Config used    : ${REPO_ROOT}/.env"

if command -v open >/dev/null 2>&1; then
    echo
    echo "[*] Opening output folder..."
    open "$MEETING_DIR" >/dev/null 2>&1 || true
fi
