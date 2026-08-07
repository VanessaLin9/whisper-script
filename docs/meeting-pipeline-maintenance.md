# 會議 Pipeline 之外的維護筆記

這份筆記記錄低頻、一次性或環境維護工作。每天錄音後的固定流程仍以會議 pipeline 為主，不需要每次重新閱讀這裡的全部內容。

## 第一段：clone 後一次性設定

### 1. Clone 專案

```bash
git clone <repository-url> /Users/user/whisper-script
cd /Users/user/whisper-script
```

### 2. 建立本機設定

```bash
cp .env.example .env
```

在 `.env` 設定本機 whisper.cpp 路徑與會議資料夾。`.env` 已被 git 忽略，不要把 token 或主機路徑提交到 repository。

### 3. 安裝與檢查 whisper.cpp

```bash
python3 setup.py check
python3 setup.py install
```

若尚未安裝 whisper.cpp，先依 setup 的提示 clone、build 並下載 multilingual model。中文會議不可使用 `.en` English-only model。

### 4. 建立私有 prompt notes 目錄

```bash
mkdir -p .local/prompt_notes
```

`.local/` 已加入 `.gitignore`。這裡的 Markdown 可能含有公司、專案、Agent 與 domain knowledge，只保留在本機，不上傳 GitHub。

### 5. 同步 Notion prompt notes

先建立一個僅能讀取相關 Notion 頁面的 internal integration，將 token 放在本機環境變數或後續 macOS Keychain；不要寫入程式碼：

```bash
export NOTION_TOKEN='***'
PYTHONPATH=. python3 scripts/sync_prompt_notes.py
```

只同步一份時：

```bash
PYTHONPATH=. python3 scripts/sync_prompt_notes.py --key inno
```

同步 script 讀取 `config/prompt_sources.json` 的 page ID，從 Notion API 取得 page blocks，輸出到 `.local/prompt_notes/inno.md`、`.local/prompt_notes/whisper.md`。輸出檔包含少量 front matter：`key`、使用者選單標題、來源頁面、同步時間與完整 prompt 內容。

### 6. 驗證本機 pipeline

```bash
PYTHONPATH=. python3 -m unittest discover -s tests -p 'test_*.py'
PYTHONPATH=. python3 -m src.meeting_pipeline --help
```

每天的固定使用方式仍是：準備好 timestamped meeting workspace 後，執行 `PYTHONPATH=. python3 -m src.meeting_pipeline`。互動選單會優先顯示本機 prompt note front matter 的標題；如果尚未同步，會退回內建的 profile metadata。

## 第二段：提示詞稿的建立與維護

### 1. 每個主題一份 Notion note 與一個本機 Markdown 檔

不要把所有 domain knowledge 混在一份巨大 prompt 裡。每個主題在 `config/prompt_sources.json` 登記一筆：

- `key`：程式內使用的穩定識別，例如 `inno`、`whisper`。
- `title`：互動選單顯示的名稱，例如 `inno｜Inno Group／AI Team`。
- `page_id`：Notion prompt note 的 page ID。
- `filename`：本機輸出檔名，例如 `inno.md`。

Notion 頁面內再維護完整 prompt、詞彙表、角色對照與不確定性規則。不要把 domain 內容直接寫進 tracked Python code 或 tracked README。

### 2. 建立新主題

1. 在 Notion 建立一份獨立 prompt note。
2. 在 `config/prompt_sources.json` 新增一筆不含敏感內容的來源 metadata。
3. 執行 `scripts/sync_prompt_notes.py --key <key>`。
4. 確認 `.local/prompt_notes/<key>.md` 具有 front matter 與完整內容。
5. 重新啟動 meeting pipeline；選單會使用本機 note 的 `title`。

若該主題尚未有可靠 prompt，先以 `new`／未註冊狀態處理，不要讓模型自行猜測 domain 詞彙。

### 3. 修改既有提示詞

先在 Notion 修改並確認版本，再重新同步本機檔案。清洗工作應記錄使用的 prompt `key`、來源 page ID 與本機同步檔案路徑；之後可再加上 prompt revision hash，讓同一場會議可追溯當時使用的版本。

### 4. 私有資料界線

- tracked repository：程式碼、測試、流程文件、prompt source registry metadata。
- gitignored `.local/prompt_notes/`：完整 prompt、公司詞彙、Agent 對照、domain knowledge。
- `MeetingRecords`：原始音訊、逐字稿、清洗稿與 pipeline state，保持在本機資料根目錄。

如果未來要讓多台主機使用，應重新設計加密同步或 Keychain/secret manager；不要因為方便而把 `.local/` 加回 git。

## 目前已知的分層

```text
Notion prompt note
        ↓ sync_prompt_notes.py
private .local/prompt_notes/*.md
        ↓ meeting_pipeline.py 讀取 front matter
使用者依本機標題選 prompt profile
        ↓ 後續清洗 adapter（尚待實作）
LLM 語意清洗與輸出驗證
```

目前 pipeline controller 已能使用本機 note 的標題與路徑建立 state，但 LLM API 還沒有由 controller 自動呼叫；這是下一階段的獨立工作。
