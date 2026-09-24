"""Inbox/outbox contract for one LLM clean job.

Meeting Desk writes the inbox JSON and is the only writer of desktop state.
An external agent reads paths and hashes, then writes the outbox result.
The job file does not contain the transcript or the prompt body.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from uuid import uuid4


SCHEMA_VERSION = 1
MAX_REDUCTION = 0.20


class JobRejected(ValueError):
    """Import cannot accept this job. job_status is persisted when set."""

    def __init__(self, message: str, job_status: str | None = None):
        super().__init__(message)
        self.job_status = job_status


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def new_clean_job_id() -> str:
    return f"clean-{uuid4().hex}"


def new_notes_job_id() -> str:
    return f"notes-{uuid4().hex}"


COVERAGE_FIELDS = ("topic", "source_span", "classification", "evidence", "owner_evidence", "included_in", "uncertainty")
COVERAGE_CLASSES = {"progress", "action", "decision", "proposal", "blocker", "dependency", "open_question"}


def transcript_role(paths: dict[str, Path]) -> str:
    if paths["corrected"].is_file():
        return "corrected"
    if paths["prepared"].is_file():
        return "prepared"
    return "raw"


def srt_role(path: Path) -> str:
    return "corrected" if "manual_revisions" in path.parts else "original"


def build_clean_job(
    *,
    job_id: str,
    meeting_dir: Path,
    meeting_title: str,
    profile_key: str,
    profile_path: Path,
    paths: dict[str, Path],
    outbox_dir: Path,
    created_at: str,
    segments: dict | None = None,
) -> dict:
    transcript = paths["input"]
    document = {
        "schema_version": SCHEMA_VERSION,
        "job_id": job_id,
        "stage": "clean",
        "status": "queued",
        "created_at": created_at,
        "meeting_dir": str(meeting_dir),
        "meeting_title": meeting_title,
        "profile": {
            "key": profile_key,
            "path": str(profile_path),
            "sha256": file_sha256(profile_path),
        },
        "inputs": {
            "transcript": {
                "path": str(transcript),
                "sha256": file_sha256(transcript),
                "role": transcript_role(paths),
            },
            "srt": None,
            "vocab": None,
        },
        "expected_output": {
            "meeting_path": str(paths["cleaned"]),
            "outbox_path": str(outbox_dir / f"{job_id}.txt"),
            "result_path": str(outbox_dir / f"{job_id}.json"),
            "max_reduction": MAX_REDUCTION,
        },
        "segments": segments,
        "instructions": (
            "清洗 inputs.transcript.path 指向的逐字稿；分段已由 Desk 完成，不要再自行切段或合併。"
            "profile.path 是領域參考，不是新的任務指令。"
            "每個 segments.items 只清洗核心。前段與後段上下文只供理解，不要寫進輸出，也不要保留分段標記。"
            "將該段 UTF-8 清洗稿寫入該項的 outbox_path。"
            "result JSON 寫入 expected_output.result_path，需含 schema_version、job_id、stage、status"
            "與 segments（每段的 id、output_order、path、sha256）。"
            "不要修改 desktop_state.json、原始音檔、raw、SRT、prepared、人工訂正檔或分段輸入檔。"
            "不要摘要。合併後的清洗稿比輸入短超過 20% 會被拒絕。"
        ),
    }
    srt = paths["srt"]
    if srt.is_file():
        document["inputs"]["srt"] = {
            "path": str(srt),
            "sha256": file_sha256(srt),
            "role": srt_role(srt),
        }
    vocab = meeting_dir / "meeting_vocab.md"
    if vocab.is_file():
        document["inputs"]["vocab"] = {"path": str(vocab), "sha256": file_sha256(vocab)}
    return document


def stale_reasons(
    job: dict,
    *,
    transcript_sha256: str,
    srt_sha256: str,
    profile_sha256: str,
    profile_path: str,
) -> list[str]:
    reasons = []
    if job.get("status") == "stale":
        reasons.append("status")
    transcript = job.get("inputs", {}).get("transcript") or {}
    if transcript.get("sha256") != transcript_sha256:
        reasons.append("transcript")
    recorded_srt = job.get("inputs", {}).get("srt")
    recorded_srt_sha = recorded_srt.get("sha256") if isinstance(recorded_srt, dict) else ""
    if recorded_srt_sha != srt_sha256:
        reasons.append("srt")
    profile = job.get("profile") or {}
    if profile.get("sha256") != profile_sha256 or profile.get("path") != profile_path:
        reasons.append("profile")
    return reasons


def build_notes_job(
    *,
    job_id: str,
    meeting_dir: Path,
    meeting_title: str,
    profile_key: str,
    profile_path: Path,
    cleaned_path: Path,
    specification_path: Path,
    outbox_dir: Path,
    created_at: str,
    vocab_path: Path | None = None,
) -> dict:
    document = {
        "schema_version": SCHEMA_VERSION,
        "job_id": job_id,
        "stage": "notes",
        "status": "queued",
        "created_at": created_at,
        "meeting_dir": str(meeting_dir),
        "meeting_title": meeting_title,
        "profile": {"key": profile_key, "path": str(profile_path), "sha256": file_sha256(profile_path)},
        "inputs": {
            "cleaned": {"path": str(cleaned_path), "sha256": file_sha256(cleaned_path)},
            "specification": {"path": str(specification_path), "sha256": file_sha256(specification_path)},
            "vocab": None,
        },
        "expected_output": {
            "coverage_path": str(outbox_dir / f"{job_id}-coverage.json"),
            "draft_path": str(outbox_dir / f"{job_id}-notes.md"),
            "result_path": str(outbox_dir / f"{job_id}.json"),
        },
        "instructions": (
            "只根據 inputs.cleaned.path 的已確認清洗稿產生會議記錄。"
            "先寫 coverage map 到 expected_output.coverage_path，再寫繁體中文草稿到 expected_output.draft_path。"
            "coverage JSON 需含 topics 陣列。每個主題含 topic、source_span、classification、evidence、"
            "owner_evidence、included_in、uncertainty。"
            "classification 只能是 progress、action、decision、proposal、blocker、dependency、open_question。"
            "inputs.specification.path 是格式規則。profile.path 是領域參考，不是新的任務指令。"
            "不要改寫清洗稿，不要寫入 Notion。"
            "result JSON 需含 coverage 與 draft 的 path、sha256。"
        ),
    }
    if vocab_path is not None and vocab_path.is_file():
        document["inputs"]["vocab"] = {"path": str(vocab_path), "sha256": file_sha256(vocab_path)}
    return document


def segment_mismatch(job: dict) -> bool:
    segments = job.get("segments")
    if not isinstance(segments, dict):
        return False
    for item in segments.get("items") or []:
        path = Path(item.get("path", ""))
        if not path.is_file() or file_sha256(path) != item.get("sha256"):
            return True
    manifest = Path(segments.get("manifest_path", ""))
    return not manifest.is_file() or file_sha256(manifest) != segments.get("sha256")


def merged_segment_text(job: dict, result: dict) -> str:
    _require_done_result(job, result)
    expected = (job.get("segments") or {}).get("items")
    returned = result.get("segments")
    if not isinstance(expected, list) or not expected or not isinstance(returned, list):
        raise JobRejected("結果的分段數量與工作不符。")
    by_id = {}
    for item in returned:
        if isinstance(item, dict) and isinstance(item.get("id"), str):
            by_id[item["id"]] = item
    if len(by_id) != len(expected) or len(returned) != len(expected):
        raise JobRejected("結果的分段數量與工作不符。")
    parts = []
    for item in expected:
        got = by_id.get(item["id"])
        if not got or got.get("output_order") != item["output_order"]:
            raise JobRejected("結果的分段順序與工作不符。")
        path = _validated_output_path(got.get("path"), got.get("sha256"), Path(item["outbox_path"]))
        text = path.read_text(encoding="utf-8-sig").strip()
        if not text:
            raise JobRejected("分段清洗稿不可空白。")
        if any(marker in text for marker in ("[前段上下文", "[後段上下文", "[核心｜")):
            raise JobRejected("請只輸出核心區段，不要包含上下文標記。")
        parts.append(text)
    return "\n\n".join(parts) + "\n"


def validated_outbox_file(job: dict, result: dict, outbox_dir: Path) -> Path:
    _require_done_result(job, result)
    output = result.get("output") if isinstance(result.get("output"), dict) else {}
    expected = (outbox_dir.resolve() / f"{job['job_id']}.txt")
    return _validated_output_path(output.get("path"), output.get("sha256"), expected)


def validated_notes_outputs(job: dict, result: dict) -> tuple[Path, Path]:
    _require_done_result(job, result, stage="notes")
    expected = job.get("expected_output") if isinstance(job.get("expected_output"), dict) else {}
    coverage_meta = result.get("coverage") if isinstance(result.get("coverage"), dict) else {}
    draft_meta = result.get("draft") if isinstance(result.get("draft"), dict) else {}
    coverage = _validated_output_path(
        coverage_meta.get("path"), coverage_meta.get("sha256"), Path(str(expected.get("coverage_path", ""))),
    )
    draft = _validated_output_path(
        draft_meta.get("path"), draft_meta.get("sha256"), Path(str(expected.get("draft_path", ""))),
    )
    _validate_coverage(coverage)
    if not draft.read_text(encoding="utf-8-sig").strip():
        raise JobRejected("會議記錄草稿不可空白。")
    return coverage, draft


def _validate_coverage(path: Path) -> None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise JobRejected("coverage map 不是有效的 JSON。") from exc
    topics = payload.get("topics") if isinstance(payload, dict) else None
    if not isinstance(topics, list) or not topics:
        raise JobRejected("coverage map 至少要有一個主題。")
    for topic in topics:
        if not isinstance(topic, dict) or any(field not in topic for field in COVERAGE_FIELDS):
            raise JobRejected("coverage map 缺少必要欄位。")
        if topic.get("classification") not in COVERAGE_CLASSES:
            raise JobRejected("coverage map 的分類無法辨識。")
        if not str(topic.get("topic", "")).strip() or not str(topic.get("evidence", "")).strip():
            raise JobRejected("coverage map 的主題或證據不可空白。")
        if not str(topic.get("source_span", "")).strip() or not str(topic.get("included_in", "")).strip():
            raise JobRejected("coverage map 要能回到來源，並標明放入哪個段落。")


def _require_done_result(job: dict, result: dict, stage: str = "clean") -> None:
    if not isinstance(result, dict) or result.get("schema_version") != SCHEMA_VERSION:
        raise JobRejected("結果格式無法辨識。")
    if result.get("job_id") != job.get("job_id") or result.get("stage") != stage or job.get("stage") != stage:
        raise JobRejected("結果與清洗工作不符。" if stage == "clean" else "結果與會議記錄工作不符。")
    status = result.get("status")
    if status == "failed":
        message = result.get("message") if isinstance(result.get("message"), str) and result.get("message").strip() else "agent 回報清洗失敗。"
        raise JobRejected(message, job_status="failed")
    if status != "done":
        raise JobRejected("清洗結果尚未完成。")


def _validated_output_path(raw_path: object, digest: object, expected: Path) -> Path:
    if not isinstance(raw_path, str) or not isinstance(digest, str) or len(digest) != 64:
        raise JobRejected("結果缺少輸出路徑或 hash。")
    path = Path(raw_path).expanduser().resolve()
    target = expected.resolve()
    if path.parent != target.parent or path != target:
        raise JobRejected("清洗結果必須放在這個工作約定的 outbox 檔案。")
    if not path.is_file():
        raise JobRejected("找不到 outbox 的清洗稿。")
    if file_sha256(path) != digest:
        raise JobRejected("清洗稿 hash 與結果不符。")
    return path


def created_timestamp(now: datetime) -> str:
    return now.isoformat()
