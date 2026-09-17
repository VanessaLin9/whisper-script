# Meeting Desk：本機桌面版本

2026-09-07 / v0.2。原生 SwiftUI + 現有 Python service，無 localhost server、web hosting、OAuth 或付費 API。Python 透過 stdin 接收單一 JSON 請求，stdout 回傳逐行進度與最終結果；不透過 shell 拼接使用者參數。

## 使用範圍

原生 App 可拖曳匯入或選取單一音檔、確認錄音時間、瀏覽既有會議、轉錄、預清洗、取消／續跑、準備 LLM 清洗交接、匯入完整清洗稿、預覽與內容確認、準備會議記錄交接。預設多語言 medium、zh。ASR 不保證字體為繁體；繁體校正與專有名詞保真交由後續 LLM 清洗。

內建錄音、批次佇列、Drive GUI、LLM API、Notion 發布尚未實作。使用者可繼續使用語音備忘錄；現有 shell 入口仍保留。

## 人工訂正與會議名稱

- 會議名稱旁的鉛筆按鈕可修改顯示名稱（1–200 字）。清單、搜尋和新交接包立即使用新名稱；資料夾、原音檔、artifact 檔名與會議識別保持不變。
- 在原始／預清洗稿按「編輯文字」，儲存成「人工訂正」版本。該分頁顯示使用中訂正稿，重新選擇會議時優先顯示，後續 GUI 清洗交接使用訂正稿作語意輸入與長度基準。
- 清洗稿也可按「編輯文字」修正。每次保存均保留原檔和之前的版本，撤銷舊的品質通過狀態。使用者重新確認後，會議記錄交接使用最新清洗稿。縮短超過 20% 的手動修改可保存但不能通過品質檢查。
- 「時間軸 → 編輯文字」逐段呈現 SRT：序號與起訖時間是唯讀標籤，只有字幕文字可輸入。後端只接受 `{id,text}`，拒絕新增／刪除／重排字幕、完整 SRT、時間欄位、時間碼注入或空白段落。多行字幕可以保留；原始 SRT 位元組不變。
- 字幕訂正與 TXT 訂正分開保存，不會推測段落對應後自動覆蓋對方。清洗交接包內含目前 SRT 參考，若文字不同，以訂正 TXT 為主要內容來源。
- 編輯採 modal 視窗；儲存中禁止繼續輸入，取消有未儲存提醒，結束 App 也會提醒先處理修改。儲存失敗保留畫面輸入並提供複製按鈕；版本衝突不覆蓋新內容。

新增 `manual_revisions/` 存放 UUID 命名的 TXT / SRT 版本，`desktop_state.json.edits` 記錄各類目前路徑、SHA-256、來源檔 hashes、保存時間，`edit_history` 保留歷程。state 只保存 metadata，不保存逐字稿內容。修改前的 token 包含來源與使用中版本 hashes；後端在 per-meeting flock 內比較，過期編輯拒絕保存。

內容或 SRT 訂正會使舊 handoff 與 quality 失效；需重新建立交接或檢查清洗稿。新 LLM 結果在已有 cleaned 時另存新版，保留舊檔。僅改名稱不影響內容品質。

原始 `*_pipeline_state.json` 與 CLI 路徑保持不變；CLI 不會自動採用 GUI 訂正版。GUI 的 packet 和 preview 明確提供使用中的訂正版路徑，不進行破壞性遷移。

## 分層

- `desktop/MeetingDesk.swift`：原生視窗、檔案拖曳／選擇、剪貼簿與 Finder、背景 Python Process。
- `desktop/assets/MeetingDesk.png`：Dock 圖示原稿（A 款：透明外框、純色深綠底、米白聲波與底線，無漸層或陰影）。Build 使用 macOS `sips` / `iconutil` 產生 16–1024 px 的 `.icns`，由 Info.plist 與執行中的 App 載入。更換原稿後啟動入口會自動重建。
- `src/desktop/service.py`：環境、資料清單、匯入、續跑、交接與品質檢查。沿用 Output Manager、transcription core、preparer。
- `src/desktop/editing.py`：訂正版解析與追溯、樂觀鎖 token、固定時間碼的 SRT 文字編輯。
- `src/desktop/__main__.py`：JSON transport，SIGTERM / SIGINT 轉成 cancellation token。
- `scripts/build-desktop.sh`：編譯及本機 ad-hoc codesign；記錄本機 repo 與 Python 絕對路徑於 Info.plist。
- `開啟 Meeting Desk.command`：首次／Swift 更新後編譯，之後啟動 App。

需要 macOS 14+、Command Line Tools、Python 3.10+。沒有額外 pip 依賴。App 必須保留原 repo 與 Python 路徑；搬移後重建。可把編譯後的 `.local/Meeting Desk.app` 固定到 Dock。

## 檔案與相容

新增 `SourceKind.MANAGED_IMPORT`，與 managed download 共用 exclusive copy；正確記錄 import ownership，不冒充網路下載。原音檔保留原位，副本寫入 timestamped workspace。

既有 artifact 命名與 `<stem>_pipeline_state.json` 語義不變、不被 GUI 修改。新增：

- `desktop_state.json`：title、status、attempts、raw/prepared hashes、profile、prompt hash、handoff、quality。
- `.desktop.lock`：flock 鎖檔，每場會議同時只有一個 GUI mutation。鎖檔可以存在，鎖在 process 結束後自動釋放。
- `llm_handoff/clean-<timestamp>.txt` / `summary-<timestamp>.txt`：私有、版本化交接包，含選定 prompt 與逐字稿。只能保存在會議資料夾，不能提交 Git。

GUI 清單可以讀取 legacy raw workspace；單一資料夾損壞／多份 raw 時列出警告，不阻擋其他會議。首次不自動選擇最新會議。

## 完成與恢復

1. 匯入時間優先讀取檔名、音訊 metadata、檔案時間，始終顯示給使用者修改／確認；台北時區，不以處理當下時間冒充錄音時間。
2. 轉錄成功後先保存 raw，再產生 prepared + manifest。已有 raw 時不重跑 Whisper；已有 prepared 必須與 manifest 的 source/output SHA-256 一致才重用。原始產物不覆寫。
3. 取消傳到現有 core；core 負責終止／回收子程序、清掉本次 partial artifacts。音檔副本保留。失敗／取消保存 attempt 結果，可回同一會議重試。
4. 強制關閉後，`running` 不代表成功；重啟顯示可續跑。已有來源／輸出衝突時 fail closed，不能為了續跑刪除不明產物。
5. 清洗交接必須選定有本機 note 的 profile；packet 記錄 prepared/raw/prompt hashes。缺本機 note 時停止，沿用 README 的 prompt 同步入口。
6. 匯入清洗稿需匹配交接的來源 hashes、非空、縮短不超過 20%；以人工訂正稿（沒有才用 prepared）字元數為主要長度基準。檢查通過後仍是 `pending_review`，不是語意保真的自動證明。
7. 使用者對照檢查後才設為 `passed`，保存 reviewer/time；展示與 summary 交接重新檢查 raw/prepared/cleaned hashes。檔案外部修改會失去已確認狀態。
8. 已存在的 cleaned 不覆寫。可選取同一個 cleaned 檔做重新驗證／採納 legacy 產物；若匯入不同檔案，會另存人工版本目錄並更新目前路徑，舊版仍保留。
9. Notion 明確保留 pending；交接包不授權遠端發布。summary 交接帶入 repo 的 meeting-summary-spec，要求 coverage、證據、決議／提案區分。

Settings 僅寫 `.local/desktop_settings.json`，不改 `.env`。模型切換不重做既有 raw；切換資料根目錄只切換清單，不搬移既有檔案。

## 驗證

`python3 -m unittest discover -s tests -p test_desktop_service.py -v`：真實暫存檔＋fake ffmpeg/whisper，涵蓋匯入 ownership、防覆寫、model 設定、續跑、失敗／取消、manifest、交接與品質檢查、legacy adoption、鎖及壞資料夾隔離。

`bash scripts/build-desktop.sh`：原生編譯與 codesign。完整回歸使用 `bash tests/run_tests.sh`；downloader socket 測試需允許綁定 loopback，無外部網路。

`tests/desktop_editor_snapshot.swift` 可與 `desktop/MeetingDesk.swift` 以 `-D DESKTOP_UI_TEST` 編譯，輸入輸出資料夾路徑即可擷取 TXT、SRT、名稱編輯視窗。只使用內建虛構內容，不讀寫使用者會議或呼叫 backend。
