#!/usr/bin/env bash
set -euo pipefail

# =======================
# Transcribe a long English meeting audio (multi-accent)
# - Prompts for input file
# - Normalizes audio to 16kHz mono WAV
# - Uses Whisper.cpp small.en (fallback to base.en)
# - Outputs .txt .srt .vtt .json
# - Copies transcript to clipboard and opens folder
# =======================

# ---- Load environment variables from .env file ----
if [ -f ".env" ]; then
  set -a  # automatically export all variables
  source .env
  set +a  # turn off automatic export
fi

# ---- Configure your Whisper.cpp absolute path ----
WHISPER_ROOT="${WHISPER_ROOT:-/Users/samuelwong/whisper.cpp}"
BIN="$WHISPER_ROOT/build/bin"
MODELS_DIR="$WHISPER_ROOT/models"
MODEL_SMALL="$MODELS_DIR/ggml-small.en.bin"
MODEL_BASE="$MODELS_DIR/ggml-base.en.bin"

# ---- Output directory (change if you want) ----
OUTDIR="${TRANSCRIPTS_DIR:-$HOME/MeetingRecords/Transcripts}"
mkdir -p "$OUTDIR"

# ---- Threads ----
THREADS="$(sysctl -n hw.logicalcpu || echo 8)"

# ---- Ask for input audio path ----
read -r -p "Enter path to your meeting audio (e.g., ~/MeetingRecords/meeting_... .wav/.m4a/.mp3): " IN
IN="${IN/#\~/$HOME}"   # expand ~ if used

if [ ! -f "$IN" ]; then
  echo "[!] File not found: $IN"
  exit 1
fi

# ---- Print basic info ----
echo "[*] Input file: $IN"
if command -v ffprobe >/dev/null 2>&1; then
  echo "[*] Probing duration with ffprobe..."
 # 使用 ffprobe 取得音訊長度
  ffprobe -v error -show_entries format=duration -of default=noprint_wrappers=1:nokey=1 "$IN" || true
  # ffprobe 參數說明：
  #   -v error：只顯示錯誤訊息
  #   -show_entries format=duration：只顯示時長資訊
  #   -of default=noprint_wrappers=1:nokey=1：輸出格式設定
  # || true：如果命令失敗就執行 true（不讓腳本退出）  
fi

# ---- Pick model: prefer small.en, else base.en ----
MODEL=""
if [ -f "$MODEL_SMALL" ]; then
  MODEL="$MODEL_SMALL"
  echo "[*] Model: $(basename "$MODEL")"
elif [ -f "$MODEL_BASE" ]; then
  MODEL="$MODEL_BASE"
  echo "[i] small.en not found, fallback to: $(basename "$MODEL")"
else
  echo "[!] No model found. Please download at least one:"
  echo "    cd $WHISPER_ROOT && bash ./models/download-ggml-model.sh small.en"
  echo "    or: bash ./models/download-ggml-model.sh base.en"
  exit 1
fi

# ---- Ensure whisper-cli exists ----
if [ ! -x "$BIN/whisper-cli" ]; then
  echo "[!] whisper-cli not found at $BIN/whisper-cli"
  echo "    Build first:  cd $WHISPER_ROOT && cmake -B build && cmake --build build -j"
  exit 1
fi

# ---- Normalize: convert to 16kHz mono PCM WAV (better stability for long meetings) ----
# 取得輸入檔案的基本名稱（不含路徑）
base_in="$(basename "$IN")"
# 取得輸入檔案的基本名稱（不含路徑）
stem="${base_in%.*}"
# ${VAR%pattern}：從變數末尾移除符合 pattern 的最短字串
# %.*：移除最後一個點及其後的所有字元（即移除副檔名）

# 設定輸出檔案的基本路徑（不含副檔名）
NORM="$OUTDIR/${stem}_norm16k.wav"

echo "[*] Normalizing audio to 16kHz mono WAV -> $NORM"
# 使用 ffmpeg 將輸入檔案轉換為 16kHz mono PCM WAV
# -y：覆蓋輸出檔案（如果存在）
# -i：輸入檔案
# -ac 1：輸出通道數為 1（單聲道）
# -ar 16000：輸出採樣率為 16000
# -c:a pcm_s16le：輸出音訊格式為 PCM 16位元小端序
# "$NORM"：輸出檔案路徑
ffmpeg -y -i "$IN" -ac 1 -ar 16000 -c:a pcm_s16le "$NORM"

# ---- Transcribe ----
OUT_BASE="$OUTDIR/${stem}_final"
echo "[*] Transcribing with $(basename "$MODEL") ..."
"$BIN/whisper-cli" \
  -m "$MODEL" \
  -f "$NORM" \
  --language en \
  --threads "$THREADS" \
  --output-txt --output-srt --output-vtt --output-json \
  --output-file "$OUT_BASE"
# whisper-cli 參數說明：
#   -m：指定模型檔案
#   -f：指定輸入音訊檔案
#   --language：指定語言
#   --threads：使用的執行緒數量
#   --output-txt：輸出純文字檔
#   --output-srt：輸出字幕檔
#   --output-vtt：輸出 WebVTT 字幕檔
#   --output-json：輸出 JSON 檔
#   --output-file：輸出檔案的基本名稱（whisper 會自動加上 .txt, .srt 等副檔名）

# ---- Post actions ----
TXT="${OUT_BASE}.txt"
if [ -f "$TXT" ]; then
  # Copy to clipboard (macOS)
  # cat "$TXT" | pbcopy：將純文字檔內容複製到剪貼簿
  # || true：如果命令失敗就執行 true（不讓腳本退出）
  cat "$TXT" | pbcopy || true
fi

echo
echo "[✓] Done."
echo "Audio (normalized): $NORM"
echo "Transcript (txt):   ${OUT_BASE}.txt"
echo "Subtitle (srt):     ${OUT_BASE}.srt"
echo "Subtitle (vtt):     ${OUT_BASE}.vtt"
echo "JSON (rich):        ${OUT_BASE}.json"

# Open output folder in Finder
open "$OUTDIR" >/dev/null 2>&1 || true
# open：macOS 的開啟檔案/資料夾命令
# >/dev/null 2>&1：忽略所有輸出
# || true：如果開啟失敗也不影響腳本執行
