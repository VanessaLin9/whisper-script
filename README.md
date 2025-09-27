# Whisper Script Collection | Whisper 腳本集合

Bash scripts for meeting transcription using [Whisper.cpp](https://github.com/ggerganov/whisper.cpp) with environment-based configuration.  
使用 [Whisper.cpp](https://github.com/ggerganov/whisper.cpp) 進行會議轉錄的 Bash 腳本，支援透過環境變數進行設定。

## Features | 功能特色

- **Live meeting recording** with automatic transcription  
  **即時錄音與轉錄**：會議進行中同步錄音，結束時自動轉錄
- **Batch audio transcription** for existing files  
  **批次音檔轉錄**：可將既有音訊檔批次轉換成文字
- **Multiple formats**: TXT, SRT, VTT, JSON  
  **多種輸出格式**：TXT、SRT、VTT、JSON
- **Environment-based configuration** via `.env` files  
  **環境變數設定**：透過 `.env` 檔案輕鬆配置
- **Smart model selection** with fallback  
  **智慧模型選擇**：優先使用指定模型，必要時自動切換備用模型
- **macOS optimized** with AVFoundation  
  **macOS 優化**：整合 AVFoundation，效能更佳

## Scripts | 腳本說明

### 1. `meeting-assist-chunked.sh` - Live Recording & Transcription | 即時錄音與轉錄
Records meetings in real-time and transcribes when stopped (Ctrl+C).  
支援即時錄音，並在手動停止（Ctrl+C）後自動生成逐字稿。

**Features | 功能：**
- Continuous recording with automatic transcription  
  持續錄音並於結束時自動轉錄
- Smart model selection (prefers `small.en`, fallback to `base.en`)  
  智慧選擇模型（優先 `small.en`，備用 `base.en`）
- Outputs: audio, TXT, SRT files  
  輸出檔案：音訊、TXT、SRT
- Environment-based configuration  
  可透過環境變數設定參數

### 2. `transcribe-meeting.sh` - Batch Transcription | 批次轉錄
Transcribes existing audio files with preprocessing.  
將現有音訊檔進行預處理（格式化）後轉錄成文字。

**Features | 功能：**
- Audio normalization to 16kHz mono WAV  
  音訊自動轉換為 16kHz 單聲道 WAV
- Multiple output formats (TXT, SRT, VTT, JSON)  
  支援多種輸出格式（TXT、SRT、VTT、JSON）
- Clipboard integration (macOS)  
  支援與 macOS 剪貼簿整合
- Drag & drop file support  
  支援拖曳檔案輸入

## Setup | 安裝設定

### 1. Install Dependencies | 安裝依賴套件

```bash
# Install Whisper.cpp | 安裝 Whisper.cpp
git clone https://github.com/ggerganov/whisper.cpp.git
cd whisper.cpp
cmake -B build -DWHISPER_PORTAUDIO=OFF
cmake --build build -j

# Download models | 下載模型
bash ./models/download-ggml-model.sh small.en
bash ./models/download-ggml-model.sh base.en

# Install FFmpeg (macOS) | 安裝 FFmpeg (macOS)
brew install ffmpeg
```

### 2. Configure Environment | 配置環境變數

```bash
# Copy and edit configuration | 複製並編輯設定檔
cp .env.example .env
nano .env  # Edit paths to match your setup | 編輯路徑以符合實際環境
```

## Configuration | 配置說明

Edit `.env` file with your paths | 編輯 `.env` 檔案設定路徑：

```bash
# Required | 必要參數
WHISPER_ROOT=/Users/yourusername/whisper.cpp

# Optional (with defaults) | 可選參數（有預設值）
MEETING_RECORDS_DIR=$HOME/MeetingRecords
TRANSCRIPTS_DIR=$HOME/MeetingRecords/Transcripts
MIC_DEVICE=:0                    # :0 = built-in Mac microphone | :0 = Mac 內建麥克風
DEFAULT_LANGUAGE=en
PREFERRED_MODEL=small            # small, base, tiny, etc. | small, base, tiny 等
THREADS=8                        # Auto-detected if not set | 未設定時自動偵測
```

**Audio devices | 音訊設備：** Run `ffmpeg -f avfoundation -list_devices true -i ""` to see available devices.  
音訊設備：執行 `ffmpeg -f avfoundation -list_devices true -i ""` 可查看可用裝置。

## Usage | 使用方法

### Live Recording | 即時錄音
```bash
./meeting-assist-chunked.sh
# Press Ctrl+C to stop and transcribe | 按 Ctrl+C 停止並轉錄
```

### Batch Transcription | 批次轉錄
```bash
./transcribe-meeting.sh
# Drag & drop audio file or type path | 拖放音訊檔案或輸入路徑
```

**Output files | 輸出檔案：**
- Audio | 音訊: `meeting_YYYYMMDD_HHMMSS.wav`
- Transcript | 逐字稿: `meeting_YYYYMMDD_HHMMSS.txt`
- Subtitles | 字幕: `meeting_YYYYMMDD_HHMMSS.srt`
- WebVTT: `meeting_YYYYMMDD_HHMMSS.vtt` (batch only | 僅批次轉錄)
- JSON: `meeting_YYYYMMDD_HHMMSS.json` (batch only | 僅批次轉錄)

## Models | 模型說明

**Preference order | 優先順序：**
1. `small.en` - Best speed/accuracy balance | 速度與準確度最佳平衡
2. `base.en` - Faster fallback | 較快的備用方案

Models auto-download to `models/` directory.  
模型會自動下載到 `models/` 目錄。

## Troubleshooting | 故障排除

**"whisper-cli not found" | "找不到 whisper-cli"**
```bash
cd $WHISPER_ROOT && cmake --build build -j
```

**"No model found" | "找不到模型"**
```bash
bash ./models/download-ggml-model.sh small.en
```

**"Recording file missing/empty" | "錄音檔案遺失或空白"**
- Check mic permissions in System Preferences | 檢查系統偏好設定中的麥克風權限
- Verify device ID: `ffmpeg -f avfoundation -list_devices true -i ""` | 確認設備 ID

**".env file not found" | "找不到 .env 檔案"**
```bash
cp .env.example .env && nano .env
```

## Files | 檔案結構

```
whisper-script/
├── .env.example                 # Configuration template | 配置範本
├── .env                         # Your configuration (ignored by git) | 使用者配置（git 忽略）
├── meeting-assist-chunked.sh    # Live recording + transcription | 即時錄音 + 轉錄
├── transcribe-meeting.sh        # Batch transcription | 批次轉錄
└── README.md

~/MeetingRecords/                # Live recording output | 即時錄音輸出
├── meeting_YYYYMMDD_HHMMSS.wav  # Audio | 音訊
├── meeting_YYYYMMDD_HHMMSS.txt  # Transcript | 逐字稿
└── meeting_YYYYMMDD_HHMMSS.srt  # Subtitles | 字幕

~/MeetingRecords/Transcripts/    # Batch transcription output | 批次轉錄輸出
├── filename_norm16k.wav         # Normalized audio | 正規化音訊
├── filename_transcription.txt   # Transcript | 逐字稿
├── filename_transcription.srt   # Subtitles | 字幕
├── filename_transcription.vtt   # WebVTT
└── filename_transcription.json  # JSON data | JSON 資料

```
## License | 授權

Open source - use freely for personal and professional transcription workflows.  
開源專案 - 可自由用於個人或專業的會議轉錄工作流程。