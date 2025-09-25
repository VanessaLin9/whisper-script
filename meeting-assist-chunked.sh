#!/usr/bin/env bash
set -euo pipefail
# -e: 遇到錯誤立即退出
# -u: 使用未定義變數時報錯
# -o pipefail: 管道中任何命令失敗，整個管道就失敗

###############################################################################
# Meeting Assist (完整錄音 + 會後轉錄)
# - 全程錄音：完整音訊檔案
# - 會後轉錄：使用 small.en 模型（較準確）
#
# How to run:
#   meeting-assist-simple.sh
#
# Output folder:
#   ~/MeetingRecords
###############################################################################

# ====== 路徑設定區塊 ======

# 環境變數設定：如果 WHISPER_ROOT 沒有設定，則使用預設值
WHISPER_ROOT="${WHISPER_ROOT:-/Users/samuelwong/whisper.cpp}"   # 你的 whisper.cpp 根目錄
BIN="$WHISPER_ROOT/build/bin"                                   # 可執行檔位置
MODELS_DIR="$WHISPER_ROOT/models"                               # 模型位置

# 定義要使用的 AI 模型檔案路徑
FINAL_MODEL="$MODELS_DIR/ggml-small.en.bin" # mainly use
FALLBACK_MODEL="$MODELS_DIR/ggml-base.en.bin" # backup model


# ====== setting section ======
# 音訊輸入設備（macOS 的 AVFoundation 格式）
MIC=":0"                       # :0 為 Mac 內建麥克風

# 使用 $HOME 變數來指定用戶家目錄下的輸出資料夾
OUTDIR="$HOME/MeetingRecords"

# 動態取得 CPU 核心數，用於多執行緒處理
# $(...) 是命令替換：執行括號內的命令並將輸出作為變數值
# || echo 8 是錯誤處理：如果前面命令失敗，就使用 8 作為預設值
THREADS="$(sysctl -n hw.logicalcpu || echo 8)"

# ====== 環境檢查區塊 ======
if [ ! -x "$BIN/whisper-cli" ]; then
  # [ ! -x FILE ] 測試：檔案不存在或不可執行時為真
  # 輸出錯誤訊息到標準錯誤輸出
  echo "[!] Cannot find executable: $BIN/whisper-cli"
  echo "    Please build whisper.cpp first:  (in $WHISPER_ROOT)"
  echo "      cmake -B build -DWHISPER_PORTAUDIO=OFF && cmake --build build -j"
  exit 1 # 以錯誤狀態 1 退出腳本
fi

# 檢查 AI 模型檔案
if [ ! -f "$FINAL_MODEL" ]; then
# [ ! -f FILE ] 測試：檔案不存在或不是普通檔案時為真
  echo "[i] FINAL_MODEL not found: $FINAL_MODEL"
  echo "    Will fallback to LIVE_MODEL for final transcription."
  if [! -f "$FALLBACK_MODEL" ]; then
    echo "[!] FALLBACK_MODEL also not found: $FALLBACK_MODEL"
    echo "    Run: bash $MODELS_DIR/../models/download-ggml-model.sh small.en"
    echo "    Or:  bash $MODELS_DIR/../models/download-ggml-model.sh base.en"
    exit 1
  else
    echo "[i] Will use fallback model: $FALLBACK_MODEL"
    # 重新指派變數：讓 FINAL_MODEL 指向備用模型
    FINAL_MODEL="$FALLBACK_MODEL"
fi

# ====== 準備輸出區塊 ======
# mkdir -p：建立目錄，-p 表示如果父目錄不存在會自動建立，如果目錄已存在不會報錯
mkdir -p "$OUTDIR"

# 使用 date 命令產生時間戳記
# +格式：指定輸出格式，%Y年%m月%d日_%H時%M分%S秒
ts="$(date +'%Y%m%d_%H%M%S')"

# 使用變數拼接建立完整檔案路徑
wav="$OUTDIR/meeting_${ts}.wav"               # 連續完整錄音

# 顯示可用的音訊設備列表（供參考用）
echo "[*] Device list (for reference):"

# ffmpeg 設備列表命令的複雜用法：
# 2>&1：將標準錯誤重定向到標準輸出（因為 ffmpeg 將設備列表輸出到 stderr）
# | sed 's/^/[ffmpeg] /'：用管道傳給 sed，在每行前面加上 [ffmpeg] 前綴
# || true：如果命令失敗，執行 true（不讓腳本因為這個命令失敗而退出）
ffmpeg -f avfoundation -list_devices true -i "" 2>&1 | sed 's/^/[ffmpeg] /' || true

# echo 空行，讓輸出更整潔
echo

# ====== 開始錄音區塊 ======

# 顯示資訊給使用者
echo "[*] Start FULL recording to: $wav"
echo "[*] Using transcription model: $(basename "$FINAL_MODEL")"
# basename：取得路徑的檔名部分（移除目錄路徑）
echo "[*] Press Ctrl+C to stop recording and start transcription..."
echo

# 設定訊號處理：當收到 INT 訊號（Ctrl+C）時執行的動作
trap 'echo -e "\n[*] Stopping recording..."; kill $FF_PID 2>/dev/null || true' INT
# trap 'commands' SIGNAL：當收到指定訊號時執行 commands
# echo -e：啟用跳脫字元解析（\n 會被解析為換行）
# kill $FF_PID：終止背景的 ffmpeg 進程
# 2>/dev/null：將錯誤輸出重定向到 /dev/null（忽略錯誤訊息）
# || true：如果 kill 失敗就執行 true（避免腳本退出）

# 開始錄音（背景執行）
ffmpeg -f avfoundation -i "$MIC" -ac 1 -ar 16000 -c:a pcm_s16le "$wav" \
  > "$OUTDIR/ffmpeg_full_${ts}.log" 2>&1 &
# ffmpeg 參數說明：
#   -f avfoundation：使用 macOS 的 AVFoundation 框架
#   -i "$MIC"：輸入設備
#   -ac 1：音訊聲道數為 1（單聲道）
#   -ar 16000：採樣率 16kHz
#   -c:a pcm_s16le：音訊編碼格式（16位元 PCM，小端序）
# \：行接續符號，讓長命令可以分多行寫
# > file 2>&1：將標準輸出和標準錯誤都重定向到 log 檔
# &：在背景執行這個命令

# 儲存背景進程的 PID（進程 ID）
FF_PID=$!
# $!：最後一個背景進程的 PID

# 等待用戶按 Ctrl+C 中斷
wait $FF_PID 2>/dev/null || true
# wait：等待指定的背景進程結束
# 2>/dev/null：忽略錯誤訊息
# || true：如果 wait 因為訊號中斷而失敗，執行 true（正常情況）

echo "[*] Recording stopped."

# ====== 檔案完整性檢查 ======

# 檢查錄音檔案是否存在且不為空
if [ ! -f "$wav" ] || [ ! -s "$wav" ]; then
  # [ ! -f FILE ]：檔案不存在或不是普通檔案
  # [ ! -s FILE ]：檔案不存在或大小為 0
  # || ：邏輯 OR，任一條件成立就執行 then 區塊
  echo "[!] Recording file is missing or empty: $wav"
  exit 1
fi

echo "[*] Recording saved: $wav"
echo "[*] Starting transcription..."

# ====== 最終轉錄區塊 ======
# 建立輸出檔案的基本路徑（不含副檔名）
base="$OUTDIR/meeting_${ts}"

# 執行 whisper 轉錄
"$BIN/whisper-cli" \
  -m "$FINAL_MODEL" \
  -f "$wav" \
  --threads "$THREADS" \
  --output-txt --output-srt \
  --output-file "$base"
# whisper-cli 參數說明：
#   -m：指定模型檔案
#   -f：指定輸入音訊檔案
#   --threads：使用的執行緒數量
#   --output-txt：輸出純文字檔
#   --output-srt：輸出字幕檔
#   --output-file：輸出檔案的基本名稱（whisper 會自動加上 .txt, .srt 等副檔名）

# 輸出完成訊息和檔案位置
echo
echo "[✓] Transcription complete!"
echo
echo "=== Output Files ==="
echo "Audio : $wav"                           # 原始音訊檔
echo "Text  : ${base}.txt"                   # 純文字轉錄檔
echo "SRT   : ${base}.srt"                   # 字幕檔
echo "Log   : $OUTDIR/ffmpeg_${ts}.log"     # ffmpeg 的 log 檔

# 腳本結束，會自動以狀態碼 0（成功）退出