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
    created_timestamp,
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
                if folder.is_dir() and ((folder / "source_meta.json").exists() or list(folder.glob("*_transcription.txt"))):
                    try:
                        rows.append(self.row(folder))
                    except (ValueError, OSError) as exc:
                        warnings.append(f"{folder.name}: {exc}")
        return {"meetings": rows, "warnings": warnings}

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
                specification = self.repo / "docs" / "meeting-summary-spec.md"
                if not specification.is_file():
                    raise ValueError("找不到會議記錄規格。")
                self._retire_previous_notes_job(state)
                inbox, outbox = self._job_dirs()
                job_id = new_notes_job_id()
                document = build_notes_job(
                    job_id=job_id, meeting_dir=folder, meeting_title=self.row(folder)["title"],
                    profile_key=profile, profile_path=note, cleaned_path=paths["cleaned"],
                    specification_path=specification, outbox_dir=outbox,
                    created_at=created_timestamp(datetime.now(TZ)),
                    vocab_path=folder / "meeting_vocab.md",
                )
                job_path = inbox / f"{job_id}.json"
                atomic_json(job_path, document)
                state.setdefault("handoffs", {})["notes"] = {
                    "job_id": job_id, "status": "queued", "job_path": str(job_path),
                }
                atomic_json(folder / STATE_NAME, state)
                return {"text": str(job_path), "path": str(job_path), "files": [
                    str(job_path), str(paths["cleaned"]), str(specification),
                ], "job_id": job_id, "meeting": self.row(folder)}
            self._retire_previous_clean_job(state)
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
            job_path = inbox / f"{job_id}.json"
            atomic_json(job_path, document)
            body = paths["input"]
            state["handoff"] = {"input_hash": digest(body), "raw_hash": digest(paths["raw"]),
                                "timeline_hash": file_hash(paths["srt"]), "status": "ready", "input_path": str(body),
                                "profile": profile, "prompt_hash": state["prompt_hash"], "packet": str(job_path)}
            state.setdefault("handoffs", {})["clean"] = {
                "job_id": job_id, "status": "queued", "job_path": str(job_path),
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
            job = read_json(path)
            job["status"] = status
            atomic_json(path, job)

    def _archive_clean_job(self, state: dict, status: str) -> None:
        self._archive_job(state, status, "clean")

    def _archive_job(self, state: dict, status: str, stage: str) -> None:
        record = self._stage_record(state, stage)
        if not record:
            return
        path = Path(record.get("job_path", ""))
        if path.is_file():
            job = read_json(path)
            job["status"] = status
            if path.parent.name == "archive":
                atomic_json(path, job)
                record["status"] = status
                return
            archive = self.root / ".llm_jobs" / "archive"
            archive.mkdir(parents=True, exist_ok=True)
            source_dir = path.parent / path.stem
            if source_dir.is_dir():
                dest_dir = archive / source_dir.name
                self._retarget_segments(job, source_dir, dest_dir)
                shutil.move(source_dir, dest_dir)
            dest = archive / path.name
            atomic_json(dest, job)
            path.unlink(missing_ok=True)
            record.update(status=status, job_path=str(dest))
            return
        record["status"] = status

    @staticmethod
    def _retarget_segments(job: dict, source: Path, dest: Path) -> None:
        segments = job.get("segments")
        if not isinstance(segments, dict):
            return

        def swap(value: str) -> str:
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
                job = read_json(job_path)
            except json.JSONDecodeError as exc:
                raise ValueError("清洗工作格式無法辨識。") from exc
            if job.get("job_id") != record["job_id"] or Path(job.get("meeting_dir", "")).resolve() != folder:
                raise ValueError("清洗工作與這場會議不符。")
            if job.get("status") in {"failed", "imported"}:
                raise ValueError("這個清洗工作已結束，請重新建立。")
            note = Path(job.get("profile", {}).get("path", ""))
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
                    text = merged_segment_text(job, result)
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
                job = read_json(job_path)
            except json.JSONDecodeError as exc:
                raise ValueError("會議記錄工作格式無法辨識。") from exc
            if job.get("job_id") != record["job_id"] or Path(job.get("meeting_dir", "")).resolve() != folder:
                raise ValueError("會議記錄工作與這場會議不符。")
            if job.get("status") in {"failed", "imported"} or record.get("status") in {"failed", "imported"}:
                raise ValueError("這個會議記錄工作已結束，請重新建立。")
            cleaned = paths["cleaned"]
            specification = self.repo / "docs" / "meeting-summary-spec.md"
            note = Path(job.get("profile", {}).get("path", ""))
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
            try:
                coverage, draft = validated_notes_outputs(job, result)
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
            exclusive_write_text(coverage_dest, coverage.read_text(encoding="utf-8-sig"))
            exclusive_write_text(draft_dest, draft.read_text(encoding="utf-8-sig"))
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
            atomic_json(folder / STATE_NAME, state)
        return {"meeting": self.row(folder)}

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


def dispatch(service: DesktopService, request: dict, cancellation=None, progress=lambda value: None) -> dict:
    action = request["action"]
    if action == "environment": return service.environment()
    if action == "settings": return service.save_settings(request["settings"])
    if action == "list": return service.meetings()
    if action == "inspect": return service.inspect_audio(request["path"])
    if action == "import": return service.import_audio(request["path"], request["title"], request["meeting_time"])
    if action == "process": return service.process(request["folder"], cancellation, progress)
    if action == "handoff": return service.handoff(request["folder"], request["profile"], request.get("summary", False))
    if action == "import_cleaned": return service.import_cleaned(request["folder"], request["path"])
    if action == "import_job": return service.import_job(request["folder"])
    if action == "import_notes": return service.import_notes(request["folder"])
    if action == "review": return service.review(request["folder"])
    if action == "preview": return service.preview(request["folder"], request["kind"])
    if action == "rename": return service.rename(request["folder"], request["title"], request["expected_title"])
    if action == "save_edit": return service.save_edit(request["folder"], request["kind"], request["token"], text=request.get("text"), cues=request.get("cues"))
    raise ValueError("未知操作。")
