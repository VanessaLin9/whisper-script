# Meeting Desk Agent 指南

## 這個 repo 是什麼

`whisper-script` 是 macOS 上的會議錄音、Whisper 轉錄與 Meeting Desk 本機工作流程。Meeting Desk 會把音訊整理成會議資料夾，產生 raw／預清洗稿，再透過本機 `.llm_jobs` 交接給 agent；agent 只寫 outbox 結果，Desk 負責驗證、人工確認與匯入。

## CLI 呼叫

從 repo 根目錄以一行 JSON 呼叫：

```bash
echo '{"action":"help"}' | python3 -m src.desktop
echo '{"action":"list_jobs"}' | python3 -m src.desktop
```

stdout 可能先有 `type=progress` 的進度事件，最後一定是 `type=result`。成功結果是 `{"type":"result","ok":true,...}`；失敗結果是 `{"type":"result","ok":false,"error":"..."}`。完整且最新的 action、必要／選填參數、回傳欄位與是否可取消，請以 `help` 為準；`{"action":"help","name":"list_jobs"}` 可只查一個 action。

## 會議資料根目錄

Desk 依下列順序決定根目錄，前者存在設定時優先：

1. `.local/desktop_settings.json` 的 `output_root`
2. repo `.env` 的 `MEETING_RECORDS_DIR`
3. `~/MeetingRecords`

測試或 agent 開發時，請將 `output_root` 指向暫存目錄。不要讀寫正式的 `/Users/user/MeetingRecords`，也不要修改 `.env` 或 `.local/`。

## 典型流程

1. 轉寫音訊，產生 raw TXT／SRT。
2. `process` 完成 deterministic 預清洗；`handoff` 建立 clean job。
3. agent 依 [docs/llm-job-contract.md](docs/llm-job-contract.md) 讀 inbox，將清洗稿與 result JSON 寫到 outbox；Desk 用 `import_job` 或 `import_cleaned` 匯入。
4. 人工檢查清洗稿後呼叫 `review`。這是人工關卡，會自動建立 notes job。
5. agent 完成 notes job 後呼叫 `import_notes`；寫入 Notion 仍必須取得人同意，本 repo 不會自動發布。

## MCP server

Meeting Desk 另提供選用的本機 MCP stdio server。建議使用虛擬環境安裝依賴：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-mcp.txt
.venv/bin/python -m src.mcp_server
```

Cursor `mcp.json` 範例（將 `/絕對路徑/whisper-script-mcp` 換成 repo 路徑）：

```json
{
  "mcpServers": {
    "meeting-desk": {
      "command": "/絕對路徑/whisper-script-mcp/.venv/bin/python",
      "args": ["-m", "src.mcp_server"],
      "cwd": "/絕對路徑/whisper-script-mcp"
    }
  }
}
```

Codex `~/.codex/config.toml` 可使用：

```toml
[mcp_servers.meeting-desk]
command = "/絕對路徑/whisper-script-mcp/.venv/bin/python"
args = ["-m", "src.mcp_server"]
cwd = "/絕對路徑/whisper-script-mcp"
```

Windows 對應寫法：`command = "C:\\絕對路徑\\whisper-script-mcp\\.venv\\Scripts\\python.exe"`，`args = ["-m", "src.mcp_server"]`，並將 `cwd` 設為 repo 絕對路徑。

開放工具為 `help`、`environment`、`list`、`list_jobs`、`preview`（唯讀），以及 `handoff`、`import_cleaned`、`import_job`、`import_notes`（交接用寫入）。`review` 保留給 Vanessa 的人工關卡；`settings`、`rename`、`save_edit`、`import`、`process`、`inspect` 不開放，以避免 MCP 直接改設定、人工內容或啟動長任務。寫入 Notion 不是 MCP 工具，仍須人同意。

換電腦時重新 clone repo、建立 venv、安裝 `requirements-mcp.txt`，確認 Meeting Desk 的環境與本機提示詞設定，再把上述範例中的絕對路徑改成新電腦路徑；不要搬移或提交 `.env`、`.local/` 或 `~/.codex` 設定。

### Agent 不得動的檔案

Agent 不得修改 inbox request、`desktop_state.json`、raw、prepared、SRT 或音訊。也不要直接覆寫 cleaned；清洗結果必須依 job 契約寫入 outbox，再由 Desk 驗證匯入。不要把逐字稿內容放進 job JSON 或聊天交接。

## 測試

測試不使用正式會議資料；請沿用測試的暫存隔離方式。完整回歸測試：

```bash
bash tests/run_tests.sh
```

Job 的欄位、路徑、hash、outbox result 與匯入限制，請看 [docs/llm-job-contract.md](docs/llm-job-contract.md)。

## 平台相關層

`src/platform_support.py` 集中處理作業系統判斷、預設路徑、`.desktop.lock` 檔案鎖、執行檔查找、剪貼簿與在資料夾中顯示。macOS／Linux 的預設會議根目錄是 `Path.home() / "MeetingRecords"`；Windows 的設定與資料預設分別位於 `%APPDATA%/WhisperScript` 與 `%LOCALAPPDATA%/WhisperScript`。

環境變數優先順序如下：明確的 action／desktop settings 設定，其次是既有的 `MEETING_RECORDS_DIR`、`WHISPER_ROOT`、`PROMPT_NOTES_DIR` 或執行檔專用變數，最後才使用平台預設值。設定目錄可用 `WHISPER_SCRIPT_CONFIG_DIR` 覆寫，資料目錄可用 `WHISPER_SCRIPT_DATA_DIR` 覆寫。執行檔可用 `WHISPER_CLI`、`FFMPEG`、`FFPROBE` 覆寫。
