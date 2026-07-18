#!/usr/bin/env bash
# Batch transcribe pre-split audio segments using whisper.cpp.
# Usage: ./multi-lang.sh <segments_folder>

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
source "${SCRIPT_DIR}/lib/common.sh"

load_project_env "${REPO_ROOT}/.env"
resolve_workflow_paths

if [ $# -lt 1 ]; then
    echo "Usage: $0 <segments_folder>"
    echo
    echo "Example:"
    echo "  $0 ~/MeetingRecords/meeting_20251009_172145/"
    echo
    echo "The folder should contain pre-split audio files:"
    echo "  segment_001.wav"
    echo "  segment_002.wav"
    echo "  ..."
    exit 1
fi

SEGMENTS_DIR="$1"
SEGMENTS_DIR="${SEGMENTS_DIR//\'/}"
SEGMENTS_DIR="${SEGMENTS_DIR//\"/}"
SEGMENTS_DIR="${SEGMENTS_DIR/#\~/$HOME}"

if [ ! -d "$SEGMENTS_DIR" ]; then
    echo "[!] Error: Segments folder not found: $SEGMENTS_DIR"
    exit 1
fi

echo "[*] Configuration:"
echo "    Whisper root: $WHISPER_ROOT"
echo "    Model: $(basename "$MODEL_FILE")"
echo "    Language: $DEFAULT_LANGUAGE"
echo "    Threads: $THREADS"
echo "    Segments folder: $SEGMENTS_DIR"
echo

SEGMENTS=()
while IFS= read -r file; do
    SEGMENTS+=("$file")
done < <(find "$SEGMENTS_DIR" -maxdepth 1 -name "segment_*.wav" -type f | sort -V)

if [ ${#SEGMENTS[@]} -eq 0 ]; then
    echo "[!] No segment files found in $SEGMENTS_DIR"
    echo "    Looking for files matching: segment_*.wav"
    exit 1
fi

echo "[*] Found ${#SEGMENTS[@]} segment(s) to transcribe"
echo

OUT_DIR="${SEGMENTS_DIR}/transcripts"
mkdir -p "$OUT_DIR"

FAILURE_LOG="${SEGMENTS_DIR}/failed_segments.log"
: >"$FAILURE_LOG"

echo "[*] Transcripts will be saved to: $OUT_DIR"
echo

TOTAL=${#SEGMENTS[@]}
SUCCESS=0
FAILED=0

for i in "${!SEGMENTS[@]}"; do
    SEGMENT="${SEGMENTS[$i]}"
    SEGMENT_NAME="$(basename "$SEGMENT" .wav)"
    NUM=$((i + 1))
    OUT_BASE="${OUT_DIR}/${SEGMENT_NAME}"
    SEGMENT_LOG="${OUT_DIR}/${SEGMENT_NAME}.whisper.log"

    echo "[$NUM/$TOTAL] Transcribing: $SEGMENT_NAME"

    # Clear prior outputs for this segment so a failed rerun cannot leave stale
    # transcripts that look like current results. Keep the input WAV.
    rm -f \
        "${OUT_BASE}.txt" \
        "${OUT_BASE}.srt" \
        "${OUT_BASE}.vtt" \
        "${OUT_BASE}.json" \
        "$SEGMENT_LOG"

    set +e
    "$WHISPER_CLI" \
        -m "$MODEL_FILE" \
        -f "$SEGMENT" \
        --language "$DEFAULT_LANGUAGE" \
        --threads "$THREADS" \
        --output-txt \
        --output-srt \
        --output-file "$OUT_BASE" >"$SEGMENT_LOG" 2>&1
    STATUS=$?
    set -e

    if [ "$STATUS" -eq 0 ]; then
        echo "    ✓ Success"
        SUCCESS=$((SUCCESS + 1))
    else
        echo "    ✗ Failed (exit ${STATUS})"
        FAILED=$((FAILED + 1))
        echo "$SEGMENT_NAME" >>"$FAILURE_LOG"
        # Drop any partial outputs from the failed attempt; keep input audio.
        rm -f \
            "${OUT_BASE}.txt" \
            "${OUT_BASE}.srt" \
            "${OUT_BASE}.vtt" \
            "${OUT_BASE}.json"
    fi

    echo
done

echo "========================================"
echo "Transcription Complete"
echo "========================================"
echo "Total segments: $TOTAL"
echo "Success: $SUCCESS"
echo "Failed: $FAILED"
echo
echo "Output location: $OUT_DIR"
echo

if [ "$FAILED" -gt 0 ]; then
    echo "[!] Some segments failed. Kept failed audio inputs and wrote:"
    echo "    $FAILURE_LOG"
    echo "Failed segments:"
    cat "$FAILURE_LOG"
    exit 1
fi

rm -f "$FAILURE_LOG"

echo "[✓] All segments transcribed successfully."
