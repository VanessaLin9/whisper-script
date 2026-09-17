"""Private, file-backed contracts for Meeting Desk LLM jobs.

Jobs contain paths and hashes only. Transcript, profile, and summary-spec bodies
remain in their original local files and are never copied into the request JSON.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from src.desktop.editing import file_hash
from src.output_manager.workspace import exclusive_write_text


JOB_SCHEMA_VERSION = 1
STAGE_SKILLS = {
    "clean": "clean-meeting-transcripts",
    "notes": "standup-worklog",
}


def _private_directory(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)
    return path


def _private_text(path: Path, text: str) -> None:
    exclusive_write_text(path, text)
    path.chmod(0o600)


def _reference(path: Path) -> dict:
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        raise ValueError(f"找不到 job 需要的本機檔案：{resolved.name}")
    return {"path": str(resolved), "sha256": file_hash(resolved), "bytes": resolved.stat().st_size}


def queue_root(output_root: Path) -> Path:
    """Return the private queue location below the configured meeting root."""
    return output_root.expanduser().resolve() / ".llm_jobs"


def create_job(
    output_root: Path,
    meeting_dir: Path,
    title: str,
    stage: str,
    profile_key: str,
    profile_label: str,
    profile_path: Path,
    input_path: Path,
    expected_name: str,
    *,
    srt_path: Path | None = None,
    spec_path: Path | None = None,
    segments_manifest: Path | None = None,
) -> dict:
    """Create an immutable local request and return its state-safe metadata."""
    if stage not in STAGE_SKILLS:
        raise ValueError("不支援的 LLM job 階段。")
    if Path(expected_name).name != expected_name or expected_name in {"", ".", ".."}:
        raise ValueError("Job 輸出檔名不安全。")

    root = queue_root(output_root)
    inbox = _private_directory(root / "inbox")
    outbox = _private_directory(root / "outbox")
    _private_directory(root / "archive")
    job_id = f"{stage}-{datetime.now(timezone.utc):%Y%m%dT%H%M%S}-{uuid4().hex[:10]}"
    result_dir = _private_directory(outbox / job_id)
    output_path = result_dir / expected_name
    result_path = result_dir / "result.json"
    request_path = inbox / f"{job_id}.json"

    payload = {
        "schema_version": JOB_SCHEMA_VERSION,
        "job_id": job_id,
        "stage": stage,
        "skill": STAGE_SKILLS[stage],
        "status": "queued",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "private_local_only": True,
        "meeting": {"path": str(meeting_dir.resolve()), "title": title},
        "profile": {"key": profile_key, "label": profile_label, **_reference(profile_path)},
        "input": _reference(input_path),
        "expected_result": {
            "output_path": str(output_path),
            "result_path": str(result_path),
        },
    }
    if srt_path is not None and srt_path.is_file():
        payload["timeline"] = _reference(srt_path)
    if spec_path is not None:
        payload["specification"] = _reference(spec_path)
    if segments_manifest is not None:
        payload["segments_manifest"] = _reference(segments_manifest)

    _private_text(request_path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    return {
        "job_id": job_id,
        "stage": stage,
        "status": "queued",
        "request_path": str(request_path),
        "request_hash": file_hash(request_path),
        "result_path": str(result_path),
        "expected_output": str(output_path),
        "input_hash": payload["input"]["sha256"],
        "profile": profile_key,
        "profile_hash": payload["profile"]["sha256"],
        "created_at": payload["created_at"],
    }


def read_job(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("LLM job JSON 不存在或格式錯誤。") from exc
    if value.get("schema_version") != JOB_SCHEMA_VERSION or value.get("stage") not in STAGE_SKILLS:
        raise ValueError("LLM job 版本或階段不受支援。")
    return value


def verify_job_references(job: dict) -> None:
    """Fail closed if any referenced input changed after queueing."""
    references = [job.get("profile"), job.get("input"), job.get("timeline"),
                  job.get("specification"), job.get("segments_manifest")]
    for reference in references:
        if not reference:
            continue
        path = Path(reference.get("path", "")).expanduser().resolve()
        if not path.is_file() or file_hash(path) != reference.get("sha256"):
            raise ValueError("Job 的來源檔案已變更，請重新建立工作。")


def read_completed_result(record: dict) -> tuple[Path, dict, dict]:
    """Validate a queued job and its agent-produced result without trusting paths."""
    request_path = Path(record.get("request_path", "")).expanduser().resolve()
    if not request_path.is_file() or file_hash(request_path) != record.get("request_hash"):
        raise ValueError("Job request 已變更或遺失，請重新建立工作。")
    job = read_job(request_path)
    if job.get("job_id") != record.get("job_id") or job.get("stage") != record.get("stage"):
        raise ValueError("Job identity 不一致，拒絕匯入。")
    verify_job_references(job)

    expected = job["expected_result"]
    expected_output = Path(expected["output_path"]).expanduser().resolve()
    expected_result = Path(expected["result_path"]).expanduser().resolve()
    if expected_output != Path(record.get("expected_output", "")).expanduser().resolve():
        raise ValueError("Job 預期輸出位置不一致，拒絕匯入。")
    if expected_result != Path(record.get("result_path", "")).expanduser().resolve():
        raise ValueError("Job result 位置不一致，拒絕匯入。")
    try:
        result = json.loads(expected_result.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("尚未找到 agent 完成的 result JSON。") from exc
    if (result.get("schema_version") != JOB_SCHEMA_VERSION or result.get("job_id") != job["job_id"]
            or result.get("stage") != job["stage"] or result.get("status") != "done"):
        raise ValueError("Agent result 的 job、階段或狀態不一致。")
    returned_output = Path(result.get("output_path", "")).expanduser().resolve()
    if returned_output != expected_output or returned_output.parent != expected_result.parent:
        raise ValueError("Agent result 不可改寫 job 指定的輸出位置。")
    if not returned_output.is_file() or not returned_output.read_text(encoding="utf-8-sig").strip():
        raise ValueError("Agent 輸出不存在或是空白。")
    if file_hash(returned_output) != result.get("output_sha256"):
        raise ValueError("Agent 輸出 hash 與 result JSON 不一致。")
    return returned_output, result, job


def result_payload(job: dict, output_path: Path) -> dict:
    """Build the small result JSON contract used by local worker agents."""
    resolved = output_path.expanduser().resolve()
    return {
        "schema_version": JOB_SCHEMA_VERSION,
        "job_id": job["job_id"],
        "stage": job["stage"],
        "status": "done",
        "output_path": str(resolved),
        "output_sha256": file_hash(resolved),
        "completed_at": datetime.now(timezone.utc).isoformat(),
    }
