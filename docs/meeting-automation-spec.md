# Meeting Automation Pipeline Specification

**Version:** 0.1 (research snapshot)  
**Date:** 2026-08-06  
**Status:** Draft / implementation-oriented  
**Owner:** AI Team

## 1. Purpose

Define the first implementable version of a local meeting workflow that can eventually be started from one desktop button:

1. record a meeting;
2. transcribe it with whisper.cpp;
3. apply deterministic pre-cleaning;
4. clean the transcript semantically with an LLM and a selected prompt profile;
5. create one readable Notion meeting page per meeting.

The design deliberately separates mechanical file operations from semantic judgment. The current CLI controller orchestrates the former and hands the latter to Codex skills; later versions may replace the hand-off with API adapters.

## 2. Scope and non-goals

### In scope

- Local workspace discovery under `/Users/user/MeetingRecords`.
- A required meeting selection step.
- A required prompt-profile selection step.
- A selectable workflow mode (`full`, `preclean`, `handoff`).
- Whisper/ASR artifacts in a timestamped meeting folder.
- Safe, repeatable deterministic pre-cleaning.
- LLM transcript cleaning that preserves meaning, order, questions, answers, examples, numbers, and technical details.
- A separate Notion database page for every meeting; meetings must not be appended to a daily work-log page.
- Pipeline state, resumability, dry-run/confirmation points, and failure visibility.

### Not in this version

- Final desktop UI implementation.
- Direct LLM API calls from the current CLI.
- Direct Notion writes from the current CLI.
- A completed OAuth flow.
- Automatic migration of every legacy recording/transcription artifact.
- Automatic interpretation of uncertain speech beyond explicitly marked uncertainty.

## 3. Current source of truth

### Existing local components

- Repository: `/Users/user/whisper-script`
- Current controller: `src/meeting_pipeline.py`
- Deterministic pre-cleaner: `src/postprocessing/preparer.py`
- Recording script: `scripts/record-meeting.sh`
- Voice Memo/local-file transcription and workspace organizer: `scripts/transcribe-english.sh`
- Tests: `tests/test_meeting_pipeline.py` and post-processing tests.

### Existing Codex skills

- `clean-meeting-transcripts`: semantic transcript cleaning; it must not turn a transcript into meeting minutes unless separately requested.
- `standup-worklog`: creates a dated Notion work-log/meeting entry. The current rule is one database page per meeting, never merge two meetings into a daily page.

### Known prompt profiles

| Key | Meaning | Source |
|---|---|---|
| `inno` | Inno Group / AI Team | Notion page ID `39ddf30c-c71c-81b6-a658-f0ec07fd7e36` |
| `whisper` | Civil-engineering-related meetings | Notion page ID `whisper-3a3df30c-c71c-8151-95bad39aeb1eb6e1` |
| `new` | Prompt not yet registered; create/finish the profile later | `not-registered` |

The CLI labels the `inno` profile as **Inno Group／AI Team**. The profile registry must be the single place to add future profiles.

## 4. Proposed end-to-end flow

```text
Record or select audio
        ↓
Organize timestamped meeting workspace
        ↓
whisper.cpp transcription (TXT/SRT)
        ↓
Deterministic pre-clean (mechanical only)
        ↓
Ask for meeting + prompt profile + mode + write confirmation
        ↓
LLM semantic transcript cleaning
        ↓
Generate meeting record from cleaned transcript
        ↓
Preview / confirm
        ↓
Create or update one Notion meeting page
        ↓
Persist state, artifacts, hashes, and errors
```

The current CLI begins at the timestamped workspace and performs discovery, questions, pre-cleaning, and state creation. LLM and Notion stages are explicit skill hand-offs until their adapters are implemented.

## 5. Workspace and artifact contract

### Folder naming

Preferred folder format:

`YYYY-MM-DD_HHMM_<meeting-id>/`

The current controller discovers this format and reads one raw `<stem>_transcription.txt` from each folder. Optional source artifacts include the matching `<stem>_transcription.srt` and audio file.

### Artifact naming

- raw transcript: `<stem>_transcription.txt`
- SRT timeline: `<stem>_transcription.srt`
- deterministic output: `<stem>_transcription_prepared.txt`
- semantic cleaned transcript: `<stem>_transcription_cleaned.txt`
- controller state: `<stem>_pipeline_state.json`
- deterministic manifest/statistics: generated beside the prepared output by `preparer.py`

Never overwrite an existing artifact by default. Use an explicit force/refresh action and retain enough state to understand what changed.

### Recording compatibility

`record-meeting.sh` now captures a higher-rate raw PCM WAV in a staging directory,
retains a copy in the timestamped meeting workspace, and derives the 16 kHz mono
Whisper input from that raw file. `transcribe-english.sh` still supports existing
local-reference files without copying them, so both entry points remain
compatible while the raw-retention policy is introduced incrementally.

## 6. Required user inputs and UX

The first run must ask:

1. **Which meeting?** Show discovered meetings, time, ID, and whether prepared/cleaned artifacts already exist. Accept list number, meeting ID, or timestamp selector.
2. **Which prompt profile?** Show registered profiles (`inno`, `whisper`) and an explicit `new/not registered` path. Do not silently invent a profile.
3. **Which mode?** `full`, `preclean`, or `handoff`.
4. **May the final content be written to Notion?** Keep this confirmation separate from local file processing.

For the desktop tool, these become a compact setup screen or defaults with an expandable confirmation panel. Progress should expose the current stage, output path, retry action, and the reason for any pause.

## 7. Stage responsibilities

### 7.1 Recording and transcription

- Start/stop recording locally.
- Preserve the original audio.
- Pass an optional vocabulary/prompt to whisper.cpp for project names and proper nouns (for example, Greek-mythology project names).
- Write a timestamped workspace and a transcript with timeline information when available.
- Treat whisper prompt hints as recognition aids, not authoritative facts.
- The local adapter passes an optional short initial prompt through whisper.cpp's
  `--prompt` flag. It is intended for a compact vocabulary/context hint, not for
  transcript-cleaning instructions or a full domain prompt.
- The full selected prompt note remains the cleaner's input; the ASR prompt is a
  separate, deliberately shorter artifact such as `.local/prompt_notes/inno_asr_short.txt`.
- Prompt variants must be evaluated against a no-prompt baseline on the same audio
  before being enabled as a profile default. A prompt that causes echo, repetition,
  over-merging, or forced wrong terms must be rejected.

### 7.2 Deterministic pre-cleaning

Mechanical operations only: normalize encoding/line endings, remove known ASR formatting noise, preserve timestamps/order, and produce a manifest/statistics file. Do not infer speakers, rewrite meaning, or resolve ambiguous terms.

The current implementation is `src/postprocessing/preparer.py`; it is intended to run before any LLM call and reduce token volume/workload.

### 7.3 LLM semantic cleaning

Expose a provider-neutral adapter such as:

```text
clean_transcript(input_text, prompt_profile, metadata) -> cleaned_transcript, warnings, usage
```

The adapter must support:

- prompt profile resolution from the registry;
- chunking for long transcripts with overlap/continuity rules;
- structured output or a strict text contract;
- retries with bounded backoff and an idempotency key;
- uncertainty markers instead of fabricated corrections;
- preservation of numbers, names, technical terms, decisions, and sequence;
- a clear separation between transcript cleaning and meeting-record summarization.

The initial provider/model, API endpoint, token limits, cost budget, and exact response schema are **TODO**. OpenAI can be the first provider, but the interface must not make a future provider swap expensive.

### 7.4 Meeting-record generation

After transcript cleaning, the meeting-record skill creates a readable record with date/time, participants when known, agenda/context, discussion, decisions, action items/owners/dates, blockers, and uncertainties. It must not invent missing facts.

### 7.5 Notion persistence

Create one page per meeting in the Notion **工作日誌 database** for the current workflow, with a stable title such as:

- `Stand-up｜YYYY/MM/DD HH:mm`
- `會議記錄｜YYYY/MM/DD HH:mm`

The currently verified database/data source is:

- database page: `https://app.notion.com/p/695c80952164490999ac79631c6aca01?pvs=204`
- data source ID: `8e8b2a89-1897-4fb3-b3a9-6d7ffe1f14b6`

These identifiers are discovery hints, not a substitute for fetching the live schema. Use the live data-source schema rather than hard-coded assumptions. At minimum, set the title property `Name`, a date, an in-progress status, and useful topic/tag fields when known. Keep the page content as the meeting record and include source artifact paths and pipeline metadata.

Every meeting page must also include the cleaned transcript as a Notion file attachment (`<stem>_transcription_cleaned.txt`). The adapter should upload the local UTF-8 text artifact, append the returned file block to the same page, and re-fetch the page to verify the attachment. A local path alone is not sufficient; if attachment upload fails, keep the Notion stage pending and make the operation retryable without recreating the page.

Idempotency should be based on a stable meeting key (date/time/meeting ID) plus a source hash. A rerun must update or clearly report the existing page rather than create an accidental duplicate.

For a single-user local MVP, an internal Notion integration token is the likely simplest authentication path. OAuth is the later choice for multiple users/workspaces or user-facing distribution. The exact integration setup, scopes, token storage, and OAuth callback flow are **TODO**.

## 8. State and resumability

The current controller writes `<stem>_pipeline_state.json` with:

- `schema_version`;
- meeting metadata and timezone (`Asia/Taipei`);
- selected prompt profile and source ID;
- raw/SRT/prepared/cleaned artifact paths;
- the semantic-cleaning input path (normally the prepared transcript; raw only when prepared is unavailable);
- stage statuses (`discover`, `preclean`, `clean_transcript`, `notion_worklog`) plus page/attachment sub-status;
- next stage;
- pre-clean result/statistics;
- skill hand-off names;
- a separate `notion_write_requires_confirmation` guard.

State must include raw/prepared/cleaned source hashes, quality-gate results, prompt revision, Notion page ID, cleaned-transcript attachment hash/status, attempt history, and redacted error details. State transitions must be monotonic unless an explicit refresh is requested.

## 9. Reliability, safety, and privacy

- Fail closed on ambiguous meeting/profile selection and existing state unless refresh is explicit.
- Keep raw audio and raw transcript immutable.
- Make every stage retryable and independently inspectable.
- Redact transcript content and credentials from logs where possible.
- Store API tokens in macOS Keychain for the desktop app; never commit secrets or rely on a plaintext `.env` for distribution.
- Use least-privilege Notion access and an explicit final-write confirmation until the flow is trusted.
- Log provider/model, prompt revision, input/output hashes, and timestamps without logging secret values.

## 10. Failure and recovery matrix

| Failure | Expected behavior | Recovery |
|---|---|---|
| No timestamped workspace | Stop before mutation | Run organizer/transcription or choose another root |
| Multiple raw transcripts in one folder | Fail closed | Select/rename the intended source |
| Pre-clean output exists | Reuse and report `existing` | Explicit refresh if source changed |
| LLM timeout/rate limit | Keep prepared input and state | Retry with bounded backoff |
| LLM uncertain terminology | Preserve uncertainty marker | Human review / prompt profile update |
| Notion auth/API failure | Keep local record and pending state | Re-authenticate and retry Notion only |
| Duplicate Notion key | Do not create silently | Update existing page or request confirmation |
| Legacy recording layout | Report unsupported layout | Run compatibility migration |

## 11. Acceptance criteria for the next implementation increment

- A user can launch the CLI and answer meeting, prompt, mode, and final-write questions.
- The selected workspace is processed without overwriting raw input.
- Pre-cleaning is deterministic and produces a manifest.
- State allows the next stage to resume after an interruption.
- `inno`, `whisper`, and an explicit unregistered profile are distinguishable.
- A future adapter can call an LLM without changing workspace/state contracts.
- A future Notion adapter can create exactly one page per meeting and safely retry.
- The desktop UI can wrap the same service functions rather than reimplementing pipeline logic.

## 12. Proposed rollout

1. **CLI stabilization:** keep tests, migrate/bridge legacy recording output, add hashes and richer state.
2. **LLM adapter:** implement provider interface, prompt retrieval/cache, chunking, retries, and local preview.
3. **Notion adapter:** implement token auth for the local MVP, schema mapping, idempotent upsert, and write confirmation.
4. **Recording integration:** unify `record-meeting.sh` and `transcribe-english.sh` behind one workspace contract.
5. **Desktop shell:** one-click start/stop, progress, review, retry, and settings stored in Keychain/config.

## 13. Open questions / TODO

- Which exact whisper.cpp model, flags, and prompt/vocabulary format are used on this host?
- Which LLM provider/model and response schema meet quality, latency, and cost targets?
- Should prompt profiles be fetched live from Notion, cached locally, or versioned in the repository?
- Should meeting pages live in the 任務資料庫 permanently, or move to a dedicated meeting database later?
- What is the authoritative participant/team metadata source?
- How should long meetings be chunked and merged while retaining timestamps?
- What is the desired macOS desktop shell (menu-bar app, small window, or shortcut-driven tool)?
- What retention policy applies to audio, raw transcripts, cleaned transcripts, and logs?

---

This document is a research snapshot, not a claim that the LLM and Notion API integrations are already implemented. The current executable baseline is the local controller plus deterministic pre-cleaning and explicit skill hand-offs.
