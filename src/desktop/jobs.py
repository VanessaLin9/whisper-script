"""Private, file-backed contracts for Meeting Desk LLM jobs.

Jobs contain paths and hashes only. Transcript, profile, and summary-spec bodies
remain in their original local files and are never copied into the request JSON.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from src.desktop.editing import file_hash, parse_srt
from src.output_manager.workspace import exclusive_write_text


JOB_SCHEMA_VERSION = 1
STAGE_SKILLS = {
    "clean": "clean-meeting-transcripts",
    "notes": "standup-worklog",
}
JOB_ID = re.compile(r"^(clean|notes)-\d{8}T\d{6}-[0-9a-f]{10}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
DEFAULT_CORE_SECONDS = 10 * 60
DEFAULT_CONTEXT_SECONDS = 45
REQUEST_REQUIRED_FIELDS = frozenset({
    "schema_version", "job_id", "stage", "skill", "status", "created_at",
    "private_local_only", "meeting", "profile", "input", "expected_result",
})
REQUEST_OPTIONAL_FIELDS = frozenset({"timeline", "specification", "segments_manifest"})
RESULT_FIELDS = frozenset({
    "schema_version", "job_id", "stage", "status", "output_path",
    "output_sha256", "completed_at",
})


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


def new_job_id(stage: str) -> str:
    if stage not in STAGE_SKILLS:
        raise ValueError("不支援的 LLM job 階段。")
    return f"{stage}-{datetime.now(timezone.utc):%Y%m%dT%H%M%S}-{uuid4().hex[:10]}"


def _milliseconds(value: str) -> int:
    hours, minutes, tail = value.split(":")
    seconds, milliseconds = tail.split(",")
    return ((int(hours) * 60 + int(minutes)) * 60 + int(seconds)) * 1000 + int(milliseconds)


def _cue_range(cue: dict) -> tuple[int, int]:
    start, end = cue["time"].split(" --> ")
    return _milliseconds(start), _milliseconds(end)


def _render_cues(cues: list[dict]) -> str:
    if not cues:
        return "（無）"
    return "\n\n".join(f"{cue['number']}\n{cue['time']}\n{cue['text']}" for cue in cues)


def _segment_plan(
    srt_path: Path,
    *,
    core_seconds: int = DEFAULT_CORE_SECONDS,
    context_seconds: int = DEFAULT_CONTEXT_SECONDS,
) -> tuple[Path, int, list[dict]]:
    if core_seconds <= 0 or context_seconds < 0:
        raise ValueError("切段時間設定不合法。")
    source = srt_path.expanduser().resolve()
    if not source.is_file():
        raise ValueError("找不到 SRT，無法為長會議建立固定切段。")
    cues = parse_srt(source.read_text(encoding="utf-8-sig"))
    timed = [(cue, *_cue_range(cue)) for cue in cues]
    duration = max(end for _, _, end in timed)
    core_ms = core_seconds * 1000
    context_ms = context_seconds * 1000
    entries = []
    for core_start in range(0, duration, core_ms):
        core_end = min(duration, core_start + core_ms)
        core = [cue for cue, start, _ in timed if core_start <= start < core_end]
        if not core:
            continue
        before_start = max(0, core_start - context_ms)
        after_end = min(duration, core_end + context_ms)
        before = [cue for cue, start, end in timed if start < core_start and end > before_start]
        after = [cue for cue, start, _ in timed if core_end <= start < after_end]
        number = len(entries) + 1
        entries.append({
            "id": f"seg-{number:02d}",
            "core_start_ms": core_start,
            "core_end_ms": core_end,
            "context_start_ms": before_start,
            "context_end_ms": after_end,
            "core_cue_ids": [cue["id"] for cue in core],
            "output_order": number,
            "_before": before,
            "_core": core,
            "_after": after,
        })
    if not entries:
        raise ValueError("SRT 沒有可切分的字幕內容。")
    return source, duration, entries


def plan_srt_segments(
    srt_path: Path,
    *,
    core_seconds: int = DEFAULT_CORE_SECONDS,
    context_seconds: int = DEFAULT_CONTEXT_SECONDS,
) -> dict:
    """Return a deterministic, read-only segmentation plan."""
    source, duration, entries = _segment_plan(
        srt_path, core_seconds=core_seconds, context_seconds=context_seconds)
    public_entries = [{key: value for key, value in entry.items() if not key.startswith("_")}
                      for entry in entries]
    return {
        "timeline_path": str(source),
        "timeline_sha256": file_hash(source),
        "duration_ms": duration,
        "core_seconds": core_seconds,
        "context_seconds": context_seconds,
        "segment_count": len(public_entries),
        "segments": public_entries,
    }


def segment_srt(
    output_root: Path,
    job_id: str,
    srt_path: Path,
    *,
    core_seconds: int = DEFAULT_CORE_SECONDS,
    context_seconds: int = DEFAULT_CONTEXT_SECONDS,
) -> Path:
    """Create deterministic core/context slices while assigning each cue to one core."""
    if not JOB_ID.fullmatch(job_id) or not job_id.startswith("clean-"):
        raise ValueError("切段必須使用有效的 clean job ID。")
    source, _, planned = _segment_plan(
        srt_path, core_seconds=core_seconds, context_seconds=context_seconds)
    directory = _private_directory(queue_root(output_root) / "work" / job_id / "segments")
    entries = []

    for entry in planned:
        path = directory / f"{entry['id']}.txt"
        text = (
            f"# Segment {entry['output_order']:02d}\n"
            f"core_ms: {entry['core_start_ms']}-{entry['core_end_ms']}\n"
            f"context_ms: {entry['context_start_ms']}-{entry['context_end_ms']}\n\n"
            "[CONTEXT_BEFORE]\n" + _render_cues(entry["_before"]) + "\n\n"
            "[CORE]\n" + _render_cues(entry["_core"]) + "\n\n"
            "[CONTEXT_AFTER]\n" + _render_cues(entry["_after"]) + "\n"
        )
        _private_text(path, text)
        entries.append({key: value for key, value in entry.items() if not key.startswith("_")} | {
            "path": str(path), "sha256": file_hash(path)})
    manifest = directory.parent / "segments.json"
    payload = {
        "schema_version": JOB_SCHEMA_VERSION,
        "job_id": job_id,
        "strategy": "srt-core-context",
        "core_seconds": core_seconds,
        "context_seconds": context_seconds,
        "merge_rule": "依 output_order 只合併各段 CORE；CONTEXT_BEFORE/AFTER 只供理解，不可重複輸出。",
        "timeline": _reference(source),
        "segments": entries,
    }
    _private_text(manifest, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    return manifest


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
    job_id: str | None = None,
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
    job_id = job_id or new_job_id(stage)
    if not JOB_ID.fullmatch(job_id) or not job_id.startswith(stage + "-"):
        raise ValueError("Job ID 與階段不一致。")
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
    if not isinstance(value, dict):
        raise ValueError("LLM job JSON 必須是物件。")
    fields = set(value)
    if not REQUEST_REQUIRED_FIELDS.issubset(fields) or fields - REQUEST_REQUIRED_FIELDS - REQUEST_OPTIONAL_FIELDS:
        raise ValueError("LLM job 欄位不符合 canonical contract。")
    stage = value.get("stage")
    if (value.get("schema_version") != JOB_SCHEMA_VERSION or stage not in STAGE_SKILLS
            or value.get("status") != "queued" or value.get("skill") != STAGE_SKILLS.get(stage)
            or not value.get("private_local_only") or not JOB_ID.fullmatch(str(value.get("job_id", "")))
            or not str(value["job_id"]).startswith(stage + "-")):
        raise ValueError("LLM job 版本或階段不受支援。")
    meeting = value.get("meeting")
    expected = value.get("expected_result")
    if (not isinstance(meeting, dict) or set(meeting) != {"path", "title"}
            or not all(isinstance(meeting.get(key), str) and meeting[key] for key in ("path", "title"))
            or not isinstance(expected, dict) or set(expected) != {"output_path", "result_path"}
            or not all(isinstance(expected.get(key), str) and expected[key]
                       for key in ("output_path", "result_path"))):
        raise ValueError("LLM job meeting 或 expected_result 格式錯誤。")
    for name in ("profile", "input", "timeline", "specification", "segments_manifest"):
        reference = value.get(name)
        if reference is None:
            continue
        required = {"path", "sha256", "bytes"}
        if name == "profile":
            required |= {"key", "label"}
        if (not isinstance(reference, dict) or set(reference) != required
                or not isinstance(reference.get("path"), str)
                or not SHA256.fullmatch(str(reference.get("sha256", "")))
                or not isinstance(reference.get("bytes"), int) or reference["bytes"] < 0):
            raise ValueError(f"LLM job {name} reference 格式錯誤。")
    return value


def _record_from_job(job: dict, request_path: Path) -> dict:
    expected = job["expected_result"]
    return {
        "job_id": job["job_id"],
        "stage": job["stage"],
        "request_path": str(request_path),
        "request_hash": file_hash(request_path),
        "expected_output": expected["output_path"],
        "result_path": expected["result_path"],
    }


def _queue_request_path(output_root: Path, request_path: Path) -> Path:
    inbox = (queue_root(output_root) / "inbox").resolve()
    resolved = request_path.expanduser().resolve()
    if resolved.parent != inbox or resolved.suffix != ".json":
        raise ValueError("只能檢查目前私有 inbox 內的 job JSON。")
    return resolved


def validate_job(output_root: Path, request_path: Path) -> dict:
    """Validate one queued job without writing files or returning private bodies."""
    resolved = _queue_request_path(output_root, request_path)
    report = {"request_path": str(resolved), "job_id": resolved.stem,
              "valid": False, "status": "invalid"}
    try:
        job = read_job(resolved)
        expected_dir = (queue_root(output_root) / "outbox" / job["job_id"]).resolve()
        expected_output = Path(job["expected_result"]["output_path"]).expanduser().resolve()
        result_path = Path(job["expected_result"]["result_path"]).expanduser().resolve()
        meeting_path = Path(job["meeting"]["path"]).expanduser().resolve()
        if (expected_output.parent != expected_dir or result_path != expected_dir / "result.json"
                or meeting_path.parent != output_root.expanduser().resolve()):
            raise ValueError("Job 的 meeting 或 expected_result 路徑超出固定位置。")
        report.update({
            "job_id": job["job_id"], "stage": job["stage"], "skill": job["skill"],
            "created_at": job["created_at"], "meeting_path": str(meeting_path),
            "meeting_title": job["meeting"]["title"], "profile": job["profile"]["key"],
            "input_path": job["input"]["path"], "expected_output": str(expected_output),
            "result_path": str(result_path),
        })
        try:
            verify_job_references(job)
        except ValueError as exc:
            report.update(status="stale", error=str(exc))
            return report
        if result_path.is_file():
            read_completed_result(_record_from_job(job, resolved))
            report["status"] = "done"
        else:
            report["status"] = "queued"
            report["partial_output"] = expected_output.is_file()
        report["valid"] = True
        return report
    except (OSError, ValueError) as exc:
        report["error"] = str(exc)
        return report


def list_jobs(output_root: Path) -> list[dict]:
    """List private queue metadata oldest-first without creating queue paths."""
    inbox = queue_root(output_root) / "inbox"
    if not inbox.is_dir():
        return []
    reports = [validate_job(output_root, path) for path in inbox.glob("*.json") if path.is_file()]
    return sorted(reports, key=lambda item: (item.get("created_at", ""), item["job_id"]))


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
    segment_reference = job.get("segments_manifest")
    if segment_reference:
        manifest = read_job_manifest(Path(segment_reference["path"]))
        timeline = manifest.get("timeline", {})
        timeline_path = Path(timeline.get("path", "")).expanduser().resolve()
        if not timeline_path.is_file() or file_hash(timeline_path) != timeline.get("sha256"):
            raise ValueError("切段使用的時間軸已變更，請重新建立工作。")
        for segment in manifest.get("segments", []):
            path = Path(segment.get("path", "")).expanduser().resolve()
            if not path.is_file() or file_hash(path) != segment.get("sha256"):
                raise ValueError("Job 的切段檔案已變更，請重新建立工作。")


def read_job_manifest(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("切段 manifest 不存在或格式錯誤。") from exc
    if (value.get("schema_version") != JOB_SCHEMA_VERSION
            or value.get("strategy") != "srt-core-context" or not isinstance(value.get("segments"), list)):
        raise ValueError("切段 manifest 版本或格式不受支援。")
    return value


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
    if (not isinstance(result, dict) or set(result) != RESULT_FIELDS
            or result.get("schema_version") != JOB_SCHEMA_VERSION or result.get("job_id") != job["job_id"]
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
