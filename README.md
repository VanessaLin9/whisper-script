# Whisper Script Collection｜Whisper 會議轉錄工具

這是一套以 [whisper.cpp](https://github.com/ggml-org/whisper.cpp) 為核心、針對 macOS 設計的會議錄音與轉錄腳本。

目前預設使用多語言 `small` 模型，主要語言設定為中文（`zh`），適合中文為主、夾雜英文專有名詞的會議。請勿改用 `small.en`；`.en` 是英文專用模型，無法可靠處理中文。

## 功能

- 使用 macOS AVFoundation 錄製會議，停止後自動轉錄
- 將既有音訊正規化為 16 kHz 單聲道 WAV 後轉錄
- 輸出 TXT、SRT、VTT 與 JSON
- 使用 `.env` 管理 whisper.cpp、輸出目錄、語言及模型設定
- 使用 `setup.py check` 做唯讀環境檢查
- 使用 `setup.py install` 編譯 whisper.cpp、下載模型並準備環境

## 新電腦安裝流程

### 1. 安裝基本工具

請先安裝 [Homebrew](https://brew.sh/)；若已安裝可跳過。

```bash
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
```

安裝 Git 與 CMake：

```bash
brew install git cmake
```

### 2. Clone whisper.cpp

本專案不包含 whisper.cpp 本體，請另外 clone：

```bash
git clone https://github.com/ggml-org/whisper.cpp.git ~/whisper.cpp
```

### 3. 建立本機設定

在本專案根目錄執行：

```bash
cp .env.example .env
```

編輯 `.env`，確認 `WHISPER_ROOT` 指向剛才 clone 的 whisper.cpp：

```bash
WHISPER_ROOT=/Users/你的帳號/whisper.cpp
```

`.env` 只供本機使用，已被 Git 忽略，不應 commit。

### 4. 檢查環境

```bash
python3 setup.py check
```

`check` 是唯讀操作，不會安裝套件、編譯程式、下載模型或建立輸出目錄。它會檢查：

- Git、CMake、FFmpeg
- `WHISPER_ROOT` 與 whisper.cpp
- `whisper-cli`
- 設定的多語言模型（預設為 `small`）
- 錄音、逐字稿及 log 目錄

第一次執行時看到部分項目尚未就緒是正常的。

### 5. 執行安裝

```bash
python3 setup.py install
```

`install` 會：

- 在 macOS 上透過 Homebrew 安裝缺少的 FFmpeg
- 編譯 `WHISPER_ROOT` 中的 whisper.cpp
- 依 `PREFERRED_MODEL` 下載多語言模型（預設只下載 `small`）
- 建立錄音、逐字稿與 log 目錄

這個步驟會連線網路，且可能需要一段時間。

### 6. 再次驗證

```bash
python3 setup.py check
```

準備完成時，Git、CMake、FFmpeg、whisper.cpp、`whisper-cli` 和 `model small` 應全部顯示成功。

## 設定

`.env.example` 的預設設定如下：

```bash
# 必填：whisper.cpp clone 的位置
WHISPER_ROOT=/Users/YourName/whisper.cpp

# 輸出目錄
MEETING_RECORDS_DIR=$HOME/MeetingRecords
TRANSCRIPTS_DIR=$HOME/MeetingRecords/Transcripts

# macOS AVFoundation 麥克風裝置
MIC_DEVICE=:0

# 中文為主、可夾雜英文的多語言轉錄
DEFAULT_LANGUAGE="zh"
PREFERRED_MODEL="small"

# 選填；未設定時自動偵測 CPU logical cores
# THREADS=8
```

模型名稱與語言是兩個不同設定：

- `PREFERRED_MODEL="small"` 對應 `ggml-small.bin` 多語言模型
- `DEFAULT_LANGUAGE="zh"` 告訴 Whisper 會議主要是中文
- 不要寫成 `small.zh`，whisper.cpp 沒有這個模型
- 不要使用 `small.en`，它是英文專用模型

列出 macOS 可用錄音裝置：

```bash
ffmpeg -f avfoundation -list_devices true -i ""
```

如果內建麥克風不是 `:0`，請依輸出結果調整 `MIC_DEVICE`。

## 使用方式

### 即時錄音並在結束後轉錄

```bash
./scripts/record-meeting.sh
```

按 `Ctrl+C` 停止錄音，腳本會接著執行轉錄。

輸出至 `MEETING_RECORDS_DIR`：

- `meeting_YYYYMMDD_HHMMSS.wav`
- `meeting_YYYYMMDD_HHMMSS.txt`
- `meeting_YYYYMMDD_HHMMSS.srt`
- `ffmpeg_YYYYMMDD_HHMMSS.log`

### 轉錄既有音訊

```bash
./scripts/transcribe-english.sh
```

依提示輸入或拖入音訊路徑。雖然檔名仍保留舊名稱 `transcribe-english.sh`，目前程式已使用多語言模型，預設以中文為主要語言。

輸出至 `TRANSCRIPTS_DIR`：

- `檔名_norm16k.wav`
- `檔名_transcription.txt`
- `檔名_transcription.srt`
- `檔名_transcription.vtt`
- `檔名_transcription.json`

### 轉錄已切割的音訊片段

若資料夾中已有 `segment_001.wav`、`segment_002.wav` 等檔案：

```bash
./scripts/multi-lang.sh /path/to/segments_folder
```

結果會寫入該資料夾下的 `transcripts/`。

## 疑難排解

### `.env` 找不到

請確認命令是在專案根目錄執行，並建立本機設定：

```bash
cp .env.example .env
```

### whisper.cpp 找不到

確認 `.env` 的 `WHISPER_ROOT` 是完整的 whisper.cpp repo，且其中存在 `CMakeLists.txt`：

```bash
python3 setup.py check
```

### FFmpeg、whisper-cli 或模型缺少

先查看檢查結果：

```bash
python3 setup.py check
```

再由專案安裝流程處理：

```bash
python3 setup.py install
```

請保留終端完整錯誤訊息，以便判斷失敗發生在 Homebrew、CMake 或模型下載階段。

### 錄音檔為空或無法錄音

- 到 macOS「系統設定 → 隱私權與安全性 → 麥克風」允許 Terminal 使用麥克風
- 使用 FFmpeg 列出裝置並修正 `.env` 中的 `MIC_DEVICE`
- 查看 `MEETING_RECORDS_DIR` 中對應的 `ffmpeg_*.log`

## 專案結構

```text
whisper-script/
├── .env.example
├── README.md
├── setup.py
├── cli.py
├── scripts/
│   ├── record-meeting.sh
│   ├── transcribe-english.sh
│   └── multi-lang.sh
├── pipelines/
│   └── multilang_batch.py
└── src/
    ├── preprocessing/
    │   └── audio_splitter.py
    └── postprocessing/
        └── cleaner.py
```

## License

Open source — 可自由用於個人或專業的會議轉錄工作流程。
