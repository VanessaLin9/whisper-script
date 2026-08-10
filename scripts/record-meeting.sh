#!/usr/bin/env bash
# Meeting Assist: record with FFmpeg, retain the raw capture in a meeting
# workspace, then derive a normalized input for transcription.
# Configuration comes from the repository .env file.
#
# Microphone capture and Ctrl+C stay in this shell. Workspace ownership and
# transcription are delegated to the shared organizer/core.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
source "${SCRIPT_DIR}/lib/common.sh"

load_project_env "${REPO_ROOT}/.env"
resolve_workflow_paths

AUDIO_CHANNELS="${RECORDING_CHANNELS:-1}"
AUDIO_SAMPLE_RATE="${RECORDING_SAMPLE_RATE:-48000}"

echo "[*] Configuration summary:"
echo "    Whisper root: $WHISPER_ROOT"
echo "    Output dir: $MEETING_RECORDS_DIR"
echo "    Mic device: $MIC_DEVICE"
echo "    Preferred model: $PREFERRED_MODEL"
echo "    Language: $DEFAULT_LANGUAGE"
echo "    Threads: $THREADS"
echo

STAGING_DIR="${MEETING_RECORDS_DIR}/.incoming"
mkdir -p "$STAGING_DIR"

ts="$(date +'%Y%m%d_%H%M%S')"
stem="meeting_${ts}"
wav="${STAGING_DIR}/${stem}.wav"
ffmpeg_log="${STAGING_DIR}/ffmpeg_${ts}.log"

echo "[*] Available audio devices:"
ffmpeg -f avfoundation -list_devices true -i "" 2>&1 | sed 's/^/[ffmpeg] /' || true
echo

echo "[*] Starting recording to: $wav"
echo "[*] Using transcription model: $(basename "$MODEL_FILE")"
echo "[*] Audio settings: ${AUDIO_CHANNELS} channel(s), ${AUDIO_SAMPLE_RATE}Hz"
echo "[*] Press Ctrl+C to stop recording and start transcription..."
echo

RECORDING_INTERRUPTED=0
FF_PID=""

on_record_interrupt() {
    RECORDING_INTERRUPTED=1
    echo -e "\n[*] Stopping recording..."
    if [ -n "${FF_PID}" ]; then
        kill "${FF_PID}" 2>/dev/null || true
    fi
}

trap on_record_interrupt INT

ffmpeg -f avfoundation -i "$MIC_DEVICE" \
    -ac "$AUDIO_CHANNELS" -ar "$AUDIO_SAMPLE_RATE" -c:a pcm_s16le "$wav" \
    >"$ffmpeg_log" 2>&1 &
FF_PID=$!

set +e
wait "$FF_PID"
FF_STATUS=$?
set -e
trap - INT

if [ "$RECORDING_INTERRUPTED" -eq 0 ] && [ "$FF_STATUS" -ne 0 ]; then
    echo "[!] FFmpeg recording failed (exit ${FF_STATUS})"
    echo "    See log: ${ffmpeg_log}"
    echo "    Transcription was not started."
    exit 1
fi

if [ ! -f "$wav" ] || [ ! -s "$wav" ]; then
    echo "[!] Recording file is missing or empty: $wav"
    echo "    See log: ${ffmpeg_log}"
    echo "    Transcription was not started."
    exit 1
fi

echo "[*] Recording stopped."
echo "[*] Recording saved: $wav"
echo "[*] Organizing recording into a meeting workspace..."

ORGANIZER="${SCRIPT_DIR}/organize_recording.py"
if [ ! -f "$ORGANIZER" ]; then
    echo "[!] Recording organizer not found: $ORGANIZER"
    exit 1
fi

set +e
ORGANIZED_JSON="$(
  PYTHONPATH="${REPO_ROOT}${PYTHONPATH:+:$PYTHONPATH}" \
    python3 "$ORGANIZER" "$wav" \
      --records-dir "$MEETING_RECORDS_DIR" \
      --retain-source \
      --yes
)"
ORGANIZER_STATUS=$?
set -e
if [ "$ORGANIZER_STATUS" -ne 0 ] || [ -z "$ORGANIZED_JSON" ]; then
    echo "[!] Failed to organize recording into a meeting folder."
    echo "    Audio and FFmpeg log were kept in: $STAGING_DIR"
    exit 1
fi
if ! MEETING_DIR="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["meeting_dir"])' <<< "$ORGANIZED_JSON")"; then
    echo "[!] Organizer returned invalid JSON."
    exit 1
fi
IN="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["audio_file"])' <<< "$ORGANIZED_JSON")"
stem="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["stem"])' <<< "$ORGANIZED_JSON")"
base="${MEETING_DIR}/${stem}"

# Keep the capture log beside the raw source and transcription artifacts.
cp "$ffmpeg_log" "${MEETING_DIR}/ffmpeg_${ts}.log"

DEFAULT_OUTPUTS="$(
  PYTHONPATH="${REPO_ROOT}${PYTHONPATH:+:$PYTHONPATH}" \
    python3 -c 'from src.output_manager import default_outputs_arg; print(default_outputs_arg())'
)"

echo "[*] Meeting folder: $MEETING_DIR"
echo "[*] Raw recording retained: $IN"
echo "[*] Starting transcription from raw recording..."

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
    --stream-subprocess \
    --ffmpeg ffmpeg
STATUS=$?
set -e

if [ "$STATUS" -ne 0 ]; then
    echo "[!] Transcription failed (see stage details above)."
    echo "    Raw audio was retained in the meeting workspace: $IN"
    echo "    Staging audio and log were kept in: $STAGING_DIR"
    exit "$STATUS"
fi

# The retained workspace copy is now authoritative. Remove only the staging
# copy created by this invocation so failed runs can still be retried.
rm -f "$wav" "$ffmpeg_log"

echo
echo "[✓] Transcription complete!"
echo
echo "=== Output Files ==="
echo "Meeting folder : $MEETING_DIR"
echo "Raw audio      : $IN"
echo "Normalized     : ${MEETING_DIR}/${stem}_norm16k.wav"
echo "Text           : ${base}_transcription.txt"
echo "SRT            : ${base}_transcription.srt"
echo "JSON           : ${base}_transcription.json"
echo "Log            : ${MEETING_DIR}/ffmpeg_${ts}.log"
echo "Config: ${REPO_ROOT}/.env"

if command -v open >/dev/null 2>&1; then
    echo
    echo "[*] Opening output folder..."
    open "$MEETING_DIR" >/dev/null 2>&1 || true
fi
