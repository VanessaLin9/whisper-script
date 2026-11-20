# Meeting Desk 私有 LLM Job 契約

這是 Meeting Desk 與本機 agent 之間的檔案交接契約。`schema_version` 目前為 `1`。實際 job 只存在會議資料根目錄，Git 不追蹤會議內容。

## 目錄與權責

```text
<MeetingRecords>/.llm_jobs/
├── inbox/<job-id>.json
├── work/<job-id>/segments/seg-01.txt
├── work/<job-id>/segments.json
├── outbox/<job-id>.txt                 # clean 的相容非分段輸出
├── outbox/seg-01.txt                   # clean 分段輸出，實際路徑以 item.outbox_path 為準
├── outbox/<job-id>.json
├── outbox/<job-id>-coverage.json       # notes
├── outbox/<job-id>-notes.md            # notes
└── archive/
```

Desk 建立 inbox request、分段檔與 desktop state；agent 只讀 request 指定的輸入，並在 request 的 `expected_output` 指定位置寫結果。agent 不得修改 inbox request、`desktop_state.json`、音訊、raw、prepared、SRT、cleaned 或提示詞 profile。

## 共用 Request 欄位

目前 `src/desktop/jobs.py` 實際建立的 request 欄位如下：

```json
{
  "schema_version": 1,
  "job_id": "clean-<uuid>",
  "stage": "clean",
  "status": "queued",
  "created_at": "2026-09-17T01:23:45+08:00",
  "meeting_dir": "/local/MeetingRecords/2026-09-17_0900_example",
  "meeting_title": "Example meeting",
  "profile": {"key": "inno", "path": "/local/profile.md", "sha256": "<sha256>"},
  "inputs": {},
  "expected_output": {},
  "instructions": "..."
}
```

`contract_digest()` 會對除了 `status` 以外的所有欄位做 canonical JSON SHA-256。Desk 匯入時會拒絕被改寫的 request；agent 只能在自己的 outbox 產生結果。`status` 的正常流程是 request `queued`，agent 結果 `done`，Desk 匯入後將 job 封存並記為 `imported`；來源變更時 Desk 會標成 `stale`。

## clean job

`inputs` 至少包含：

```json
{
  "transcript": {"path": "..._transcription_prepared.txt", "sha256": "...", "role": "prepared"},
  "srt": {"path": "..._transcription.srt", "sha256": "...", "role": "original"},
  "vocab": {"path": "meeting_vocab.md", "sha256": "..."}
}
```

不存在的 SRT 或 vocab 會是 `null`。transcript 可能是 `raw`、`prepared` 或 `corrected`。`expected_output` 至少包含 `meeting_path`、`outbox_path`、`result_path` 與 `max_reduction`；有分段時還會有頂層 `segments`：其 `items` 各有 `id`、`output_order`、`path`、`sha256`、`outbox_path`，以及 manifest 的 `directory`、`manifest_path`、`sha256`。

agent 應讀 `inputs.transcript.path`、SRT、vocab 與分段檔，將每個 segment 的核心內容清洗後寫到各 item 的 outbox path，不能把 context 或分段標記寫入輸出。結果 JSON 寫到 `expected_output.result_path`，至少包含：

```json
{
  "schema_version": 1,
  "job_id": "clean-<uuid>",
  "stage": "clean",
  "status": "done",
  "segments": [{"id": "seg-01", "output_order": 1, "path": "...", "sha256": "..."}]
}
```

沒有 segments 的舊／相容 job 可用 `output` 物件回傳 `path` 與 `sha256`。Desk 會驗證 job id、stage、來源 hash、profile hash、分段 hash、輸出位置、非空內容，並拒絕清洗後比輸入縮短超過 20%。

## notes job

notes job 由人工 `review` 成功後自動建立，或由 `handoff` 的 `summary: true` 建立。它的 `inputs` 為：

```json
{
  "cleaned": {"path": "..._transcription_cleaned.txt", "sha256": "..."},
  "specification": {"path": "repo/docs/meeting-summary-spec.md", "sha256": "..."},
  "vocab": null
}
```

如果使用人工訂正版，cleaned path 會在 `manual_revisions`。`expected_output` 包含 `coverage_path`、`draft_path`、`result_path`。agent 先寫 coverage map，再寫繁體中文 notes 草稿，最後寫 result JSON：

```json
{
  "schema_version": 1,
  "job_id": "notes-<uuid>",
  "stage": "notes",
  "status": "done",
  "coverage": {"path": "...-coverage.json", "sha256": "..."},
  "draft": {"path": "...-notes.md", "sha256": "..."}
}
```

coverage 的 `topics` 每項必須含 `topic`、`source_span`、`classification`、`evidence`、`owner_evidence`、`included_in`、`uncertainty`；`classification` 只能是 `progress`、`action`、`decision`、`proposal`、`blocker`、`dependency`、`open_question`。notes job 不得改寫 cleaned 或寫入 Notion。

## Agent 操作順序與人工關卡

1. 讀 inbox request 並重新計算所有輸入 hash；不一致就停止。
2. 先以暫存檔完成輸出，再原子移到 request 的預期路徑。
3. 計算輸出 SHA-256，最後才寫 result JSON。
4. 由 Desk `import_job`／`import_notes` 驗證並封存。

cleaned 匯入後仍是 `pending_review`；只有人按 `review` 才算確認，並觸發 notes job。notes 匯入本機不等於發佈到 Notion；任何外部發佈都必須另取得人同意。

## 只讀查詢

CLI 的 `{"action":"list_jobs"}` 只讀取 inbox，`include_archive: true` 時也讀 archive；可用 `stage`、`status`、`folder` 篩選。`{"action":"help"}` 是 action 參數的機器可讀權威來源。
