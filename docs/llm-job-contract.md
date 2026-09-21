# Meeting Desk 私有 LLM Job 契約

**Schema version：** 1
**適用階段：** `clean`、`notes`
**資料邊界：** 本機私有；Git 只追蹤本契約，不追蹤實際 job 或會議內容。

## 固定目錄

```text
<MeetingRecords>/.llm_jobs/
  inbox/<job-id>.json
  work/<job-id>/segments/seg-01.txt
  work/<job-id>/segments.json
  outbox/<job-id>/<expected-output>
  outbox/<job-id>/result.json
  archive/
```

`.llm_jobs/`、`llm_handoff/`、`manual_revisions/` 與 `notes_drafts/` 均由 `.gitignore` 排除。Desk 建立的私有目錄使用 `0700`，檔案使用 `0600`。

## Canonical 欄位對照

本表是 schema version 1 的欄位名稱權威來源。Request JSON 是 agent 的輸入契約；handoff record 只存在於 `desktop_state.json`，是 Desk 的內部索引；Result JSON 是 agent 唯一可寫的完成契約。

| 概念 | Request JSON | Handoff record | Result JSON |
|---|---|---|---|
| Schema | `schema_version` | 無 | `schema_version` |
| Job 識別 | `job_id` | `job_id` | `job_id` |
| 階段 | `stage` | `stage` | `stage` |
| 建立狀態 | `status = queued` | `status = queued` | 無 |
| 完成狀態 | 不修改 | Desk 匯入後改為 `imported` | `status = done` |
| 會議資料夾 | `meeting.path` | 不重複保存 | 無 |
| 會議名稱 | `meeting.title` | 不重複保存 | 無 |
| 輸入 | `input.path`／`sha256`／`bytes` | `input_path`／`input_hash` | 無 |
| Profile | `profile.key`／`label`／`path`／`sha256`／`bytes` | `profile`／`profile_hash` | 無 |
| Request 完整性 | Request 檔案本身 | `request_path`／`request_hash` | 無 |
| 預期輸出 | `expected_result.output_path` | `expected_output` | `output_path` |
| 完成標記 | `expected_result.result_path` | `result_path` | Result JSON 本身 |
| 輸出 hash | 無 | 匯入後的 `output_hash` | `output_sha256` |
| 階段稽核 | `timeline.sha256` 等 reference hashes | `raw_hash`／`timeline_hash`／`cleaned_hash`／`prompt_hash`（依階段） | 無 |

不得使用 `meeting_dir`、`cleaned_path`、`expected_result.expected_output` 或其他別名。兩個階段的 handoff record 都使用 `input_path`；`expected_output` 是 Desk handoff record 的扁平欄位，不是 Request JSON 欄位。既有本機 state 中的舊別名只供相容讀取，不得寫入新 job。

## Request JSON

以下只是假路徑與假 hash。Request 不得內嵌 prompt、逐字稿、SRT、coverage spec 或 agent 輸出內容。

```json
{
  "schema_version": 1,
  "job_id": "clean-20260917T010203-a1b2c3d4e5",
  "stage": "clean",
  "skill": "clean-meeting-transcripts",
  "status": "queued",
  "private_local_only": true,
  "meeting": {
    "path": "/local/MeetingRecords/2026-09-17_0900_example",
    "title": "Example meeting"
  },
  "profile": {
    "key": "inno",
    "label": "Inno Team",
    "path": "/local/private-profiles/inno.md",
    "sha256": "<sha256>",
    "bytes": 1234
  },
  "input": {
    "path": "/local/MeetingRecords/..._transcription_prepared.txt",
    "sha256": "<sha256>",
    "bytes": 45678
  },
  "timeline": {
    "path": "/local/MeetingRecords/..._transcription.srt",
    "sha256": "<sha256>",
    "bytes": 56789
  },
  "segments_manifest": {
    "path": "/local/MeetingRecords/.llm_jobs/work/<job-id>/segments.json",
    "sha256": "<sha256>",
    "bytes": 2345
  },
  "expected_result": {
    "output_path": "/local/MeetingRecords/.llm_jobs/outbox/<job-id>/example_transcription_cleaned.txt",
    "result_path": "/local/MeetingRecords/.llm_jobs/outbox/<job-id>/result.json"
  }
}
```

`notes` request 使用 `standup-worklog` skill，以已人工確認的 cleaned TXT 為 `input`，並增加 `specification` 路徑／hash；輸出副檔名為 `.md`。notes 不得重新清洗逐字稿，也不得自行發布到 Notion。

Request 必填欄位固定為：`schema_version`、`job_id`、`stage`、`skill`、`status`、`created_at`、`private_local_only`、`meeting`、`profile`、`input`、`expected_result`。選填欄位只有 `timeline`、`specification`、`segments_manifest`；新增欄位必須提升 schema version。

## 領取與狀態語意

- Request JSON 建立後不可修改，`status` 永遠保持 `queued`。Agent 不得將它改成 `running`，也不得修改 `desktop_state.json`。
- Schema version 1 是單一 consumer queue：同一個 `.llm_jobs` queue 同時間只能有一個 worker 掃描與處理。`list_jobs` 是只讀檢視，不代表領取。
- Desk 不接受 agent 寫入的 `running` 狀態；它只檢查固定 outbox 中的 `result.json`，驗證成功時衍生顯示為 `done`。
- Desk 匯入結果後，只有 Desk 可以把 handoff record 更新為 `imported`。
- 若未來需要多個 bot 並行，必須提升契約版本並加入原子 claim／lease；不得用修改 request JSON 的方式模擬 claim。

## Clean segments

每個 `seg-NN.txt` 有三區：

```text
[CONTEXT_BEFORE]
只供理解

[CORE]
本段唯一需要輸出的內容

[CONTEXT_AFTER]
只供理解
```

`segments.json` 的 `merge_rule` 是權威規則：依 `output_order` 合併每段 CORE；前後 context 不可再次輸出。每個 SRT cue 只屬於一個 core。TXT 人工訂正版仍是語意權威來源，SRT segments 用於時間切段、順序與邊界理解。

## Agent 寫回順序

1. 讀 request 指定的 skill、profile、input、timeline、manifest 與 specification。
2. 重新計算來源 hash；不一致時停止，不處理舊 job。
3. 先將完整輸出寫到暫存檔，完成後原子移到 `expected_result.output_path`。
4. 計算輸出 SHA-256。
5. 最後才寫 `result.json`；它是 Desk 判定 agent 已完成的標記。
6. 不修改 request JSON、`desktop_state.json`、raw、prepared、SRT、cleaned 或 profile。

Result 格式：

```json
{
  "schema_version": 1,
  "job_id": "clean-20260917T010203-a1b2c3d4e5",
  "stage": "clean",
  "status": "done",
  "output_path": "/local/MeetingRecords/.llm_jobs/outbox/<job-id>/example_transcription_cleaned.txt",
  "output_sha256": "<sha256>",
  "completed_at": "2026-09-17T01:23:45+00:00"
}
```

Desk 拒絕以下結果：job ID／stage 不符、輸出位置被改寫、來源或 profile/spec/segment 已變更、輸出空白、output hash 不符。clean 結果通過機械檢查後仍需人工確認；notes 草稿匯入本機也不代表已發布。

## 只讀入口

Desktop JSON 入口支援以下只讀 action；它們只回傳 metadata，不回傳逐字稿、profile、segment 或 notes 內容，也不建立或修改檔案：

```json
{"action": "list_jobs"}
```

`list_jobs` 依建立時間列出 inbox jobs，狀態為 `queued`、`done`、`stale` 或 `invalid`。如果只有輸出檔、還沒有 `result.json`，仍是 `queued`，並回傳 `partial_output: true`。

```json
{
  "action": "validate_job",
  "request_path": "/local/MeetingRecords/.llm_jobs/inbox/clean-20260917T010203-a1b2c3d4e5.json"
}
```

`validate_job` 只接受目前 output root 的 `inbox/*.json`，驗證 canonical 欄位、固定路徑、來源 hashes、segments 及已存在的 result；驗證失敗不會移動、刪除或修復任何檔案。

## Handoff dry-run

```json
{
  "action": "handoff",
  "folder": "/local/MeetingRecords/2026-09-17_0900_example",
  "profile": "inno",
  "summary": false,
  "dry_run": true
}
```

Dry-run 回傳預覽用 `job_id`、stage、輸入與 profile hashes、預期 request/result/output 路徑、切段數和每段核心時間範圍。它不建立 `.llm_jobs`、segments、request、lock 或 handoff state。預覽用 `job_id` 不會保留；正式建立時一定產生新的 ID。
