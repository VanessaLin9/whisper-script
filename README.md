# Whisper Script Collection｜Whisper 會議轉錄工具

這是一套以 [whisper.cpp](https://github.com/ggml-org/whisper.cpp) 為核心、針對 macOS 設計的會議錄音與轉錄腳本。

目前預設使用多語言 `small` 模型，主要語言設定為中文（`zh`），適合中文為主、夾雜英文專有名詞的會議。請勿改用 `small.en`；`.en` 是英文專用模型，無法可靠處理中文。

## 功能

- 使用 macOS AVFoundation 錄製會議，停止後自動轉錄
- 將既有本機音訊正規化為 16 kHz 單聲道 WAV 後轉錄
- 接受 **公開 Google Drive 連結**，下載後寫入 meeting workspace 再本機轉錄（無需 GUI／OAuth）
- 預設輸出 **TXT、SRT、JSON**（VTT 為 opt-in）
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

## Meeting workspace 與產物

每次本機音訊或 Drive 流程成功後，會在 `MEETING_RECORDS_DIR`（或 CLI 指定的 `--output-root`）下建立：

```text
YYYY-MM-DD_HHMM_<safe-stem>/
├── source_meta.json
├── <safe-stem>.<ext>                 # 僅 managed 來源（Drive 下載）會保存音檔副本
├── <safe-stem>_norm16k.wav           # normalize 開啟時
├── <safe-stem>_transcription.txt
├── <safe-stem>_transcription.srt
└── <safe-stem>_transcription.json
# + .vtt 僅在明確要求 VTT 時
```

來源 ownership：

- **本機音訊（local reference）**：不複製、不移動原始檔；Core 直接讀取原路徑；`retained_in_workspace=false`
- **公開 Drive 下載（managed download）**：下載後安全寫入 workspace，原始音檔與 raw transcript 會保留；`retained_in_workspace=true`
- 輸出衝突採 **fail closed**：既有 audio／artifacts 不會被覆寫

預設 artifacts 為 TXT + SRT + JSON；VTT 需額外加入（例如 `--outputs txt,srt,json,vtt`）。

## 使用方式

### 即時錄音並在結束後轉錄

```bash
./scripts/record-meeting.sh
```

按 `Ctrl+C` 停止錄音，腳本會接著執行轉錄。

此腳本**尚未**遷移到 Output Manager meeting workspace。目前仍直接寫入 `MEETING_RECORDS_DIR` 扁平檔名：

```text
MEETING_RECORDS_DIR/
├── meeting_YYYYMMDD_HHMMSS.wav
├── meeting_YYYYMMDD_HHMMSS.txt
├── meeting_YYYYMMDD_HHMMSS.srt
└── ffmpeg_YYYYMMDD_HHMMSS.log
```

### 轉錄既有本機音訊

```bash
./scripts/transcribe-english.sh
```

依提示輸入或拖入音訊路徑。雖然檔名仍保留舊名稱 `transcribe-english.sh`，目前程式已使用多語言模型，預設以中文為主要語言。

腳本會先確認會議時間（檔名中的 `YYYY-MM-DD_HHMM`、metadata、檔案時間或手動輸入），再透過 Output Manager 建立 meeting workspace。

**原始錄音不會被複製、移動或改名。** Core 以 reference 方式讀取原檔；資料夾名稱含時間前綴，artifact 檔名使用 `<safe-stem>`（不再重複加時間前綴）：

```text
MeetingRecords/
└── 2026-07-17_1500_<safe-stem>/
    ├── source_meta.json
    ├── <safe-stem>_norm16k.wav
    ├── <safe-stem>_transcription.txt
    ├── <safe-stem>_transcription.srt
    └── <safe-stem>_transcription.json
```

若輸出產物已存在，腳本會停止而不是靜默覆蓋。

### 公開 Google Drive 連結（Phase 1 CLI）

僅支援 **公開** sharing link（不做 Google OAuth）。下載失敗會 bounded retry 後停止，且不會啟動轉錄。

```bash
PYTHONPATH=. python3 -m src.workflow \
  "https://drive.google.com/file/d/<FILE_ID>/view?usp=sharing" \
  --output-root "$HOME/MeetingRecords" \
  --language zh \
  --model small \
  --model-path "$HOME/whisper.cpp/models/ggml-small.bin" \
  --whisper-cli "$HOME/whisper.cpp/build/bin/whisper-cli" \
  --threads 8
```

也可用 `--url` 代替 positional URL。成功時 stdout 會列出 workspace、raw audio、raw transcript 與各 artifact 路徑；進度與錯誤寫入 stderr。

機器可讀輸出（stdout 僅最終 JSON；進度仍在 stderr）：

```bash
PYTHONPATH=. python3 -m src.workflow \
  --url "https://drive.google.com/file/d/<FILE_ID>/view" \
  --output-root "$HOME/MeetingRecords" \
  --language zh \
  --model small \
  --model-path "$HOME/whisper.cpp/models/ggml-small.bin" \
  --whisper-cli "$HOME/whisper.cpp/build/bin/whisper-cli" \
  --threads 8 \
  --json
```

成功 JSON 含 `ok`、`workspace_dir`、`raw_audio_path`、`raw_transcript_path`、`artifacts` 等欄位；失敗時 `ok=false` 並帶 `stage`／`message`，exit code 非 0。

可選參數：

- `--outputs txt,srt,json`（預設；可加 `vtt`；workflow 會確保保留 TXT）
- `--meeting-time 2026-07-18T12:34:00+08:00`
- `--no-normalize` / `--no-keep-normalized`
- `--quiet-progress`

此流程需要可連線下載公開 Drive 檔案，並使用本機 whisper 模型；離線 CI 以 fake downloader／fake core 覆蓋，端到端真人驗證請在本機對公開測試檔執行上述命令。

### 轉錄已切割的音訊片段

若資料夾中已有 `segment_001.wav`、`segment_002.wav` 等檔案：

```bash
./scripts/multi-lang.sh /path/to/segments_folder
```

結果會寫入該資料夾下的 `transcripts/`。此 batch 流程尚未遷移到新的 meeting workspace manager（不在 Phase 1 範圍）。

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

### Drive 下載失敗

- 確認連結為公開分享（anyone with the link）
- 檢查 stderr 的 `stage=download` 訊息（permission／404／timeout／HTML）
- 網路失敗會 bounded retry；失敗後不會開始轉錄，也不會覆寫既有 meeting 資料

### 輸出衝突

若同名 workspace／artifact 已存在，流程會 fail closed 並回報 `workspace` 或 core 的 conflict stage，不會覆寫既有 raw audio／transcript。

## 測試

離線 regression tests（不使用網路、麥克風、真實模型、剪貼簿或 Finder）：

```bash
bash tests/run_tests.sh
```

涵蓋 Output Manager、Drive downloader、Phase 1 workflow／CLI、transcription core，以及既有 shell wrapper 行為。

## 專案結構

```text
whisper-script/
├── .env.example
├── README.md
├── env_loader.py
├── setup.py
├── cli.py                      # legacy entry (not Phase 1 Drive CLI)
├── scripts/
│   ├── lib/common.sh
│   ├── organize_recording.py
│   ├── record-meeting.sh
│   ├── transcribe-english.sh   # local audio → workspace → core
│   └── multi-lang.sh
├── tests/
│   └── run_tests.sh
├── pipelines/
│   └── multilang_batch.py      # not migrated in Phase 1
└── src/
    ├── drive/                  # public Drive URL adapter + downloader
    ├── output_manager/         # meeting workspace + ownership policy
    ├── workflow/               # Phase 1 Drive→workspace→transcribe + CLI
    ├── transcription/          # reusable local single-file core
    ├── preprocessing/
    │   └── audio_splitter.py
    └── postprocessing/
        └── cleaner.py
```

## License

Open source — 可自由用於個人或專業的會議轉錄工作流程。
