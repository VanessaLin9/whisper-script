#!/usr/bin/env bash
set -euo pipefail

###############################################################################
# Meeting Assist (chunked near-live captions + full recording + post transcript)
# - Live captions: base.en (較快)
# - Final transcript: small.en (較準；若不存在則自動退回 base.en)
#
# How to run:
#   meeting-assist-chunked.sh
#
# Output folder:
#   ~/MeetingRecords
###############################################################################

# ====== Paths (請確認你的 whisper.cpp 路徑正確) ======
WHISPER_ROOT="${WHISPER_ROOT:-/Users/samuelwong/whisper.cpp}"   # 你的 whisper.cpp 根目錄
BIN="$WHISPER_ROOT/build/bin"                                   # 可執行檔位置
MODELS_DIR="$WHISPER_ROOT/models"                               # 模型位置

# 會中即時字幕模型（快）：base.en
LIVE_MODEL="$MODELS_DIR/ggml-base.en.bin"
# 會後完整逐字稿模型（準）：small.en（若找不到就退回 LIVE_MODEL）
FINAL_MODEL="$MODELS_DIR/ggml-small.en.bin"

# ====== Audio / System ======
MIC=":0"                       # 用 ffmpeg -list_devices 找到的音訊裝置，預設 :0 為 Mac 內建麥克風
OUTDIR="$HOME/MeetingRecords"  # 輸出根資料夾
SEG_SEC=5                      # 每段切 5 秒（越小越即時，但更吃 CPU / IO）
THREADS="$(sysctl -n hw.logicalcpu || echo 8)"

# ====== Sanity checks ======
if [ ! -x "$BIN/whisper-cli" ]; then
  echo "[!] Cannot find executable: $BIN/whisper-cli"
  echo "    Please build whisper.cpp first:  (in $WHISPER_ROOT)"
  echo "      cmake -B build -DWHISPER_PORTAUDIO=OFF && cmake --build build -j"
  exit 1
fi

if [ ! -f "$LIVE_MODEL" ]; then
  echo "[!] LIVE_MODEL not found: $LIVE_MODEL"
  echo "    Run: bash $MODELS_DIR/../models/download-ggml-model.sh base.en"
  exit 1
fi

if [ ! -f "$FINAL_MODEL" ]; then
  echo "[i] FINAL_MODEL not found: $FINAL_MODEL"
  echo "    Will fallback to LIVE_MODEL for final transcription."
  FINAL_MODEL="$LIVE_MODEL"
fi

# ====== Prep output ======
mkdir -p "$OUTDIR"
ts="$(date +'%Y%m%d_%H%M%S')"
wav="$OUTDIR/meeting_${ts}.wav"               # 連續完整錄音
segdir="$OUTDIR/meeting_${ts}_chunks"         # 分段檔案資料夾
mkdir -p "$segdir"
touch "$segdir/.processed"                    # 已處理清單

echo "[*] Device list (for reference):"
ffmpeg -f avfoundation -list_devices true -i "" 2>&1 | sed 's/^/[ffmpeg] /' || true
echo

# ====== Start recorders ======
echo "[*] Start FULL recording to: $wav"
ffmpeg -f avfoundation -i "$MIC" -ac 1 -ar 16000 -c:a pcm_s16le "$wav" \
  > "$OUTDIR/ffmpeg_full_${ts}.log" 2>&1 &
FF_FULL_PID=$!

echo "[*] Start SEGMENTED recording (every ${SEG_SEC}s) to: $segdir"
ffmpeg -f avfoundation -i "$MIC" -ac 1 -ar 16000 -c:a pcm_s16le \
  -f segment -segment_time "$SEG_SEC" -reset_timestamps 1 \
  "$segdir/seg_%06d.wav" \
  > "$OUTDIR/ffmpeg_seg_${ts}.log" 2>&1 &
FF_SEG_PID=$!

# ====== Live-ish captions loop (near real-time) ======
echo "[*] Live captions (model: $(basename "$LIVE_MODEL")) ...  Press Ctrl+C to stop."
trap 'echo "[*] Stopping..."; kill $FF_SEG_PID $FF_FULL_PID 2>/dev/null || true' INT

while sleep 0.5; do
  for f in "$segdir"/seg_*.wav; do
    [ -e "$f" ] || continue
    bn="$(basename "$f")"
    if ! grep -q "^$bn$" "$segdir/.processed" 2>/dev/null; then
      echo "$bn" >> "$segdir/.processed"
      (
        echo -n "[${bn}] "
        "$BIN/whisper-cli" \
          -m "$LIVE_MODEL" \
          -f "$f" \
          --threads "$THREADS" \
          --print-colors \
          --no-timestamps \
          --no-gpu \
          2>/dev/null | sed "s/^/[${bn}] /"
      ) &
    fi
  done
done

# ====== Cleanup after Ctrl+C ======
wait $FF_SEG_PID 2>/dev/null || true
wait $FF_FULL_PID 2>/dev/null || true

# ====== Final full transcription (更準的 small.en；無則退回 base.en) ======
echo "[*] Transcribing FULL recording with: $(basename "$FINAL_MODEL")"
base="$OUTDIR/meeting_${ts}"
"$BIN/whisper-cli" \
  -m "$FINAL_MODEL" \
  -f "$wav" \
  --threads "$THREADS" \
  --output-txt --output-srt \
  --output-file "$base"

echo
echo "[✓] Done."
echo "Audio : $wav"
echo "Text  : ${base}.txt"
echo "SRT   : ${base}.srt"
echo "Chunks: $segdir"

