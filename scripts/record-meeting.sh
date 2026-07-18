#!/usr/bin/env bash
# Meeting Assist: record with FFmpeg, then transcribe with whisper.cpp.
# Configuration comes from the repository .env file.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
source "${SCRIPT_DIR}/lib/common.sh"

load_project_env "${REPO_ROOT}/.env"
resolve_workflow_paths

AUDIO_CHANNELS="1"
AUDIO_SAMPLE_RATE="16000"

echo "[*] Configuration summary:"
echo "    Whisper root: $WHISPER_ROOT"
echo "    Output dir: $MEETING_RECORDS_DIR"
echo "    Mic device: $MIC_DEVICE"
echo "    Preferred model: $PREFERRED_MODEL"
echo "    Language: $DEFAULT_LANGUAGE"
echo "    Threads: $THREADS"
echo

mkdir -p "$MEETING_RECORDS_DIR"

ts="$(date +'%Y%m%d_%H%M%S')"
wav="${MEETING_RECORDS_DIR}/meeting_${ts}.wav"
ffmpeg_log="${MEETING_RECORDS_DIR}/ffmpeg_${ts}.log"

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
echo "[*] Starting transcription..."

base="${MEETING_RECORDS_DIR}/meeting_${ts}"

"$WHISPER_CLI" \
    -m "$MODEL_FILE" \
    -f "$wav" \
    --language "$DEFAULT_LANGUAGE" \
    --threads "$THREADS" \
    --output-txt --output-srt \
    --output-file "$base"

echo
echo "[✓] Transcription complete!"
echo
echo "=== Output Files ==="
echo "Audio : $wav"
echo "Text  : ${base}.txt"
echo "SRT   : ${base}.srt"
echo "Log   : ${ffmpeg_log}"
echo "Config: ${REPO_ROOT}/.env"
