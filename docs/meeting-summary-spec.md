# 會議記錄產出規格

**版本：** 0.1  
**最後更新：** 2026-08-07  
**適用流程：** cleaned transcript → 會議記錄草稿 → Notion meeting page  
**目前執行者：** Codex（LLM API adapter 尚未啟用）

這份文件定義「用 cleaned transcript 產生會議記錄」的內容、格式、證據與驗證規則。它是未來 LLM API prompt、local desktop tool 與 Notion adapter 的共同契約；實作可以更換，但輸入、輸出與品質要求不能任意改變。

## 1. 目標與非目標

### 目標

- 產出一篇可快速閱讀、可追溯、可重試的單場會議記錄。
- 保留所有實質討論主題，不因摘要要短而整段刪除。
- 清楚區分進度、待辦、決議、提案、阻塞、依賴與待確認事項。
- 讓每個 owner、期限、名稱、ID、技術詞都能回到逐字稿或明確宣告的 context。
- 讓相同規則可由 Codex、LLM API 或未來桌面工具產生相近結果。

### 非目標

- 不重新清洗逐字稿；輸入必須是已通過 `clean-meeting-transcripts` 的 cleaned TXT。
- 不把不確定的 ASR 片段猜成正式名稱、owner、日期或決議。
- 不把討論中的想法寫成已定案決策。
- 不合併不同會議，也不把內容追加到 `工作日誌 - YYYY/MM/DD` 聚合頁。
- 不直接讓 LLM 寫入 Notion；必須先產生草稿、預覽、確認，再由 adapter 寫入與驗證。

## 2. 輸入契約

### 必要輸入

1. `*_transcription_cleaned.txt`
2. meeting metadata：
   - `meeting_id`
   - `date`
   - `start_time`
   - `end_time`（若可得）
   - `timezone`，預設 `Asia/Taipei`
   - workspace / source path
3. selected prompt profile 的識別資訊：`name`、`label`、source page ID 或 local note path、revision/hash（若有）。

### 可選輸入

- matching SRT，僅用來回查時間軸與定位來源，不取代 cleaned TXT。
- 使用者明確提供的團隊／參與者 roster、會議 metadata 或 glossary。
- 外部 reference summary。它只能作為 coverage 與 actionability 的比較基準，不能覆蓋 transcript evidence。

### 輸入保護

- raw TXT、SRT、prepared TXT、音訊與 JSON 不得被修改。
- cleaned TXT 不存在時停止；不得靜默改用 raw ASR 產生會議記錄。
- LLM API 失敗時保留本機草稿與中間 JSON，不得直接寫入半成品 Notion 頁面。

## 3. 兩階段產出流程

### Stage A：議題覆蓋表（coverage map）

先讀完整 cleaned transcript，再建立機器可驗證的 coverage map；不要一邊讀一邊直接寫摘要。

每個 substantial topic 至少要記錄：

| 欄位 | 要求 |
|---|---|
| `topic` | 簡短且不過度推論的主題名稱 |
| `source_span` | 段落、時間軸或可回查的來源位置 |
| `classification` | `progress`、`action`、`decision`、`proposal`、`blocker`、`dependency`、`open_question` |
| `evidence` | 支持該分類的逐字稿內容或 context 來源 |
| `owner_evidence` | owner 證據；沒有就留空或標記待確認 |
| `included_in` | 最終會議記錄中的 section |
| `uncertainty` | 未確認的名稱、日期、數字或技術片語 |

Coverage 規則：

- 每個實質主題必須出現在至少一個最終 section，或明確記錄 `omitted` 與原因。
- 參考摘要遺漏的 transcript 主題仍要保留。
- 參考摘要新增但 transcript/context 無法證實的資訊，不得直接採用。

### Stage B：會議記錄草稿

依 coverage map 產出 Notion Markdown 與中間 JSON。先完成草稿和品質檢查，再進入 Notion。

## 4. 內容分類規則

### 目前進度

已完成、進行中、已驗證或正在修正的工作。只描述 transcript 明確支持的狀態。

### 今日計畫／下一步

只有明確說出的 action、交付物、測試、聯絡、排程或 follow-up 才能使用 checkbox。

動作用可驗證的動詞開頭，例如：

```text
- [ ] 重新執行數目表、Action trigger 與回覆格式測試
- [ ] 確認最終 Beta 展示時間與形式
```

若 action 清楚但 owner 或期限不清楚：

```text
- [ ] 更新機敏資訊遮擋規則（Owner：[待確認]；期限：[待確認]）
```

不要把「持續關注」「處理一下」「應該可以」直接轉成 checkbox。

### 阻塞與風險

記錄 ASR 不確定性、搜尋不穩定、缺少 key、依賴未完成、資料分類可能漏抓等會影響後續工作的因素。

### 決策與依賴

只放已確認的共識、選定方案或明確依賴。建議、假設、仍在比較的方案必須另標示為「討論中的提案」，不可放在 confirmed decisions。

### 待確認

集中列出：

- `[待確認：專有名詞]`
- `[待確認：owner]`
- `[待確認：日期／期限]`
- `[待確認：方案是否定案]`
- `[內容不清，需回聽]`

不確定詞要盡量縮小到詞或句子，不要讓整段內容失去可讀性。

## 5. Owner、日期與專有名詞規則

- owner、deadline、人名只能來自清楚的 transcript 證據，或使用者明確提供且宣告可用的 roster／meeting context。
- 不能因另一份 reference summary 寫了某個 owner，就把該 owner 當成事實。
- 只有名稱拼法明確、且與 prompt profile／context 一致時才可自動校正。
- 無法確認的術語保留 `[待確認：疑似 XXX]`，不能因 glossary 出現就強制替換。
- 技術詞、ticket key、版本號、數字與日期不可被泛化成「相關工作」。

## 6. 固定 Notion Markdown 格式

每場會議一篇頁面。頁面 property title 與內文 heading 必須一致。

```markdown
## Stand-up｜YYYY-MM-DD HH:mm

## 會議資訊

- 時間：YYYY-MM-DD HH:mm–HH:mm（Asia/Taipei）
- 類型：Daily stand-up
- 提示詞：<profile label>
- 清洗來源：`<absolute cleaned transcript path>`
- 註：無法確認的專有名詞、owner、日期與技術片語保留為「[待確認]」。

## 討論主題與目前進度

- **主題**：...

## 今日計畫／下一步

- [ ] ...

## 阻塞與風險

- ...

## 決策與依賴

### 目前共識

- ...

### 討論中的提案／依賴

- ...

## 待確認

- ...

## 附件

清洗後逐字稿檔案：

<file src="file-upload://..."></file>
```

非 stand-up 會議使用 `會議記錄｜YYYY-MM-DD HH:mm`，但 section 與證據規則相同。

## 7. LLM API prompt 與中間 JSON 契約

LLM prompt 應要求模型只產生中間 JSON，不直接產生 Notion API payload。adapter 再把通過驗證的 JSON 渲染成 Markdown。

建議中間 JSON：

```json
{
  "schema_version": 1,
  "meeting": {
    "meeting_id": "12",
    "date": "2026-08-07",
    "start_time": "10:17",
    "end_time": "10:36",
    "timezone": "Asia/Taipei"
  },
  "source": {
    "cleaned_path": ".../12_transcription_cleaned.txt",
    "cleaned_sha256": "...",
    "prompt_profile": "inno",
    "prompt_revision": "..."
  },
  "coverage": [
    {
      "topic": "...",
      "source_span": "...",
      "classification": "progress|action|decision|proposal|blocker|dependency|open_question",
      "evidence": "...",
      "owner": null,
      "owner_evidence": null,
      "deadline": null,
      "uncertain": []
    }
  ],
  "sections": {
    "progress": [],
    "next_steps": [],
    "blockers": [],
    "decisions": [],
    "proposals_dependencies": [],
    "uncertain": []
  },
  "validation": {
    "coverage_complete": false,
    "unsupported_claims": [],
    "owner_or_deadline_without_evidence": [],
    "decision_proposal_conflicts": [],
    "status": "draft"
  }
}
```

### Prompt 必須要求

- 讀完整 cleaned transcript。
- 先建立 coverage，再產生 sections。
- 不得把 reference summary 當成事實來源。
- 不得發明 owner、deadline、決議、背景或技術細節。
- 不確定項目必須進 `uncertain` 或 `[待確認]`。
- `decision` 與 `proposal` 必須分開。
- 輸出只能是符合 schema 的 JSON；無法判斷時使用 `null`、空陣列或 uncertainty marker。

## 8. 品質閘門

Notion 寫入前必須全部通過：

1. cleaned transcript path 存在，且 source hash 與 draft 記錄一致。
2. coverage map 中每個 substantial topic 都被納入，或有明確 omitted reason。
3. 沒有未經證據支持的 owner、deadline、人名、數字或技術詞。
4. 沒有把 proposal 寫成 decision。
5. 所有 checkbox 都對應 transcript 中明確 action。
6. `[待確認]`、ticket ID、版本號、API／模型名稱被保留。
7. 會議頁有正確日期、時間、profile、source path。
8. cleaned TXT 已上傳為 Notion file block，且 fetch 可驗證附件存在。
9. Notion 既有同標題頁面時不得重建重複頁；頁面已建立但附件失敗時只重試附件。
10. 若 cleaned transcript 仍含敏感資訊且未獲得 Notion 寫入確認，停止在 pending。

## 9. 目前執行狀態與未來 adapter 界線

- 目前由 Codex 讀取 prompt profile、建立 coverage、撰寫與驗證 Markdown。
- LLM API adapter 尚未啟用；接入時不得改變本文件定義的 sections、classification、uncertainty 與 quality gates。
- Notion write 必須是獨立、可確認、可重試的 stage。
- API timeout、rate limit、schema validation 或 Notion 權限錯誤時，保留本機 draft JSON／Markdown，不得標記整場完成。
- API token、完整 prompt body、完整逐字稿不得寫入 Git 或 pipeline state。

## 10. 驗收案例

- 同一份 cleaned transcript 重跑兩次，產出相同 coverage 與穩定 page title，不新增第二篇頁面。
- reference summary 包含 transcript 未證實的 owner 時，draft 將 owner 留空或標示 `[待確認]`。
- transcript 同時包含 confirmed decision 與 alternative proposal 時，兩者分別出現在不同分類。
- transcript 有五個 substantial topics 時，final record 不得只留下兩個最容易摘要的主題。
- Notion page 建立成功但 cleaned TXT 上傳失敗時，retry 只補附件，不重建 page。

## 11. 關聯文件

- [工作契約流程.md](工作契約流程.md)
- [meeting-automation-spec.md](meeting-automation-spec.md)
- [meeting-pipeline-maintenance.md](meeting-pipeline-maintenance.md)
- `standup-worklog` skill：`/Users/user/.codex/skills/standup-worklog/SKILL.md`
- `clean-meeting-transcripts` skill：`/Users/user/.codex/skills/clean-meeting-transcripts/SKILL.md`
