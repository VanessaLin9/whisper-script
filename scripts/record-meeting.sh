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

# Raw capture 預設 48 kHz；Whisper 輸入另由 core normalize 成 16 kHz mono（PR #10）。
AUDIO_CHANNELS="${RECORDING_CHANNELS:-1}"
AUDIO_SAMPLE_RATE="${RECORDING_SAMPLE_RATE:-48000}"
RECORDING_SOURCE="${RECORDING_SOURCE:-mic}"
case "$RECORDING_SOURCE" in
    mic|system|both) ;;
    *) echo "[!] RECORDING_SOURCE must be mic, system, or both"; exit 1 ;;
esac
if [ -t 0 ]; then
    echo "錄音來源：1) 麥克風  2) 系統音（耳機裡的聲音）  3) 系統音＋麥克風"
    read -r -p "選擇來源 [預設 ${RECORDING_SOURCE}]：" source_choice
    case "$source_choice" in
        1|mic) RECORDING_SOURCE=mic ;;
        2|system) RECORDING_SOURCE=system ;;
        3|both) RECORDING_SOURCE=both ;;
        "") ;;
        *) echo "[!] 無效的來源；未開始錄音。"; exit 1 ;;
    esac
fi
echo "[*] 請確認會議場合允許錄音，並視需要告知與會者。"
echo "[*] Selected source: $RECORDING_SOURCE (no automatic detection or fallback)"
if [ "$RECORDING_SOURCE" = mic ]; then
    echo "[!] 麥克風模式不會直接錄到耳機裡的遠端聲音。"
else
    echo "[*] 只擷取音訊，不保存螢幕畫面；開始前安靜不會阻止錄音。"
    RECORDER="${REPO_ROOT}/.local/recorder/meeting-recorder"
    if [ ! -x "$RECORDER" ] || [ "${REPO_ROOT}/native/recorder/MeetingRecorder.swift" -nt "$RECORDER" ] || [ "${REPO_ROOT}/native/recorder/Info.plist" -nt "$RECORDER" ] || [ "${SCRIPT_DIR}/build-recorder.sh" -nt "$RECORDER" ]; then
        bash "${SCRIPT_DIR}/build-recorder.sh"
    fi
fi

echo "[*] Configuration summary:"
echo "    Whisper root: $WHISPER_ROOT"
echo "    Output dir: $MEETING_RECORDS_DIR"
if [ "$RECORDING_SOURCE" = mic ]; then
    echo "    FFmpeg mic device: $MIC_DEVICE"
elif [ "$RECORDING_SOURCE" = both ]; then
    echo "    Native mic: ${RECORDING_MIC_DEVICE_ID:-macOS default input}"
fi
echo "    Preferred model: $PREFERRED_MODEL"
echo "    Language: $DEFAULT_LANGUAGE"
echo "    Threads: $THREADS"
echo

# Staging 失敗保留；成功後只清本次 .incoming 暫存（PR #10）。
STAGING_DIR="${MEETING_RECORDS_DIR}/.incoming"
mkdir -p "$STAGING_DIR"

ts="$(TZ=Asia/Taipei date +'%Y%m%d_%H%M%S')"
recorded_at="$(TZ=Asia/Taipei date +'%Y-%m-%dT%H:%M:%S+08:00')"
stem="meeting_${ts}_$$"
wav="${STAGING_DIR}/${stem}.wav"
ffmpeg_log="${STAGING_DIR}/ffmpeg_${ts}_$$.log"
system_wav="${STAGING_DIR}/${stem}_system.wav"
mic_wav="${STAGING_DIR}/${stem}_microphone.wav"
capture_manifest="${STAGING_DIR}/${stem}_capture.json"
capture_info="${STAGING_DIR}/${stem}_info.json"

if [ "$RECORDING_SOURCE" = mic ]; then
    echo "[*] Available audio devices:"
    ffmpeg -f avfoundation -list_devices true -i "" 2>&1 | sed 's/^/[ffmpeg] /' || true
fi
echo

echo "[*] Starting recording to: $wav"
echo "[*] Using transcription model: $(basename "$MODEL_FILE")"
if [ "$RECORDING_SOURCE" = mic ]; then
    echo "[*] Audio settings: ${AUDIO_CHANNELS} channel(s), ${AUDIO_SAMPLE_RATE}Hz"
else
    echo "[*] Audio settings: 48 kHz mono PCM16 per track (native capture)"
fi
echo "[*] Press Ctrl+C to stop recording and start transcription..."
echo

RECORDING_INTERRUPTED=0
RECORDING_ABORTED=0
FF_PID=""
LOG_PID=""

on_record_interrupt() {
    RECORDING_INTERRUPTED=1
    echo -e "\n[*] Stopping recording..."
    if [ -n "${FF_PID}" ]; then
        # Background jobs may inherit SIGINT ignored. SIGTERM is handled by
        # the native recorder and lets FFmpeg finalize its WAV header too.
        kill -TERM "${FF_PID}" 2>/dev/null || true
    fi
}

trap on_record_interrupt INT
trap 'RECORDING_ABORTED=1; on_record_interrupt' TERM

if [ "$RECORDING_SOURCE" = mic ]; then
    ffmpeg -nostdin -n -f avfoundation -i "$MIC_DEVICE" \
        -ac "$AUDIO_CHANNELS" -ar "$AUDIO_SAMPLE_RATE" -c:a pcm_s16le "$wav" \
        >"$ffmpeg_log" 2>&1 &
    FF_PID=$!
else
    capture_args=(--source "$RECORDING_SOURCE" --output "$system_wav" --metadata "$capture_info")
    if [ "$RECORDING_SOURCE" = both ]; then
        capture_args+=(--mic-output "$mic_wav")
        if [ -n "${RECORDING_MIC_DEVICE_ID:-}" ]; then
            capture_args+=(--mic-device-id "$RECORDING_MIC_DEVICE_ID")
        fi
    fi
    "$RECORDER" "${capture_args[@]}" >"$ffmpeg_log" 2>&1 &
    FF_PID=$!
    # Follow diagnostics without putting the recorder in a pipeline (its PID
    # must remain the signal target, and /dev/fd is unavailable in some sandboxes).
    tail -n +1 -f "$ffmpeg_log" &
    LOG_PID=$!
fi

set +e
wait "$FF_PID"
FF_STATUS=$?
# An interrupt can end bash's wait before the child has closed the WAV.
while kill -0 "$FF_PID" 2>/dev/null; do
    wait "$FF_PID"
    FF_STATUS=$?
done
set -e
trap - INT TERM
if [ -n "$LOG_PID" ]; then
    kill "$LOG_PID" 2>/dev/null || true
    wait "$LOG_PID" 2>/dev/null || true
fi
if [ "$RECORDING_ABORTED" -eq 1 ]; then
    echo "[!] Recording terminated. Capture files/log kept: $STAGING_DIR"
    exit 143
fi
if [ "$RECORDING_SOURCE" != mic ]; then
    cat "$ffmpeg_log"
    if [ "$FF_STATUS" -ne 0 ]; then
        echo "[!] Native capture failed (exit $FF_STATUS); files/log kept: $STAGING_DIR"
        exit 1
    fi
    prepare_args=(prepare --system "$system_wav" --output "$wav" --manifest "$capture_manifest" --capture-info "$capture_info"
        --system-gain "${RECORDING_SYSTEM_GAIN:-1}" --mic-gain "${RECORDING_MIC_GAIN:-1}")
    if [ "$RECORDING_SOURCE" = both ]; then prepare_args+=(--microphone "$mic_wav"); fi
    python3 "${SCRIPT_DIR}/prepare_capture.py" "${prepare_args[@]}" >>"$ffmpeg_log" 2>&1 || {
        cat "$ffmpeg_log"
        exit 1
    }
    recorded_at="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["recorded_at"])' "$capture_manifest")"
fi

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
      --recorded-at "$recorded_at" \
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
if [ "$RECORDING_SOURCE" != mic ]; then
    python3 "${SCRIPT_DIR}/prepare_capture.py" retain \
        --manifest "$capture_manifest" --workspace "$MEETING_DIR" \
        --stem "$stem" --recorded-at "$recorded_at"
fi

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
    --ffmpeg ffmpeg </dev/null
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
if [ "$RECORDING_SOURCE" != mic ]; then
    rm -f "$system_wav" "$mic_wav" "$capture_manifest" "$capture_info"
fi

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
