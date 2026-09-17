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
