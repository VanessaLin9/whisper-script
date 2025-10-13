#!/usr/bin/env bash
# multi-lang.sh
# Batch transcribe pre-split audio segments using whisper.cpp multilingual model
# Usage: ./multi-lang.sh <segments_folder>

set -euo pipefail

# ====== 環境變數載入 ======

# 載入 .env 檔案（如果存在）
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ -f "$SCRIPT_DIR/.env" ]; then
    source "$SCRIPT_DIR/.env"
elif [ -f "$SCRIPT_DIR/../.env" ]; then
    source "$SCRIPT_DIR/../.env"
else
    echo "[!] .env file not found. Please configure your environment."
    exit 1
fi

# ====== 設定和預設值 ======

WHISPER_ROOT="${WHISPER_ROOT:-}"
PREFERRED_MODEL="${PREFERRED_MODEL:-small}"

# 展開環境變數
WHISPER_ROOT=$(eval echo "$WHISPER_ROOT")
PREFERRED_MODEL=$(eval echo "$PREFERRED_MODEL")

BIN="$WHISPER_ROOT/build/bin"
MODELS_DIR="$WHISPER_ROOT/models"
MODEL="$MODELS_DIR/ggml-${PREFERRED_MODEL}.bin"

# CPU 執行緒數
if [ -n "${THREADS:-}" ]; then
    THREADS="$THREADS"
else
    THREADS="$(sysctl -n hw.logicalcpu || echo 8)"
fi

# ====== 參數檢查 ======

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

# 清理路徑（移除引號和展開 ~）
SEGMENTS_DIR="${SEGMENTS_DIR//\'/}"
SEGMENTS_DIR="${SEGMENTS_DIR//\"/}"
SEGMENTS_DIR="${SEGMENTS_DIR/#\~/$HOME}"

# 檢查資料夾是否存在
if [ ! -d "$SEGMENTS_DIR" ]; then
    echo "[!] Error: Segments folder not found: $SEGMENTS_DIR"
    exit 1
fi

# ====== 環境檢查 ======

echo "[*] Configuration:"
echo "    Whisper root: $WHISPER_ROOT"
echo "    Model: $(basename "$MODEL")"
echo "    Threads: $THREADS"
echo "    Segments folder: $SEGMENTS_DIR"
echo

# 檢查 whisper-cli
if [ ! -x "$BIN/whisper-cli" ]; then
    echo "[!] whisper-cli not found at $BIN/whisper-cli"
    echo "    Please build whisper.cpp first:"
    echo "      cd $WHISPER_ROOT"
    echo "      cmake -B build && cmake --build build -j"
    exit 1
fi

# 檢查多語言模型
if [ ! -f "$MODEL" ]; then
    echo "[!] Multilingual model not found: $MODEL"
    echo "    Please download it:"
    echo "      cd $WHISPER_ROOT"
    echo "      bash ./models/download-ggml-model.sh ${PREFERRED_MODEL}"
    echo
    echo "    Note: Make sure to download the MULTILINGUAL version (not .en)"
    exit 1
fi

# ====== 尋找所有 segment 檔案 ======

# 尋找所有 segment_*.wav 檔案並排序（相容 Bash 3.2）
SEGMENTS=()
while IFS= read -r file; do
    SEGMENTS+=("$file")
done < <(find "$SEGMENTS_DIR" -name "segment_*.wav" -type f | sort -V)

# 檢查是否有檔案
if [ ${#SEGMENTS[@]} -eq 0 ]; then
    echo "[!] No segment files found in $SEGMENTS_DIR"
    echo "    Looking for files matching: segment_*.wav"
    exit 1
fi

echo "[*] Found ${#SEGMENTS[@]} segment(s) to transcribe"
echo

# ====== 建立輸出資料夾 ======

TRANSCRIPTS_DIR="$SEGMENTS_DIR/transcripts"
mkdir -p "$TRANSCRIPTS_DIR"

echo "[*] Transcripts will be saved to: $TRANSCRIPTS_DIR"
echo

# ====== 批次轉錄 ======

TOTAL=${#SEGMENTS[@]}
SUCCESS=0
FAILED=0

for i in "${!SEGMENTS[@]}"; do
    SEGMENT="${SEGMENTS[$i]}"
    SEGMENT_NAME="$(basename "$SEGMENT" .wav)"
    NUM=$((i + 1))
    
    echo "[$NUM/$TOTAL] Transcribing: $SEGMENT_NAME"
    
    # 輸出檔案基本路徑（到 transcripts 子資料夾）
    OUT_BASE="$TRANSCRIPTS_DIR/${SEGMENT_NAME}"
    
    # 執行轉錄
    if "$BIN/whisper-cli" \
        -m "$MODEL" \
        -f "$SEGMENT" \
        --language auto \
        --threads "$THREADS" \
        --output-txt \
        --output-srt \
        --output-file "$OUT_BASE" 2>&1 | grep -v "whisper_init_from_file"; then
        
        echo "    ✓ Success"
        ((SUCCESS++))
    else
        echo "    ✗ Failed"
        ((FAILED++))
        # 記錄錯誤但繼續處理
        echo "$SEGMENT_NAME" >> "$SEGMENTS_DIR/failed_segments.log"
    fi
    
    echo
done

# ====== 結果摘要 ======

echo "========================================"
echo "Transcription Complete"
echo "========================================"
echo "Total segments: $TOTAL"
echo "Success: $SUCCESS"
echo "Failed: $FAILED"
echo
echo "Output location: $TRANSCRIPTS_DIR"
echo

if [ $FAILED -gt 0 ]; then
    echo "[!] Some segments failed. Check: $SEGMENTS_DIR/failed_segments.log"
fi

# 列出產生的檔案
echo "Generated files:"
ls -1 "$TRANSCRIPTS_DIR"/*.txt 2>/dev/null | head -5
if [ $(ls -1 "$TRANSCRIPTS_DIR"/*.txt 2>/dev/null | wc -l) -gt 5 ]; then
    echo "... and more"
fi

echo
echo "[✓] Done!"