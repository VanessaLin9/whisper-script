"""Immutable manual revisions and text-only SRT edits.

Only revision pointers in desktop state change. Original files and previous
revisions remain intact; timestamps are never supplied by an editable UI field.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from src.output_manager.workspace import exclusive_write_text


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else ""


def effective_paths(folder: Path, paths: dict[str, Path], state: dict) -> dict[str, Path]:
    result = dict(paths)
    result["corrected"] = folder / "manual_revisions" / "not-created.txt"
    for kind, record in state.get("edits", {}).items():
        if kind not in {"corrected", "cleaned", "srt"}:
            continue
        path = (folder / record["path"]).resolve()
        if path.parent != (folder / "manual_revisions").resolve() or not path.is_file():
            raise ValueError("找不到人工訂正版本，請確認檔案仍在會議資料夾內。")
        if file_hash(path) != record["sha256"]:
            raise ValueError("人工訂正檔案已在外部變更，請先檢查版本，避免使用不一致的內容。")
        for base, expected in record["base_hashes"].items():
            if file_hash(paths[base]) != expected:
                raise ValueError("人工訂正的來源檔案已變更，請先檢查來源。")
        result[kind] = path
    result["input"] = result["corrected"] if result["corrected"].is_file() else (
        paths["prepared"] if paths["prepared"].is_file() else paths["raw"])
    return result


def edit_token(paths: dict[str, Path], state: dict) -> str:
    value = {"files": {key: file_hash(path) for key, path in paths.items()}, "edits": state.get("edits", {})}
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


TIMESTAMP = re.compile(r"^(\d{2,}:[0-5]\d:[0-5]\d,\d{3})[ \t]+-->[ \t]+(\d{2,}:[0-5]\d:[0-5]\d,\d{3})$")


def parse_srt(text: str) -> list[dict]:
    normalized = text.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not normalized:
        raise ValueError("時間軸檔案是空白。")
    cues = []
    for block in re.split(r"\n[ \t]*\n", normalized):
        lines = block.split("\n")
        if len(lines) < 3 or not lines[0].isdigit() or not TIMESTAMP.fullmatch(lines[1]):
            raise ValueError("時間軸格式無法安全編輯；請保留原檔並確認 SRT 格式。")
        cues.append({"id": len(cues), "number": lines[0], "time": lines[1], "text": "\n".join(lines[2:])})
    return cues


def revise_srt(original: str, submitted: list[dict]) -> str:
    cues = parse_srt(original)
    if not isinstance(submitted, list) or len(submitted) != len(cues):
        raise ValueError("不可新增或刪除字幕段落，時間軸必須保持不變。")
    blocks = []
    for cue, edit in zip(cues, submitted):
        if not isinstance(edit, dict) or edit.get("id") != cue["id"] or set(edit) != {"id", "text"}:
            raise ValueError("只可訂正字幕文字，不可修改序號、時間或順序。")
        text = edit["text"]
        if not isinstance(text, str):
            raise ValueError("字幕必須是文字。")
        text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
        if not text or re.search(r"\n[ \t]*\n", text) or "-->" in text or "\x00" in text:
            raise ValueError("每段字幕需有文字；不可插入空白段落或時間碼。")
        blocks.append(f"{cue['number']}\n{cue['time']}\n{text}")
    return "\n\n".join(blocks) + "\n"


def store_revision(folder: Path, paths: dict[str, Path], state: dict, kind: str,
                   text: str, source_kind: str) -> Path:
    directory = folder / "manual_revisions"
    directory.mkdir(exist_ok=True)
    extension = "srt" if kind == "srt" else "txt"
    path = directory / f"{kind}-{uuid4().hex}.{extension}"
    exclusive_write_text(path, text)
    bases = ("srt",) if kind == "srt" else ("raw", "prepared", "cleaned") if kind == "cleaned" else ("raw", "prepared")
    record = {"kind": kind, "path": str(path.relative_to(folder)), "sha256": file_hash(path),
              "base_hashes": {key: file_hash(paths[key]) for key in bases if paths[key].is_file()},
              "source_kind": source_kind, "saved_at": datetime.now(timezone.utc).isoformat()}
    state.setdefault("edits", {})[kind] = record
    state.setdefault("edit_history", []).append(record)
    return path
