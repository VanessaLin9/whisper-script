"""File-backed desktop workflow shared by the native UI and offline tests.

Desktop state is additive: legacy pipeline snapshots and raw artifacts are never
rewritten. Each mutation holds a per-workspace flock, including transcription.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from env_loader import load_env
from src.common.cancellation import OperationCancelled
from src.desktop.editing import edit_token, effective_paths, file_hash, parse_srt, revise_srt, store_revision
from src.desktop.jobs import (
    JobRejected,
    build_clean_job,
    build_notes_job,
    contract_digest,
    created_timestamp,
    ensure_job_shape,
    file_sha256,
    merged_segment_text,
    new_clean_job_id,
    new_notes_job_id,
    segment_mismatch,
    stale_reasons,
    validated_notes_outputs,
    validated_outbox_file,
)
from src.desktop.segments import SegmentationError, write_job_segments
from src.output_manager import SourceDescriptor, SourceKind, create_workspace, plan_workspace
from src.output_manager.workspace import exclusive_write_text
from src.postprocessing.preparer import prepare_file
from src.prompt_profiles import load_prompt_profiles
from src.transcription.core import transcribe
from src.transcription.types import ArtifactKind, TranscribeRequest

REPO = Path(__file__).resolve().parents[2]
TZ = ZoneInfo("Asia/Taipei")
AUDIO_EXTENSIONS = {".m4a", ".mp3", ".wav", ".aac", ".flac", ".aiff", ".aif", ".mp4", ".caf", ".ogg"}
STATE_NAME = "desktop_state.json"


def digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".desktop-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


@contextmanager
def meeting_lock(folder: Path):
    with (folder / ".desktop.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError("這場會議正在另一個視窗處理，請稍後再試。") from exc
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


class DesktopService:
    def __init__(self, repo: Path = REPO, settings: dict | None = None):
        self.repo = repo
        env = load_env(repo / ".env") if (repo / ".env").exists() else {}
        self.settings_path = repo / ".local" / "desktop_settings.json"
        whisper_root = Path(env.get("WHISPER_ROOT", str(Path.home() / "whisper.cpp")))
        self.settings = {
            "output_root": env.get("MEETING_RECORDS_DIR", str(Path.home() / "MeetingRecords")),
            "whisper_root": str(whisper_root), "model": "medium", "language": "zh",
            "threads": int(env.get("THREADS", os.cpu_count() or 4)),
        }
        self.settings.update(read_json(self.settings_path))
        if settings:
            self.settings.update(settings)

    @property
    def root(self) -> Path:
        return Path(self.settings["output_root"]).expanduser().resolve()

    def environment(self) -> dict:
        root = Path(self.settings["whisper_root"]).expanduser()
        checks = [
            {"name": "Whisper 執行檔", "ok": os.access(root / "build/bin/whisper-cli", os.X_OK)},
            {"name": f"{self.settings['model']} 多語言模型", "ok": (root / f"models/ggml-{self.settings['model']}.bin").is_file()},
            {"name": "FFmpeg", "ok": bool(shutil.which("ffmpeg"))},
        ]
        profiles = [{"key": key, "label": value["label"], "available": bool(value.get("local_path"))}
                    for key, value in load_prompt_profiles().items() if key != "new"]
        return {"settings": self.settings, "checks": checks, "profiles": profiles}

    def save_settings(self, values: dict) -> dict:
        if values.get("model") not in {"tiny", "base", "small", "medium", "large-v3"}:
            raise ValueError("請選擇多語言模型。")
        if not 1 <= int(values.get("threads", 0)) <= 256:
            raise ValueError("執行緒必須介於 1 到 256。")
        for key in ("output_root", "whisper_root"):
            if not values.get(key, "").strip():
                raise ValueError("資料夾路徑不可空白。")
            self.settings[key] = str(Path(values[key]).expanduser().resolve())
        self.settings.update(model=values["model"], threads=int(values["threads"]), language="zh")
        atomic_json(self.settings_path, self.settings)
        return self.environment()

    def folder(self, value: str) -> Path:
        path = Path(value).expanduser().resolve()
        if path.parent != self.root or not path.is_dir():
            raise ValueError("請選擇目前會議資料夾內的會議。")
        return path

    def paths(self, folder: Path) -> dict[str, Path]:
        meta = read_json(folder / "source_meta.json")
        raw = sorted(folder.glob("*_transcription.txt"))
        if len(raw) > 1:
            raise ValueError("有多份原始逐字稿，請先確認要處理的檔案。")
        stem = raw[0].name.removesuffix("_transcription.txt") if raw else meta.get("safe_stem")
        if not stem or Path(stem).name != stem or stem in {".", ".."}:
            raise ValueError("找不到會議來源或原始逐字稿。")
        return {"raw": folder / f"{stem}_transcription.txt",
                "srt": folder / f"{stem}_transcription.srt",
                "prepared": folder / f"{stem}_transcription_prepared.txt",
                "cleaned": folder / f"{stem}_transcription_cleaned.txt"}

    def row(self, folder: Path) -> dict:
        state = read_json(folder / STATE_NAME)
        paths = self.effective(folder, state)
        status = "待轉錄"
        if paths["raw"].exists():
            status = "待預清洗"
        if paths["prepared"].exists():
            status = "待 LLM 清洗"
        if paths["corrected"].exists():
            status = "已人工訂正 · 待 LLM 清洗"
        if paths["cleaned"].exists():
            status = "待檢查清洗稿"
        quality = state.get("quality", {})
        if (quality.get("status") == "passed" and paths["cleaned"].exists()
                and paths["input"].exists() and paths["raw"].exists()
                and self.quality_matches(quality, paths)):
            status = "清洗已確認"
        if quality.get("status") in {"stale", "failed"} and paths["cleaned"].exists():
            status = "內容已變更 · 待重新檢查"
        if state.get("status") in {"failed", "cancelled", "running"}:
            # A running snapshot is never proof a worker survived app/process exit.
            status = {"failed": "處理失敗 · 可重試", "cancelled": "已取消 · 可繼續", "running": "處理中／可續跑"}[state["status"]]
        try:
            display_date = datetime.strptime(folder.name[:15], "%Y-%m-%d_%H%M").strftime("%Y-%m-%d %H:%M")
            fallback_title = folder.name[16:]
        except ValueError:
            display_date, fallback_title = "時間未確認", folder.name
        return {"id": str(folder), "title": state.get("title", fallback_title),
                "date": display_date, "status": status,
                "error": state.get("error", ""), "profile": state.get("profile", ""),
                "raw": paths["raw"].exists(), "prepared": paths["prepared"].exists(),
                "corrected": paths["corrected"].exists(),
                "cleaned": paths["cleaned"].exists(), "reviewed": status == "清洗已確認"}

    def effective(self, folder: Path, state: dict | None = None) -> dict[str, Path]:
        return effective_paths(folder, self.paths(folder), state if state is not None else read_json(folder / STATE_NAME))

    @staticmethod
    def quality_matches(quality: dict, paths: dict[str, Path]) -> bool:
        return (quality.get("input_hash") == file_hash(paths["input"])
                and quality.get("raw_hash") == file_hash(paths["raw"])
                and quality.get("cleaned_hash") == file_hash(paths["cleaned"])
                and ("timeline_hash" not in quality or quality["timeline_hash"] == file_hash(paths["srt"])))

    def quality_result(self, paths: dict[str, Path], state: dict) -> dict:
        original = paths["input"].read_text(encoding="utf-8")
        text = paths["cleaned"].read_text(encoding="utf-8")
        reduction = 1 - len(text.strip()) / max(1, len(original.strip()))
        return {"status": "failed" if reduction > .20 else "pending_review",
                "input_hash": digest(paths["input"]), "semantic_input": str(paths["input"]),
                "raw_hash": digest(paths["raw"]), "cleaned_hash": digest(paths["cleaned"]),
                "timeline_hash": file_hash(paths["srt"]),
                "input_chars": len(original.strip()), "cleaned_chars": len(text.strip()),
                "semantic_input_bytes": paths["input"].stat().st_size, "cleaned_bytes": paths["cleaned"].stat().st_size,
                "reduction_ratio": reduction, "checked_at": datetime.now(TZ).isoformat(),
                "profile": state.get("profile", ""), "prompt_hash": state.get("prompt_hash", "")}

    def rename(self, value: str, title: str, expected_title: str) -> dict:
        folder = self.folder(value)
        if not isinstance(title, str) or not title.strip() or len(title.strip()) > 200 or any(ord(c) < 32 for c in title):
            raise ValueError("會議名稱需為 1–200 個字，不可含換行或控制字元。")
        with meeting_lock(folder):
            state = read_json(folder / STATE_NAME)
            current = self.row(folder)["title"]
            if current != expected_title:
                raise ValueError("會議名稱已在別處更新，請重新開啟編輯。")
            state["title"] = title.strip()
            atomic_json(folder / STATE_NAME, state)
        return {"meeting": self.row(folder)}

    def save_edit(self, value: str, kind: str, token: str, *, text=None, cues=None) -> dict:
        if kind not in {"raw", "prepared", "corrected", "cleaned", "srt"}:
            raise ValueError("不支援的訂正類型。")
        folder = self.folder(value)
        with meeting_lock(folder):
            state = read_json(folder / STATE_NAME)
            base_paths = self.paths(folder)
            paths = self.effective(folder, state)
            if token != edit_token(paths, state):
                raise ValueError("檔案已有新版本，尚未覆寫任何內容。請先保留你的修改，再重新載入編輯。")
            if not paths[kind].is_file():
                raise ValueError("此階段尚未有可訂正的稿件。")
            before = paths[kind].read_text(encoding="utf-8")
            if kind == "srt":
                if text is not None:
                    raise ValueError("時間軸只能提交逐段字幕文字，不能提交完整 SRT。")
                text = revise_srt(before, cues)
            elif not isinstance(text, str) or not text.strip() or "\x00" in text:
                raise ValueError("訂正稿不可空白，也不可含 NUL 字元。")
            if text == before:
                return {"meeting": self.row(folder), "kind": kind, "changed": False}
            target = "corrected" if kind in {"raw", "prepared", "corrected"} else kind
            store_revision(folder, base_paths, state, target, text, kind)
            state["error"] = ""
            if target == "cleaned":
                state["quality"] = self.quality_result(self.effective(folder, state), state)
                state["quality"]["origin"] = "manual_edit"
                state["status"] = "review"
                self._write_job_status(self._stage_record(state, "notes"), "stale")
            else:
                if state.get("handoff"):
                    state["handoff"]["status"] = "stale"
                self._write_job_status(self._clean_record(state), "stale")
                self._write_job_status(self._stage_record(state, "notes"), "stale")
                if state.get("quality"):
                    state["quality"]["status"] = "stale"
                state["status"] = "prepared"
            atomic_json(folder / STATE_NAME, state)
        return {"meeting": self.row(folder), "kind": target, "changed": True,
                "warning": "清洗稿縮短超過 20%，請檢查是否遺漏內容。" if state.get("quality", {}).get("status") == "failed" else ""}

    def meetings(self) -> dict:
        rows, warnings = [], []
        if self.root.is_dir():
            for folder in sorted(self.root.iterdir(), reverse=True):
                # .llm_jobs 沒有 source_meta 或 *_transcription.txt，不會被當成一場會議（PR #13）。
                if folder.is_dir() and ((folder / "source_meta.json").exists() or list(folder.glob("*_transcription.txt"))):
                    try:
                        rows.append(self.row(folder))
                    except (ValueError, OSError) as exc:
                        warnings.append(f"{folder.name}: {exc}")
        return {"meetings": rows, "warnings": warnings}

    def list_jobs(self, *, stage=None, status=None, folder=None, include_archive=False) -> dict:
        """以唯讀方式列出 inbox（及選擇性的 archive）工作。

        這裡刻意不呼叫 ``_job_dirs``、``prepare`` 或任何會取得 meeting lock
        的方法；工作目錄不存在時，直接回傳空清單。
        """
        if stage is not None and stage not in {"clean", "notes"}:
            raise ValueError("stage 必須是 clean 或 notes。")
        statuses = self._list_job_statuses(status)
        if folder is not None:
            if not isinstance(folder, str) or not folder.strip():
                raise ValueError("folder 必須是非空字串。")
            folder_path = self.folder(folder)
            folder_value = str(folder_path)
        else:
            folder_value = None
        if not isinstance(include_archive, bool):
            raise ValueError("include_archive 必須是布林值。")

        jobs_root = self.root / ".llm_jobs"
        inbox = jobs_root / "inbox"
        locations = [(inbox, "inbox")]
        if include_archive:
            locations.append((jobs_root / "archive", "archive"))
        jobs, invalid = [], []
        for directory, location in locations:
            # glob/iterdir 都是唯讀；不存在的目錄代表目前沒有工作。
            if not directory.is_dir():
                continue
            for path in sorted(directory.glob("*.json")):
                try:
                    job = json.loads(path.read_text(encoding="utf-8"))
                    self._validate_list_job(job)
                    if stage is not None and job["stage"] != stage:
                        continue
                    if statuses is not None and job["status"] not in statuses:
                        continue
                    if folder_value is not None and job["meeting_dir"] != folder_value:
                        continue
                    jobs.append(self._job_listing(job, path, location))
                except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
                    invalid.append({"job_path": str(path), "error": str(exc)})
        jobs.sort(key=lambda item: (item["_created_at"], item["job_id"]))
        for item in jobs:
            item.pop("_created_at", None)
        return {"jobs": jobs, "invalid": invalid, "root": str(self.root)}

    @staticmethod
    def _list_job_statuses(value):
        allowed = {"queued", "running", "done", "failed", "stale", "imported"}
        if value is None:
            return None
        if isinstance(value, str):
            values = [value]
        elif isinstance(value, list) and all(isinstance(item, str) for item in value):
            values = value
        else:
            raise ValueError("status 必須是字串或字串清單。")
        if not values or any(item not in allowed for item in values):
            raise ValueError("status 含有不支援的值；可用 queued、running、done、failed、stale、imported。")
        return set(values)

    @staticmethod
    def _validate_list_job(job):
        if not isinstance(job, dict):
            raise ValueError("工作 JSON 必須是物件。")
        required = ("job_id", "stage", "status", "created_at", "meeting_dir", "meeting_title",
                    "profile", "inputs", "expected_output")
        missing = [key for key in required if key not in job]
        if missing:
            raise ValueError("工作缺少欄位：" + "、".join(missing) + "。")
        if not isinstance(job["job_id"], str) or not job["job_id"]:
            raise ValueError("job_id 必須是非空字串。")
        if job["stage"] not in {"clean", "notes"}:
            raise ValueError("stage 必須是 clean 或 notes。")
        if not isinstance(job["status"], str):
            raise ValueError("status 必須是字串。")
        if job["status"] not in {"queued", "running", "done", "failed", "stale", "imported"}:
            raise ValueError("status 不是契約支援的值。")
        if not isinstance(job["created_at"], str):
            raise ValueError("created_at 必須是 ISO 時間字串。")
        try:
            datetime.fromisoformat(job["created_at"])
        except ValueError as exc:
            raise ValueError("created_at 不是有效的 ISO 時間字串。") from exc
        if not isinstance(job["meeting_dir"], str) or not isinstance(job["meeting_title"], str):
            raise ValueError("meeting_dir 與 meeting_title 必須是字串。")
        profile = job["profile"]
        if (not isinstance(profile, dict) or not isinstance(profile.get("key"), str)
                or not isinstance(profile.get("path"), str) or not isinstance(profile.get("sha256"), str)):
            raise ValueError("profile 必須含有 key、path 與 sha256。")
        inputs = job["inputs"]
        if not isinstance(inputs, dict):
            raise ValueError("inputs 必須是物件。")
        input_key = "transcript" if job["stage"] == "clean" else "cleaned"
        main_input = inputs.get(input_key)
        if (not isinstance(main_input, dict) or not isinstance(main_input.get("path"), str)
                or not isinstance(main_input.get("sha256"), str)):
            raise ValueError(f"inputs.{input_key} 必須含有 path 與 sha256。")
        expected = job["expected_output"]
        if not isinstance(expected, dict) or not isinstance(expected.get("result_path"), str):
            raise ValueError("expected_output 必須含有 result_path。")

    @staticmethod
    def _list_file_hash(path: Path) -> str:
        return file_sha256(path) if path.is_file() else ""

    def _job_listing(self, job: dict, path: Path, location: str) -> dict:
        input_key = "transcript" if job["stage"] == "clean" else "cleaned"
        main_input = job["inputs"][input_key]
        updated_at = datetime.fromtimestamp(path.stat().st_mtime, TZ).isoformat()
        stale, stale_detail = self._list_job_stale_info(job)
        result_path = Path(job["expected_output"]["result_path"])
        created_at = datetime.fromisoformat(job["created_at"])
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=TZ)
        return {
            "job_id": job["job_id"], "stage": job["stage"], "status": job["status"],
            "job_path": str(path), "location": location, "meeting_dir": job["meeting_dir"],
            "meeting_title": job["meeting_title"],
            "profile": {"key": job["profile"]["key"], "sha256": job["profile"]["sha256"]},
            "input": {"path": main_input["path"], "sha256": main_input["sha256"]},
            "input_path": main_input["path"], "input_sha256": main_input["sha256"],
            "expected_output": job["expected_output"], "result_ready": result_path.is_file(),
            "created_at": job["created_at"], "updated_at": updated_at,
            "stale": bool(stale), "stale_reasons": stale, "stale_detail": stale_detail,
            "_created_at": created_at,
        }

    def _list_job_stale(self, job: dict) -> list[str]:
        return self._list_job_stale_info(job)[0]

    def _list_job_stale_info(self, job: dict) -> tuple[list[str], str | None]:
        reasons = ["status"] if job["status"] == "stale" else []
        folder = Path(job["meeting_dir"]).expanduser().resolve()
        if not folder.is_dir():
            return list(dict.fromkeys(reasons + ["meeting_dir"])), None
        stale_detail = None
        try:
            state = read_json(folder / STATE_NAME)
            paths = self.effective(folder, state)
            profile_path = Path(job["profile"].get("path", ""))
            profile_sha = self._list_file_hash(profile_path)
            if job["stage"] == "clean":
                reasons.extend(stale_reasons(
                    job, transcript_sha256=self._list_file_hash(paths["input"]),
                    srt_sha256=self._list_file_hash(paths["srt"]),
                    profile_sha256=profile_sha, profile_path=str(profile_path),
                ))
                if not paths["input"].is_file() and "transcript" not in reasons:
                    reasons.append("transcript")
            else:
                current = self._list_file_hash(paths["cleaned"])
                recorded = job["inputs"]["cleaned"].get("sha256")
                if current != recorded:
                    reasons.append("cleaned")
            if profile_sha != job["profile"].get("sha256") or not profile_path.is_file():
                reasons.append("profile")
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
            reasons.append("meeting_files")
            stale_detail = str(exc) or exc.__class__.__name__
        return list(dict.fromkeys(reasons)), stale_detail

    def inspect_audio(self, value: str) -> dict:
        path = Path(value).expanduser().resolve()
        if not path.is_file() or path.suffix.lower() not in AUDIO_EXTENSIONS:
            raise ValueError("請選擇 m4a、wav、mp3 或其他支援的音訊檔。")
        from scripts.organize_recording import parse_standard_prefix
        detected = parse_standard_prefix(path.stem)
        date = detected.value.replace(tzinfo=TZ) if detected else None
        source = "檔名" if date else "檔案時間（請確認是否為錄音時間）"
        if date is None:
            try:
                result = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format_tags=creation_time",
                                         "-of", "json", str(path)], capture_output=True, text=True, timeout=8, check=True)
                value = json.loads(result.stdout).get("format", {}).get("tags", {}).get("creation_time")
                if value:
                    date = datetime.fromisoformat(value.replace("Z", "+00:00"))
                    date = date.replace(tzinfo=TZ) if date.tzinfo is None else date.astimezone(TZ)
                    source = "音訊 metadata"
            except (OSError, ValueError, subprocess.SubprocessError):
                pass
        if date is None:
            stat = path.stat()
            date = datetime.fromtimestamp(getattr(stat, "st_birthtime", stat.st_mtime), TZ)
        return {"path": str(path), "title": path.stem, "meeting_time": date.strftime("%Y-%m-%d %H:%M"),
                "time_source": source}

    def import_audio(self, path: str, title: str, meeting_time: str) -> dict:
        source = Path(self.inspect_audio(path)["path"])
        date = datetime.strptime(meeting_time, "%Y-%m-%d %H:%M").replace(tzinfo=TZ)
        if not title.strip():
            raise ValueError("請輸入會議名稱。")
        plan = plan_workspace(self.root, SourceDescriptor(SourceKind.MANAGED_IMPORT, source,
                              title.strip() + source.suffix), date)
        workspace = create_workspace(plan)
        atomic_json(workspace.workspace_dir / STATE_NAME, {
            "schema_version": 1, "title": title.strip(), "status": "imported", "attempts": [],
        })
        return {"meeting": self.row(workspace.workspace_dir)}

    def prepare(self, folder: Path) -> dict[str, Path]:
        paths = self.paths(folder)
        raw, prepared = paths["raw"], paths["prepared"]
        if not raw.is_file() or not raw.read_text(encoding="utf-8").strip():
            raise ValueError("原始逐字稿不存在或是空白，請先轉錄。")
        manifest_path = prepared.with_suffix(".txt.manifest.json")
        if prepared.exists():
            manifest = read_json(manifest_path)
            if manifest.get("source_sha256") != digest(raw) or manifest.get("output_sha256") != digest(prepared):
                raise ValueError("預清洗稿與來源／manifest 不一致。請先保留並檢查既有檔案，工具不會覆寫。")
        else:
            prepare_file(raw, prepared)
        return paths

    def process(self, value: str, cancellation=None, progress=lambda value: None) -> dict:
        folder = self.folder(value)
        with meeting_lock(folder):
            state = read_json(folder / STATE_NAME)
            state.update(status="running", error="")
            attempt = {"started_at": datetime.now(TZ).isoformat(), "model": self.settings["model"], "language": "zh"}
            state.setdefault("attempts", []).append(attempt)
            atomic_json(folder / STATE_NAME, state)
            try:
                paths = self.paths(folder)
                if not paths["raw"].exists():
                    missing = [item["name"] for item in self.environment()["checks"] if not item["ok"]]
                    if missing:
                        raise ValueError("缺少：" + "、".join(missing) + "。請到設定確認路徑與模型。")
                    meta = read_json(folder / "source_meta.json")
                    root = Path(self.settings["whisper_root"]).expanduser()
                    transcribe(TranscribeRequest(
                        audio_path=Path(meta["audio_path_for_core"]), language="zh", model=self.settings["model"],
                        model_path=root / f"models/ggml-{self.settings['model']}.bin", whisper_cli=root / "build/bin/whisper-cli",
                        threads=int(self.settings["threads"]), output_dir=folder, stem=meta["safe_stem"],
                        outputs=frozenset({ArtifactKind.TXT, ArtifactKind.SRT, ArtifactKind.JSON}),
                    ), cancellation=cancellation,
                        on_progress=lambda event: progress({"stage": event.stage.value, "status": event.status.value}))
                if cancellation:
                    cancellation.throw_if_cancelled("preclean")
                progress({"stage": "preclean", "status": "started"})
                paths = self.prepare(folder)
                state.update(status="prepared", raw_hash=digest(paths["raw"]), prepared_hash=digest(paths["prepared"]))
                attempt["outcome"] = "completed"
            except Exception as exc:
                state.update(status="cancelled" if isinstance(exc, OperationCancelled) else "failed", error=str(exc))
                attempt["outcome"] = state["status"]
                raise
            finally:
                attempt["finished_at"] = datetime.now(TZ).isoformat()
                atomic_json(folder / STATE_NAME, state)
        return {"meeting": self.row(folder)}

    def handoff(self, value: str, profile: str, summary: bool = False) -> dict:
        folder = self.folder(value)
        with meeting_lock(folder):
            self.prepare(folder)
            paths = self.effective(folder)
            profiles = load_prompt_profiles()
            if profile not in profiles or profile == "new":
                raise ValueError("請先選擇提示詞。")
            selected = profiles[profile]
            note_path = selected.get("local_path")
            if not note_path or not Path(note_path).is_file():
                raise ValueError("此提示詞尚未同步到本機，請先同步提示詞筆記。")
            state = read_json(folder / STATE_NAME)
            note = Path(note_path)
            state.update(profile=profile, prompt_hash=digest(note))
            if summary:
                if not self.row(folder)["reviewed"]:
                    raise ValueError("請先匯入清洗稿並完成內容檢查。")
                result = self._create_notes_job(folder, state, paths, profile, note)
                atomic_json(folder / STATE_NAME, state)
                return {**result, "meeting": self.row(folder)}
            inbox, outbox = self._job_dirs()
            job_id = new_clean_job_id()
            try:
                segments = write_job_segments(paths["srt"], inbox / job_id, outbox / job_id)
            except SegmentationError as exc:
                raise ValueError(str(exc)) from exc
            document = build_clean_job(
                job_id=job_id, meeting_dir=folder, meeting_title=self.row(folder)["title"],
                profile_key=profile, profile_path=note, paths=paths, outbox_dir=outbox,
                created_at=created_timestamp(datetime.now(TZ)), segments=segments,
            )
            # 先完成切段與 job 內容，再封存上一份。切段失敗時上一份仍留在 inbox（PR #13）。
            self._retire_previous_clean_job(state)
            job_path = inbox / f"{job_id}.json"
            atomic_json(job_path, document)
            body = paths["input"]
            state["handoff"] = {"input_hash": digest(body), "raw_hash": digest(paths["raw"]),
                                "timeline_hash": file_hash(paths["srt"]), "status": "ready", "input_path": str(body),
                                "profile": profile, "prompt_hash": state["prompt_hash"], "packet": str(job_path)}
            state.setdefault("handoffs", {})["clean"] = {
                "job_id": job_id, "status": "queued", "job_path": str(job_path),
                "contract_sha256": contract_digest(document),
            }
            atomic_json(folder / STATE_NAME, state)
            files = [str(job_path), str(body), segments["manifest_path"]]
            if paths["srt"].is_file():
                files.append(str(paths["srt"]))
            vocab = document["inputs"]["vocab"]
            if vocab:
                files.append(vocab["path"])
            return {"text": str(job_path), "path": str(job_path), "files": files,
                    "job_id": job_id, "meeting": self.row(folder)}

    def _create_notes_job(self, folder: Path, state: dict, paths: dict[str, Path],
                          profile_key: str | None = None, profile_path: Path | None = None) -> dict:
        """建立或重用目前有效清洗稿的 notes job；呼叫者負責持久化 state。"""
        cleaned = paths["cleaned"]
        if not cleaned.is_file():
            raise ValueError("找不到目前生效的清洗稿。")
        cleaned_hash = file_sha256(cleaned)
        profile_key = profile_key or state.get("profile")
        clean = self._clean_record(state)
        if clean and clean.get("job_path"):
            try:
                clean_job = read_json(Path(clean["job_path"]))
            except (OSError, json.JSONDecodeError):
                clean_job = {}
            profile = clean_job.get("profile") if isinstance(clean_job, dict) else None
            if isinstance(profile, dict):
                profile_key = profile_key or profile.get("key")
                if not profile_path and isinstance(profile.get("path"), str):
                    profile_path = Path(profile["path"])
        if not profile_key:
            raise ValueError("找不到建立會議記錄工作所需的提示詞 profile；請先建立清洗工作。")
        profiles = load_prompt_profiles()
        selected = profiles.get(profile_key)
        if not isinstance(selected, dict):
            raise ValueError("找不到建立會議記錄工作所需的提示詞 profile；請重新選擇提示詞。")
        note = profile_path or Path(selected.get("local_path", ""))
        if not note.is_file():
            raise ValueError("此提示詞尚未同步到本機，無法建立會議記錄工作。")
        profile_sha256 = file_sha256(note)

        candidates = self._find_notes_jobs(folder, cleaned_hash)
        for job, job_path in candidates:
            job_profile = job.get("profile") if isinstance(job.get("profile"), dict) else {}
            same_profile = (job_profile.get("key") == profile_key
                            and job_profile.get("sha256") == profile_sha256)
            if same_profile:
                state.setdefault("handoffs", {})["notes"] = {
                    "job_id": job["job_id"], "status": job.get("status", "queued"),
                    "job_path": str(job_path), "contract_sha256": contract_digest(job),
                }
                return {"text": str(job_path), "path": str(job_path), "files": [str(job_path), str(cleaned)],
                        "job_id": job["job_id"], "deduplicated": True}
            if job.get("status") == "running":
                raise ValueError("目前清洗稿已有不同提示詞的會議記錄工作正在執行，請等待完成後再切換提示詞。")
            if job.get("status") == "queued":
                self._archive_notes_candidate(state, job_path, "stale")

        specification = self.repo / "docs" / "meeting-summary-spec.md"
        if not specification.is_file():
            raise ValueError("找不到會議記錄規格。")
        state.update(profile=profile_key, prompt_hash=profile_sha256)
        self._retire_previous_notes_job(state)
        inbox, outbox = self._job_dirs()
        job_id = new_notes_job_id()
        document = build_notes_job(
            job_id=job_id, meeting_dir=folder, meeting_title=self.row(folder)["title"],
            profile_key=profile_key, profile_path=note, cleaned_path=cleaned,
            specification_path=specification, outbox_dir=outbox,
            created_at=created_timestamp(datetime.now(TZ)), vocab_path=folder / "meeting_vocab.md",
        )
        job_path = inbox / f"{job_id}.json"
        atomic_json(job_path, document)
        state.setdefault("handoffs", {})["notes"] = {
            "job_id": job_id, "status": "queued", "job_path": str(job_path),
            "contract_sha256": contract_digest(document),
        }
        return {"text": str(job_path), "path": str(job_path), "files": [
            str(job_path), str(cleaned), str(specification),
        ], "job_id": job_id, "deduplicated": False}

    def _find_notes_jobs(self, folder: Path, cleaned_hash: str) -> list[tuple[dict, Path]]:
        """找出同一會議、同一清洗稿 hash 且仍可處理的 notes jobs。"""
        root = self.root / ".llm_jobs"
        candidates = []
        record = self._stage_record(read_json(folder / STATE_NAME), "notes")
        if record and record.get("job_path"):
            candidates.append(Path(record["job_path"]))
        candidates.extend(sorted((root / "inbox").glob("notes-*.json")))
        candidates.extend(sorted((root / "archive").glob("notes-*.json")))
        seen = set()
        matches = []
        for path in candidates:
            if path in seen or not path.is_file():
                continue
            seen.add(path)
            try:
                job = read_json(path)
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(job, dict) or job.get("stage") != "notes":
                continue
            if job.get("meeting_dir") != str(folder.resolve()) or job.get("status") not in {"queued", "running", "done"}:
                continue
            recorded = (job.get("inputs", {}).get("cleaned") or {}).get("sha256")
            if recorded == cleaned_hash:
                matches.append((job, path))
        return matches

    def _archive_notes_candidate(self, state: dict, path: Path, status: str) -> None:
        """將 profile 不同的 queued notes job 以 stale 狀態封存。"""
        record = self._stage_record(state, "notes")
        if record and Path(record.get("job_path", "")).resolve() == path.resolve():
            self._archive_job(state, status, "notes")
            return
        if not path.is_file():
            return
        try:
            job = read_json(path)
        except (OSError, json.JSONDecodeError):
            job = None
        archive = self.root / ".llm_jobs" / "archive"
        archive.mkdir(parents=True, exist_ok=True)
        destination = archive / path.name
        if job is not None:
            job["status"] = status
            atomic_json(destination, job)
            path.unlink(missing_ok=True)
        else:
            shutil.move(path, destination)

    def _job_dirs(self) -> tuple[Path, Path]:
        root = self.root / ".llm_jobs"
        inbox, outbox, archive = root / "inbox", root / "outbox", root / "archive"
        for path in (inbox, outbox, archive):
            path.mkdir(parents=True, exist_ok=True)
        return inbox, outbox

    def _stage_record(self, state: dict, stage: str) -> dict | None:
        record = state.get("handoffs", {}).get(stage) if isinstance(state.get("handoffs"), dict) else None
        return record if isinstance(record, dict) else None

    def _clean_record(self, state: dict) -> dict | None:
        return self._stage_record(state, "clean")

    def _write_job_status(self, record: dict | None, status: str) -> None:
        if not record:
            return
        record["status"] = status
        path = Path(record.get("job_path", ""))
        if path.is_file():
            try:
                job = read_json(path)
            except json.JSONDecodeError:
                return
            if not isinstance(job, dict):
                return
            job["status"] = status
            atomic_json(path, job)

    def _place_exact_text(self, dest: Path, text: str) -> None:
        if dest.is_symlink():
            raise ValueError("會議記錄路徑不可為符號連結。")
        data = text.encode("utf-8")
        if dest.exists():
            if not dest.is_file() or file_sha256(dest) != hashlib.sha256(data).hexdigest():
                raise ValueError("會議記錄檔已存在且內容不同，已停止匯入。")
            return
        exclusive_write_text(dest, text)

    def _archive_clean_job(self, state: dict, status: str) -> None:
        self._archive_job(state, status, "clean")

    def _archive_job(self, state: dict, status: str, stage: str) -> None:
        record = self._stage_record(state, stage)
        if not record:
            return
        path = Path(record.get("job_path", ""))
        if path.is_file():
            try:
                loaded = read_json(path)
            except json.JSONDecodeError:
                loaded = None
            job = loaded if isinstance(loaded, dict) else None
            if path.parent.name == "archive":
                if job is not None:
                    job["status"] = status
                    atomic_json(path, job)
                record["status"] = status
                return
            archive = self.root / ".llm_jobs" / "archive"
            archive.mkdir(parents=True, exist_ok=True)
            source_dir = path.parent / path.stem
            if source_dir.is_dir():
                dest_dir = archive / source_dir.name
                if job is not None:
                    self._retarget_segments(job, source_dir, dest_dir)
                shutil.move(source_dir, dest_dir)
            dest = archive / path.name
            if job is not None:
                job["status"] = status
                atomic_json(dest, job)
                path.unlink(missing_ok=True)
            else:
                shutil.move(path, dest)
            record.update(status=status, job_path=str(dest))
            return
        record["status"] = status

    @staticmethod
    def _retarget_segments(job: dict, source: Path, dest: Path) -> None:
        segments = job.get("segments")
        if not isinstance(segments, dict):
            return

        def swap(value: str) -> str:
            if not isinstance(value, str):
                return value
            path = Path(value)
            try:
                relative = path.resolve().relative_to(source.resolve())
            except ValueError:
                return value
            return str(dest / relative)

        for key in ("directory", "manifest_path"):
            if isinstance(segments.get(key), str):
                segments[key] = swap(segments[key])
        for item in segments.get("items") or []:
            if isinstance(item.get("path"), str):
                item["path"] = swap(item["path"])

    def _retire_previous_clean_job(self, state: dict) -> None:
        record = self._clean_record(state)
        # 已匯入的 job 留在 archive，不被下一份標成 stale（PR #13）。notes 沿用同一規則。
        if not record or record.get("status") == "imported":
            return
        self._archive_clean_job(state, "stale")

    def _retire_previous_notes_job(self, state: dict) -> None:
        record = self._stage_record(state, "notes")
        if not record or record.get("status") == "imported":
            return
        self._archive_job(state, "stale", "notes")

    def _reject_if_handoff_stale(self, folder: Path, state: dict, paths: dict[str, Path]) -> None:
        handoff = state.get("handoff", {})
        profile_key = handoff.get("profile") or state.get("profile")
        note = load_prompt_profiles().get(profile_key, {}).get("local_path", "") if profile_key else ""
        prompt_changed = bool(note and handoff.get("prompt_hash") and digest(Path(note)) != handoff["prompt_hash"])
        stale = (handoff.get("status") == "stale" or handoff.get("input_hash") != digest(paths["input"])
                 or handoff.get("raw_hash") != digest(paths["raw"])
                 or ("timeline_hash" in handoff and handoff["timeline_hash"] != file_hash(paths["srt"]))
                 or prompt_changed)
        if not stale:
            return
        if state.get("handoff"):
            state["handoff"]["status"] = "stale"
        self._write_job_status(self._clean_record(state), "stale")
        atomic_json(folder / STATE_NAME, state)
        if prompt_changed:
            raise ValueError("提示詞已變更，請重新建立清洗工作。")
        raise ValueError("請先為目前逐字稿建立 LLM 交接，再匯入對應的結果。")

    def _store_cleaned(self, folder: Path, state: dict, paths: dict[str, Path], text: str, *, revalidate: bool) -> None:
        if not text.strip():
            raise ValueError("清洗稿不可空白。")
        original = paths["input"].read_text(encoding="utf-8")
        reduction = 1 - len(text.strip()) / max(1, len(original.strip()))
        if reduction > .20:
            raise ValueError(f"清洗稿縮短 {reduction:.1%}，超過 20%。請確認是否誤做摘要或遺漏；原檔仍保留在選取的位置。")
        if not revalidate:
            if paths["cleaned"].exists():
                store_revision(folder, self.paths(folder), state, "cleaned", text, "llm_import")
            else:
                exclusive_write_text(paths["cleaned"], text)
        state.update(status="review", quality=self.quality_result(self.effective(folder, state), state))

    def import_job(self, value: str) -> dict:
        folder = self.folder(value)
        with meeting_lock(folder):
            self.prepare(folder)
            paths = self.effective(folder)
            state = read_json(folder / STATE_NAME)
            record = self._clean_record(state)
            if not record or not record.get("job_id") or not record.get("job_path"):
                raise ValueError("請先建立清洗工作。")
            job_path = Path(record["job_path"])
            if not job_path.is_file():
                raise ValueError("找不到清洗工作檔。請重新建立。")
            try:
                job = ensure_job_shape(read_json(job_path))
            except (json.JSONDecodeError, JobRejected) as exc:
                raise ValueError("清洗工作格式無法辨識。") from exc
            meeting_dir = job.get("meeting_dir")
            if job.get("job_id") != record["job_id"] or not isinstance(meeting_dir, str) or Path(meeting_dir).resolve() != folder:
                raise ValueError("清洗工作與這場會議不符。")
            if job.get("status") in {"failed", "imported"}:
                raise ValueError("這個清洗工作已結束，請重新建立。")
            # 除了 status，輸入路徑與 hash 必須仍是 Desk 建立時的那一份（PR #13）。
            if record.get("contract_sha256") != contract_digest(job):
                raise ValueError("清洗工作內容已被改寫，請重新建立。")
            profile = job.get("profile") if isinstance(job.get("profile"), dict) else {}
            profile_path = profile.get("path") if isinstance(profile.get("path"), str) else ""
            note = Path(profile_path)
            reasons = stale_reasons(
                job, transcript_sha256=file_sha256(paths["input"]) if paths["input"].is_file() else "",
                srt_sha256=file_sha256(paths["srt"]) if paths["srt"].is_file() else "",
                profile_sha256=file_sha256(note) if note.is_file() else "",
                profile_path=str(note) if note.is_file() else "",
            )
            if segment_mismatch(job):
                reasons.append("segments")
            if reasons:
                if state.get("handoff"):
                    state["handoff"]["status"] = "stale"
                self._write_job_status(record, "stale")
                atomic_json(folder / STATE_NAME, state)
                if "profile" in reasons:
                    raise ValueError("提示詞已變更，請重新建立清洗工作。")
                raise ValueError("逐字稿或時間軸已變更，請重新建立清洗工作。")
            _, outbox = self._job_dirs()
            result_path = outbox / f"{job['job_id']}.json"
            if not result_path.is_file():
                raise ValueError("尚未在 outbox 找到這個工作的結果。")
            try:
                result = read_json(result_path)
            except json.JSONDecodeError as exc:
                raise ValueError("結果格式無法辨識。") from exc
            try:
                if isinstance(job.get("segments"), dict):
                    text = merged_segment_text(job, result, outbox)
                else:
                    text = validated_outbox_file(job, result, outbox).read_text(encoding="utf-8-sig")
            except JobRejected as exc:
                if exc.job_status:
                    self._write_job_status(record, exc.job_status)
                    atomic_json(folder / STATE_NAME, state)
                raise
            self._store_cleaned(folder, state, paths, text, revalidate=False)
            self._archive_clean_job(state, "imported")
            atomic_json(folder / STATE_NAME, state)
        return {"meeting": self.row(folder)}

    def import_notes(self, value: str) -> dict:
        folder = self.folder(value)
        with meeting_lock(folder):
            self.prepare(folder)
            paths = self.effective(folder)
            state = read_json(folder / STATE_NAME)
            record = self._stage_record(state, "notes")
            if not record or not record.get("job_id") or not record.get("job_path"):
                raise ValueError("請先建立會議記錄工作。")
            job_path = Path(record["job_path"])
            if not job_path.is_file():
                raise ValueError("找不到會議記錄工作檔。請重新建立。")
            try:
                job = ensure_job_shape(read_json(job_path))
            except (json.JSONDecodeError, JobRejected) as exc:
                raise ValueError("會議記錄工作格式無法辨識。") from exc
            meeting_dir = job.get("meeting_dir")
            if job.get("job_id") != record["job_id"] or not isinstance(meeting_dir, str) or Path(meeting_dir).resolve() != folder:
                raise ValueError("會議記錄工作與這場會議不符。")
            if job.get("status") in {"failed", "imported"} or record.get("status") in {"failed", "imported"}:
                raise ValueError("這個會議記錄工作已結束，請重新建立。")
            if record.get("contract_sha256") != contract_digest(job):
                raise ValueError("會議記錄工作內容已被改寫，請重新建立。")
            cleaned = paths["cleaned"]
            specification = self.repo / "docs" / "meeting-summary-spec.md"
            profile = job.get("profile") if isinstance(job.get("profile"), dict) else {}
            profile_path = profile.get("path") if isinstance(profile.get("path"), str) else ""
            note = Path(profile_path)
            quality = state.get("quality", {})
            confirmed = quality.get("status") == "passed" and self.quality_matches(quality, paths)
            reasons = []
            if not confirmed or not cleaned.is_file() or file_sha256(cleaned) != (job.get("inputs", {}).get("cleaned") or {}).get("sha256"):
                reasons.append("cleaned")
            if not specification.is_file() or file_sha256(specification) != (job.get("inputs", {}).get("specification") or {}).get("sha256"):
                reasons.append("specification")
            if not note.is_file() or file_sha256(note) != (job.get("profile") or {}).get("sha256"):
                reasons.append("profile")
            if reasons:
                self._write_job_status(record, "stale")
                atomic_json(folder / STATE_NAME, state)
                if "cleaned" in reasons:
                    raise ValueError("清洗稿已變更或尚未確認，請重新建立會議記錄工作。")
                if "specification" in reasons:
                    raise ValueError("會議記錄規格已變更，請重新建立會議記錄工作。")
                raise ValueError("提示詞已變更，請重新建立會議記錄工作。")
            _, outbox = self._job_dirs()
            result_path = outbox / f"{job['job_id']}.json"
            if not result_path.is_file():
                raise ValueError("尚未在 outbox 找到這個工作的結果。")
            try:
                result = read_json(result_path)
            except json.JSONDecodeError as exc:
                raise ValueError("結果格式無法辨識。") from exc
            cleaned_before = cleaned.read_bytes()
            # notes 匯入不得改寫清洗稿，也不改 clean 的 handoff 狀態（PR #13）。
            try:
                coverage, draft = validated_notes_outputs(job, result, outbox)
            except JobRejected as exc:
                if exc.job_status:
                    self._write_job_status(record, exc.job_status)
                    atomic_json(folder / STATE_NAME, state)
                raise
            if cleaned.read_bytes() != cleaned_before:
                raise ValueError("清洗稿被改動，已停止匯入。")
            notes_dir = folder / "meeting_notes"
            notes_dir.mkdir(exist_ok=True)
            coverage_dest = notes_dir / f"{job['job_id']}-coverage.json"
            draft_dest = notes_dir / f"{job['job_id']}-notes.md"
            # 寫到一半可以重試：既有檔只有 hash 相同才沿用，不覆寫不同內容（PR #13）。
            self._place_exact_text(coverage_dest, coverage.read_text(encoding="utf-8-sig"))
            self._place_exact_text(draft_dest, draft.read_text(encoding="utf-8-sig"))
            if cleaned.read_bytes() != cleaned_before:
                raise ValueError("清洗稿被改動，已停止匯入。")
            self._archive_job(state, "imported", "notes")
            imported = self._stage_record(state, "notes")
            if imported:
                imported.update(coverage_path=str(coverage_dest), draft_path=str(draft_dest))
            atomic_json(folder / STATE_NAME, state)
        return {"meeting": self.row(folder), "coverage_path": str(coverage_dest), "draft_path": str(draft_dest)}

    def import_cleaned(self, value: str, source: str) -> dict:
        folder = self.folder(value)
        with meeting_lock(folder):
            self.prepare(folder)
            paths = self.effective(folder)
            source_path = Path(source).expanduser().resolve()
            text = source_path.read_text(encoding="utf-8-sig")
            revalidate = source_path == paths["cleaned"].resolve()
            state = read_json(folder / STATE_NAME)
            self._reject_if_handoff_stale(folder, state, paths)
            self._store_cleaned(folder, state, paths, text, revalidate=revalidate)
            atomic_json(folder / STATE_NAME, state)
        return {"meeting": self.row(folder)}

    def review(self, value: str) -> dict:
        folder = self.folder(value)
        with meeting_lock(folder):
            self.prepare(folder)
            paths = self.effective(folder)
            state = read_json(folder / STATE_NAME)
            quality = state.get("quality", {})
            if (quality.get("status") != "pending_review" or not paths["cleaned"].is_file()
                    or not self.quality_matches(quality, paths)):
                raise ValueError("清洗稿沒有有效的匯入檢查，或檔案已變更。請重新確認來源。")
            quality.update(status="passed", reviewed_at=datetime.now(TZ).isoformat(), reviewer="user")
            state["status"] = "reviewed"
            notes_job_error = ""
            try:
                notes_result = self._create_notes_job(folder, state, paths)
                state["error"] = ""
            except Exception as exc:
                # 人工確認是獨立且不可回滾的動作；job 失敗必須明確回傳並留在 Desk。
                notes_result = {"job_id": "", "path": "", "deduplicated": False}
                notes_job_error = f"內容已確認，但自動建立會議記錄工作失敗：{exc}"
                state["error"] = notes_job_error
            atomic_json(folder / STATE_NAME, state)
        return {"meeting": self.row(folder), "notes_job_error": notes_job_error,
                "notes_job": notes_result}

    def preview(self, value: str, kind: str) -> dict:
        if kind not in {"raw", "prepared", "corrected", "cleaned", "srt"}:
            raise ValueError("不支援的預覽類型。")
        folder = self.folder(value)
        state = read_json(folder / STATE_NAME)
        paths = self.effective(folder, state)
        path = paths[kind]
        if not path.is_file():
            return {"text": "此階段尚未產生檔案。", "path": ""}
        text = path.read_text(encoding="utf-8")
        result = {"text": text, "path": str(path), "token": edit_token(paths, state), "editable": True}
        if kind == "srt":
            try:
                result["cues"] = parse_srt(text)
            except ValueError as exc:
                result.update(editable=False, edit_error=str(exc))
        return result


ACTION_REGISTRY = [
    {"name": "help", "summary": "不帶 name 時回傳 actions；帶 name 時回傳該 action 的說明欄位（name、summary、required、optional、returns、read_only、long_running、cancellable）。",
     "required": {}, "optional": {"name": {"type": "string", "default": None}},
     "returns": ["actions", "name", "summary", "required", "optional", "returns", "read_only", "long_running", "cancellable"],
     "read_only": True, "long_running": False, "cancellable": False,
     "handler": "help"},
    {"name": "environment", "summary": "檢查 Meeting Desk 的設定、模型、執行檔與提示詞。",
     "required": {}, "optional": {}, "returns": ["settings", "checks", "profiles"],
     "read_only": True, "long_running": False, "cancellable": False, "handler": "environment"},
    {"name": "settings", "summary": "儲存並回傳 Meeting Desk 設定。",
     "required": {"settings": "object"}, "optional": {}, "returns": ["settings", "checks", "profiles"],
     "read_only": False, "long_running": False, "cancellable": False, "handler": "settings"},
    {"name": "list", "summary": "列出會議資料夾與目前處理狀態。",
     "required": {}, "optional": {}, "returns": ["meetings", "warnings"],
     "read_only": True, "long_running": False, "cancellable": False, "handler": "list"},
    {"name": "list_jobs", "summary": "唯讀列出 LLM inbox 與可選的 archive 工作。",
     "required": {}, "optional": {"stage": {"type": "string", "default": None}, "status": {"type": "string_or_array", "default": None},
     "folder": {"type": "string", "default": None}, "include_archive": {"type": "boolean", "default": False}},
     "returns": ["jobs", "invalid", "root"], "read_only": True, "long_running": False, "cancellable": False,
     "handler": "list_jobs"},
    {"name": "inspect", "summary": "讀取音訊檔並推測會議時間。",
     "required": {"path": "string"}, "optional": {}, "returns": ["path", "title", "meeting_time", "time_source"],
     "read_only": True, "long_running": False, "cancellable": False, "handler": "inspect"},
    {"name": "import", "summary": "將音訊匯入新的會議資料夾。",
     "required": {"path": "string", "title": "string", "meeting_time": "string"}, "optional": {},
     "returns": ["meeting"], "read_only": False, "long_running": False, "cancellable": False, "handler": "import"},
    {"name": "process", "summary": "轉錄音訊並產生預清洗稿。",
     "required": {"folder": "string"}, "optional": {}, "returns": ["meeting"],
     "read_only": False, "long_running": True, "cancellable": True, "handler": "process"},
    {"name": "handoff", "summary": "建立清洗或會議記錄的 LLM 工作。",
     "required": {"folder": "string", "profile": "string"}, "optional": {"summary": {"type": "boolean", "default": False}},
     "returns": ["path", "text", "meeting", "job_id", "deduplicated"], "read_only": False,
     "long_running": False, "cancellable": False, "handler": "handoff"},
    {"name": "import_cleaned", "summary": "匯入 LLM 清洗稿並等待人工確認。",
     "required": {"folder": "string", "path": "string"}, "optional": {}, "returns": ["meeting"],
     "read_only": False, "long_running": False, "cancellable": False, "handler": "import_cleaned"},
    {"name": "import_job", "summary": "匯入清洗工作完成的 outbox 結果。",
     "required": {"folder": "string"}, "optional": {}, "returns": ["meeting"],
     "read_only": False, "long_running": False, "cancellable": False, "handler": "import_job"},
    {"name": "import_notes", "summary": "匯入會議記錄工作產生的 coverage 與草稿。",
     "required": {"folder": "string"}, "optional": {}, "returns": ["meeting", "coverage_path", "draft_path"],
     "read_only": False, "long_running": False, "cancellable": False, "handler": "import_notes"},
    {"name": "review", "summary": "人工確認清洗稿並自動建立會議記錄工作。",
     "required": {"folder": "string"}, "optional": {}, "returns": ["meeting", "notes_job_error", "notes_job"],
     "read_only": False, "long_running": False, "cancellable": False, "handler": "review"},
    {"name": "preview", "summary": "讀取會議某個階段的稿件預覽與編輯 token。",
     "required": {"folder": "string", "kind": "string"}, "optional": {},
     "returns": ["text", "path", "token", "editable", "cues"], "read_only": True,
     "long_running": False, "cancellable": False, "handler": "preview"},
    {"name": "rename", "summary": "更新會議顯示名稱。",
     "required": {"folder": "string", "title": "string", "expected_title": "string"}, "optional": {},
     "returns": ["meeting"], "read_only": False, "long_running": False, "cancellable": False, "handler": "rename"},
    {"name": "save_edit", "summary": "以 token 儲存人工訂正的稿件或字幕文字。",
     "required": {"folder": "string", "kind": "string", "token": "string"},
     "optional": {"text": {"type": "string", "default": None}, "cues": {"type": "array", "default": None}},
     "returns": ["meeting", "kind", "changed", "warning"], "read_only": False,
     "long_running": False, "cancellable": False, "handler": "save_edit"},
]


HANDLERS = {
    "environment": lambda service, request, cancellation, progress: service.environment(),
    "settings": lambda service, request, cancellation, progress: service.save_settings(request["settings"]),
    "list": lambda service, request, cancellation, progress: service.meetings(),
    "list_jobs": lambda service, request, cancellation, progress: service.list_jobs(
        stage=request.get("stage"), status=request.get("status"), folder=request.get("folder"),
        include_archive=request.get("include_archive", False)),
    "inspect": lambda service, request, cancellation, progress: service.inspect_audio(request["path"]),
    "import": lambda service, request, cancellation, progress: service.import_audio(
        request["path"], request["title"], request["meeting_time"]),
    "process": lambda service, request, cancellation, progress: service.process(
        request["folder"], cancellation, progress),
    "handoff": lambda service, request, cancellation, progress: service.handoff(
        request["folder"], request["profile"], request.get("summary", False)),
    "import_cleaned": lambda service, request, cancellation, progress: service.import_cleaned(
        request["folder"], request["path"]),
    "import_job": lambda service, request, cancellation, progress: service.import_job(request["folder"]),
    "import_notes": lambda service, request, cancellation, progress: service.import_notes(request["folder"]),
    "review": lambda service, request, cancellation, progress: service.review(request["folder"]),
    "preview": lambda service, request, cancellation, progress: service.preview(
        request["folder"], request["kind"]),
    "rename": lambda service, request, cancellation, progress: service.rename(
        request["folder"], request["title"], request["expected_title"]),
    "save_edit": lambda service, request, cancellation, progress: service.save_edit(
        request["folder"], request["kind"], request["token"],
        text=request.get("text"), cues=request.get("cues")),
}


def action_help(name=None) -> dict:
    public = [{key: value for key, value in action.items() if key != "handler"} for action in ACTION_REGISTRY]
    if name is not None:
        for action in public:
            if action["name"] == name:
                return action
        raise ValueError(f"找不到 action「{name}」。可用 action：{', '.join(item['name'] for item in public)}。")
    return {"actions": public}


def dispatch(service: DesktopService, request: dict, cancellation=None, progress=lambda value: None) -> dict:
    if not isinstance(request, dict) or "action" not in request:
        raise ValueError("缺少必要參數：action。")
    action = request["action"]
    by_name = {item["name"]: item for item in ACTION_REGISTRY}
    if action not in by_name:
        raise ValueError(f"未知操作「{action}」。可用 action：{', '.join(by_name)}。")
    spec = by_name[action]
    missing = [name for name in spec["required"] if name not in request]
    if missing:
        raise ValueError("缺少必要參數：" + "、".join(missing) + "。")
    if action == "help":
        return action_help(request.get("name"))
    try:
        return HANDLERS[spec["handler"]](service, request, cancellation, progress)
    except KeyError as exc:
        raise RuntimeError(f"未註冊的 action handler：{spec['handler']}。") from exc
