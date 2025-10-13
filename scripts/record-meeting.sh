#!/usr/bin/env bash
# ↑ shebang：告訴系統用 bash 來執行這個腳本

# 設定腳本執行的嚴格模式
set -euo pipefail
# -e: 遇到錯誤立即退出
# -u: 使用未定義變數時報錯
# -o pipefail: 管道中任何命令失敗，整個管道就失敗

###############################################################################
# Meeting Assist (完整錄音 + 會後轉錄) - 環境變數版本
# - 全程錄音：完整音訊檔案
# - 會後轉錄：使用 small.en 模型（較準確）
# - 使用 .env 檔案管理設定，避免硬編碼路徑
#
# 使用前請先：
# 1. 複製 .env.example 為 .env
# 2. 修改 .env 中的路徑設定
#
# How to run:
#   meeting-assist.sh
###############################################################################

# ====== 環境變數載入區塊 ======

# 取得腳本所在目錄
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# ${BASH_SOURCE[0]}：目前腳本的路徑
# dirname：取得路徑的目錄部分
# cd ... && pwd：切換到該目錄並取得絕對路徑

# 載入 .env 檔案
ENV_FILE="$SCRIPT_DIR/.env"

if [ ! -f "$ENV_FILE" ]; then
    echo "[!] Environment file not found: $ENV_FILE"
    echo "    Please copy .env.example to .env and configure your paths:"
    echo "    cp .env.example .env"
    echo "    nano .env  # 編輯設定檔"
    exit 1
fi

# 載入環境變數（只載入符合格式的行，忽略註解）
# 讀取 .env 檔案，過濾掉空行和註解行
while IFS= read -r line || [ -n "$line" ]; do
    # 跳過空行
    [[ -z "$line" ]] && continue
    # 跳過註解行（以 # 開頭）
    [[ "$line" =~ ^[[:space:]]*# ]] && continue
    # 跳過不包含 = 的行
    [[ "$line" != *"="* ]] && continue
    
    # 匯出變數到環境中
    export "$line"
done < "$ENV_FILE"

echo "[*] Loaded configuration from: $ENV_FILE"

# ====== 設定檢查和預設值 ======

# 檢查必要的環境變數
if [ -z "${WHISPER_ROOT:-}" ]; then
    echo "[!] WHISPER_ROOT not set in .env file"
    exit 1
fi

# 設定預設值（如果 .env 中沒有定義）
MEETING_RECORDS_DIR="${MEETING_RECORDS_DIR:-$HOME/MeetingRecords}"
TRANSCRIPTS_DIR="${TRANSCRIPTS_DIR:-$HOME/MeetingRecords/Transcripts}"
MIC_DEVICE="${MIC_DEVICE:-:0}"
DEFAULT_LANGUAGE="${DEFAULT_LANGUAGE:-en}"
PREFERRED_MODEL="${PREFERRED_MODEL:-small}"

# 固定的音訊設定（這些通常不需要改變）
AUDIO_CHANNELS="1"      # 單聲道
AUDIO_SAMPLE_RATE="16000"  # 16kHz 採樣率

# 展開環境變數中的 $HOME (如果有的話)
WHISPER_ROOT=$(eval echo "$WHISPER_ROOT")
MEETING_RECORDS_DIR=$(eval echo "$MEETING_RECORDS_DIR")
TRANSCRIPTS_DIR=$(eval echo "$TRANSCRIPTS_DIR")

# ====== 路徑建構區塊 ======

# 使用環境變數建構路徑
BIN="$WHISPER_ROOT/build/bin"          # whisper 可執行檔位置
MODELS_DIR="$WHISPER_ROOT/models"      # AI 模型檔案位置

# 根據偏好模型建構檔案路徑
FINAL_MODEL="$MODELS_DIR/ggml-${PREFERRED_MODEL}.en.bin"
FALLBACK_MODEL="$MODELS_DIR/ggml-base.en.bin"

# ====== 系統設定區塊 ======

# CPU 執行緒數：優先使用環境變數，否則自動偵測
if [ -n "${THREADS:-}" ]; then
    THREADS="$THREADS"
else
    THREADS="$(sysctl -n hw.logicalcpu || echo 8)"
fi

# ====== 環境檢查區塊 ======

echo "[*] Configuration summary:"
echo "    Whisper root: $WHISPER_ROOT"
echo "    Output dir: $MEETING_RECORDS_DIR"
echo "    Mic device: $MIC_DEVICE"
echo "    Preferred model: $PREFERRED_MODEL"
echo "    Threads: $THREADS"
echo

# 檢查可執行檔是否存在且可執行
if [ ! -x "$BIN/whisper-cli" ]; then
    echo "[!] Cannot find executable: $BIN/whisper-cli"
    echo "    Please build whisper.cpp first in: $WHISPER_ROOT"
    echo "      cd $WHISPER_ROOT"
    echo "      cmake -B build -DWHISPER_PORTAUDIO=OFF && cmake --build build -j"
    exit 1
fi

# 檢查 AI 模型檔案
if [ ! -f "$FINAL_MODEL" ]; then
    echo "[!] Preferred model not found: $FINAL_MODEL"
    
    # 檢查備用模型
    if [ ! -f "$FALLBACK_MODEL" ]; then
        echo "[!] Fallback model also not found: $FALLBACK_MODEL"
        echo "    Please download at least one model:"
        echo "      cd $WHISPER_ROOT"
        echo "      bash ./models/download-ggml-model.sh ${PREFERRED_MODEL}.en"
        echo "      or: bash ./models/download-ggml-model.sh base.en"
        exit 1
    else
        echo "[i] Will use fallback model: $(basename "$FALLBACK_MODEL")"
        FINAL_MODEL="$FALLBACK_MODEL"
    fi
fi

# ====== 準備輸出區塊 ======

# 建立輸出目錄
mkdir -p "$MEETING_RECORDS_DIR"

# 產生時間戳記
ts="$(date +'%Y%m%d_%H%M%S')"

# 建立完整檔案路徑
wav="$MEETING_RECORDS_DIR/meeting_${ts}.wav"

# 顯示可用的音訊設備列表（供參考用）
echo "[*] Available audio devices:"
ffmpeg -f avfoundation -list_devices true -i "" 2>&1 | sed 's/^/[ffmpeg] /' || true
echo

# ====== 開始錄音區塊 ======

echo "[*] Starting recording to: $wav"
echo "[*] Using transcription model: $(basename "$FINAL_MODEL")"
echo "[*] Audio settings: ${AUDIO_CHANNELS} channel(s), ${AUDIO_SAMPLE_RATE}Hz"
echo "[*] Press Ctrl+C to stop recording and start transcription..."
echo

# 設定訊號處理：當收到 INT 訊號（Ctrl+C）時執行的動作
trap 'echo -e "\n[*] Stopping recording..."; kill $FF_PID 2>/dev/null || true' INT

# 開始錄音（背景執行）
ffmpeg -f avfoundation -i "$MIC_DEVICE" \
    -ac "$AUDIO_CHANNELS" -ar "$AUDIO_SAMPLE_RATE" -c:a pcm_s16le "$wav" \
    > "$MEETING_RECORDS_DIR/ffmpeg_${ts}.log" 2>&1 &

# 儲存背景進程的 PID
FF_PID=$!

# 等待用戶按 Ctrl+C 中斷
wait $FF_PID 2>/dev/null || true

echo "[*] Recording stopped."

# ====== 檔案完整性檢查 ======

# 檢查錄音檔案是否存在且不為空
if [ ! -f "$wav" ] || [ ! -s "$wav" ]; then
    echo "[!] Recording file is missing or empty: $wav"
    exit 1
fi

echo "[*] Recording saved: $wav"
echo "[*] Starting transcription..."

# ====== 最終轉錄區塊 ======

# 建立輸出檔案的基本路徑（不含副檔名）
base="$MEETING_RECORDS_DIR/meeting_${ts}"

# 執行 whisper 轉錄
"$BIN/whisper-cli" \
    -m "$FINAL_MODEL" \
    -f "$wav" \
    --language "$DEFAULT_LANGUAGE" \
    --threads "$THREADS" \
    --output-txt --output-srt \
    --output-file "$base"

# 輸出完成訊息和檔案位置
echo
echo "[✓] Transcription complete!"
echo
echo "=== Output Files ==="
echo "Audio : $wav"
echo "Text  : ${base}.txt"
echo "SRT   : ${base}.srt"
echo "Log   : $MEETING_RECORDS_DIR/ffmpeg_${ts}.log"
echo "Config: $ENV_FILE"