#!/usr/bin/env bash
# ↑ shebang：告訴系統用 bash 來執行這個腳本

set -euo pipefail
# 設定腳本執行的嚴格模式

# =======================
# 手動轉錄長時間英語會議音訊（支援多口音）- 環境變數版本
# - 提示輸入音訊檔案路徑
# - 將音訊正規化為指定格式的 WAV
# - 使用 Whisper.cpp 模型進行轉錄
# - 輸出多種格式：.txt .srt .vtt .json
# - 複製轉錄文字到剪貼簿並開啟輸出資料夾
# - 使用 .env 檔案管理設定
# =======================

# ====== 環境變數載入區塊 ======

# 載入 .env 檔案（如果存在）
if [ -f ".env" ]; then
    set -a  # 自動匯出所有變數
    source .env
    set +a  # 關閉自動匯出
    echo "[*] Loaded configuration from .env"
else
    echo "[!] .env file not found. Please copy .env.sample to .env and configure your paths."
    echo "    cp .env.sample .env"
    echo "    nano .env  # 編輯設定檔"
    exit 1
fi

# ====== 設定檢查和預設值 ======

# 檢查必要的環境變數並設定預設值
WHISPER_ROOT="${WHISPER_ROOT:-/Users/samuelwong/whisper.cpp}"
MEETING_RECORDS_DIR="${MEETING_RECORDS_DIR:-$HOME/MeetingRecords}"
TRANSCRIPTS_DIR="${TRANSCRIPTS_DIR:-$HOME/MeetingRecords/Transcripts}"
DEFAULT_LANGUAGE="${DEFAULT_LANGUAGE:-en}"
PREFERRED_MODEL="${PREFERRED_MODEL:-small}"

# 展開環境變數（如果包含 $HOME）
WHISPER_ROOT=$(eval echo "$WHISPER_ROOT")
MEETING_RECORDS_DIR=$(eval echo "$MEETING_RECORDS_DIR")
TRANSCRIPTS_DIR=$(eval echo "$TRANSCRIPTS_DIR")

# ====== 路徑建構區塊 ======

BIN="$WHISPER_ROOT/build/bin"
MODELS_DIR="$WHISPER_ROOT/models"
MODEL_PREFERRED="$MODELS_DIR/ggml-${PREFERRED_MODEL}.en.bin"
MODEL_BASE="$MODELS_DIR/ggml-base.en.bin"

# 建立輸出目錄
mkdir -p "$TRANSCRIPTS_DIR"

# ====== 系統設定 ======

# CPU 執行緒數：優先使用環境變數，否則自動偵測
if [ -n "${THREADS:-}" ]; then
    THREADS="$THREADS"
else
    THREADS="$(sysctl -n hw.logicalcpu || echo 8)"
fi

# ====== 環境檢查區塊 ======

echo "[*] Configuration summary:"
echo "    Whisper root: $WHISPER_ROOT"
echo "    Transcripts dir: $TRANSCRIPTS_DIR"  
echo "    Preferred model: $PREFERRED_MODEL"
echo "    Language: $DEFAULT_LANGUAGE"
echo "    Threads: $THREADS"
echo

# 檢查可執行檔
if [ ! -x "$BIN/whisper-cli" ]; then
    echo "[!] whisper-cli not found at $BIN/whisper-cli"
    echo "    Build first:"
    echo "      cd $WHISPER_ROOT"
    echo "      cmake -B build && cmake --build build -j"
    exit 1
fi

# ====== 模型選擇區塊 ======

MODEL=""

# 優先使用偏好的模型
if [ -f "$MODEL_PREFERRED" ]; then
    MODEL="$MODEL_PREFERRED"
    echo "[*] Using preferred model: $(basename "$MODEL")"
elif [ -f "$MODEL_BASE" ]; then
    MODEL="$MODEL_BASE"
    echo "[i] Preferred model not found, using fallback: $(basename "$MODEL")"
else
    echo "[!] No suitable model found. Please download at least one:"
    echo "    cd $WHISPER_ROOT"
    echo "    bash ./models/download-ggml-model.sh ${PREFERRED_MODEL}.en"
    echo "    or: bash ./models/download-ggml-model.sh base.en"
    exit 1
fi

# ====== 使用者輸入區塊 ======

echo
read -r -p "Enter path to your meeting audio (drag & drop or type path): " IN

# 清理輸入路徑（移除可能的引號）
IN="${IN//\'/}"  # 移除單引號
IN="${IN//\"/}"  # 移除雙引號

# 展開 ~ 符號為實際的家目錄路徑
IN="${IN/#\~/$HOME}"

if [ ! -f "$IN" ]; then
    echo "[!] File not found: $IN"
    exit 1
fi

# ====== 檔案資訊顯示區塊 ======

echo "[*] Input file: $IN"

# 顯示音訊資訊（如果 ffprobe 可用）
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

# ====== 音訊正規化區塊 ======

# 取得輸入檔案的基本名稱（不含路徑）
base_in="$(basename "$IN")"
# basename：取得路徑的檔名部分

# 取得檔案名稱（不含副檔名）
stem="${base_in%.*}"
# ${VAR%pattern}：從變數末尾移除符合 pattern 的最短字串
# %.*：移除最後一個點及其後的所有字元（即移除副檔名）

# 建立正規化後的音訊檔案路徑
NORM="$TRANSCRIPTS_DIR/${stem}_norm16k.wav"

echo "[*] Normalizing audio to 16kHz mono WAV -> $NORM"

# 使用 ffmpeg 正規化音訊
ffmpeg -y -i "$IN" -ac 1 -ar 16000 -c:a pcm_s16le "$NORM"
# ffmpeg 參數說明：
#   -y：如果輸出檔案已存在，自動覆蓋
#   -i "$IN"：輸入檔案
#   -ac 1：音訊聲道數為 1（單聲道）
#   -ar 16000：採樣率設為 16kHz
#   -c:a pcm_s16le：音訊編碼為 16位元 PCM，小端序

# ====== 轉錄執行區塊 ======

# 建立輸出檔案的基本路徑
OUT_BASE="$TRANSCRIPTS_DIR/${stem}_transcription"

echo "[*] Starting transcription with $(basename "$MODEL")..."
echo "    This may take a while for long recordings..."

# 執行轉錄
"$BIN/whisper-cli" \
    -m "$MODEL" \
    -f "$NORM" \
    --language "$DEFAULT_LANGUAGE" \
    --threads "$THREADS" \
    --output-txt --output-srt --output-vtt --output-json \
    --output-file "$OUT_BASE"

# ====== 後續處理區塊 ======

TXT="${OUT_BASE}.txt"

# 複製到剪貼簿（macOS）
if [ -f "$TXT" ]; then
    if command -v pbcopy >/dev/null 2>&1; then
        cat "$TXT" | pbcopy
        echo "[*] Transcript copied to clipboard"
    fi
fi

# ====== 結果輸出區塊 ======

echo
echo "[✓] Transcription completed successfully!"
echo
echo "=== Input ==="
echo "Original audio : $IN"
echo "Normalized     : $NORM"
echo
echo "=== Output Files ==="
echo "Text transcript: ${OUT_BASE}.txt"
echo "SRT subtitles  : ${OUT_BASE}.srt"  
echo "VTT subtitles  : ${OUT_BASE}.vtt"
echo "JSON data      : ${OUT_BASE}.json"
echo "Config used    : $ENV_FILE"

# 開啟輸出資料夾（macOS）
if command -v open >/dev/null 2>&1; then
    echo
    echo "[*] Opening output folder..."
    open "$TRANSCRIPTS_DIR" >/dev/null 2>&1 || true
fi