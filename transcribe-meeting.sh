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

# ---- Configure your Whisper.cpp absolute path ----
WHISPER_ROOT="/Users/samuelwong/whisper.cpp"
BIN="$WHISPER_ROOT/build/bin"
MODELS_DIR="$WHISPER_ROOT/models"
MODEL_SMALL="$MODELS_DIR/ggml-small.en.bin"
MODEL_BASE="$MODELS_DIR/ggml-base.en.bin"

# ---- Output directory (change if you want) ----
OUTDIR="$HOME/MeetingRecords/Transcripts"
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
  ffprobe -v error -show_entries format=duration -of default=noprint_wrappers=1:nokey=1 "$IN" || true
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
base_in="$(basename "$IN")"
stem="${base_in%.*}"
NORM="$OUTDIR/${stem}_norm16k.wav"

echo "[*] Normalizing audio to 16kHz mono WAV -> $NORM"
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

# ---- Post actions ----
TXT="${OUT_BASE}.txt"
if [ -f "$TXT" ]; then
  # Copy to clipboard (macOS)
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
