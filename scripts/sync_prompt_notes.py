#!/usr/bin/env python3
"""Sync Notion prompt notes into the private local prompt registry.

The generated Markdown files contain domain vocabulary and are written below
``.local/prompt_notes`` (ignored by git).  Use a Notion integration token in
``NOTION_TOKEN`` or pass ``--token``; the token is never written to output.

Example:
    NOTION_TOKEN=... python3 scripts/sync_prompt_notes.py
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_API_VERSION = "2022-06-28"
REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REGISTRY = REPO_ROOT / "config" / "prompt_sources.json"
DEFAULT_OUTPUT_DIR = REPO_ROOT / ".local" / "prompt_notes"
UUID_RE = re.compile(
    r"(?P<uuid>(?:[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}|[0-9a-f]{8}(?:-[0-9a-f]{4}){2}-[0-9a-f]{16}))$",
    re.IGNORECASE,
)


class NotionSyncError(RuntimeError):
    """A user-actionable Notion sync failure."""


def _api_page_id(value: str) -> str:
    """Accept a Notion slug-like ID such as ``whisper-<uuid>``."""
    match = UUID_RE.search(value.strip())
    return match.group("uuid") if match else value.strip()


def _request_json(
    url: str,
    *,
    token: str,
    api_version: str,
) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Notion-Version": api_version,
            "Content-Type": "application/json",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise NotionSyncError(
            f"Notion API returned HTTP {exc.code}: {detail[:500]}"
        ) from exc
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise NotionSyncError(f"Unable to read Notion API: {exc}") from exc
    if not isinstance(payload, dict):
        raise NotionSyncError("Notion API returned an unexpected response")
    return payload


def _plain_text(rich_text: list[dict[str, Any]]) -> str:
    return "".join(
        str(item.get("plain_text", item.get("text", {}).get("content", "")))
        for item in rich_text
    )


def _page_title(page: dict[str, Any]) -> str:
    for property_value in page.get("properties", {}).values():
        if property_value.get("type") == "title":
            return _plain_text(property_value.get("title", []))
    return "Untitled Notion note"


def _block_text(block: dict[str, Any]) -> tuple[str, int]:
    block_type = str(block.get("type", ""))
    value = block.get(block_type, {})
    rich_text = value.get("rich_text", [])
    text = _plain_text(rich_text)
    if block_type == "heading_1":
        return f"# {text}", 0
    if block_type == "heading_2":
        return f"## {text}", 0
    if block_type == "heading_3":
        return f"### {text}", 0
    if block_type == "bulleted_list_item":
        return f"- {text}", 0
    if block_type == "numbered_list_item":
        return f"1. {text}", 0
    if block_type == "to_do":
        checked = "x" if value.get("checked") else " "
        return f"- [{checked}] {text}", 0
    if block_type == "quote":
        return f"> {text}", 0
    if block_type == "code":
        language = value.get("language", "plain text") or "plain text"
        return f"```{language}\n{text}\n```", 0
    if block_type == "divider":
        return "---", 0
    if block_type in {"paragraph", "callout"}:
        return text, 0
    return text, 0


def _fetch_blocks(
    page_id: str,
    *,
    token: str,
    api_version: str,
    indent: int = 0,
) -> list[str]:
    lines: list[str] = []
    cursor: str | None = None
    while True:
        query = {"page_size": "100"}
        if cursor:
            query["start_cursor"] = cursor
        url = "https://api.notion.com/v1/blocks/" + urllib.parse.quote(page_id, safe="")
        url += "?" + urllib.parse.urlencode(query)
        payload = _request_json(url, token=token, api_version=api_version)
        for block in payload.get("results", []):
            line, _ = _block_text(block)
            if line:
                prefix = "\t" * indent if indent and not line.startswith("```") else ""
                lines.extend(prefix + part for part in line.splitlines())
            if block.get("has_children"):
                lines.extend(
                    _fetch_blocks(
                        str(block["id"]),
                        token=token,
                        api_version=api_version,
                        indent=indent + 1,
                    )
                )
        if not payload.get("has_more"):
            break
        cursor = payload.get("next_cursor")
        if not cursor:
            break
    return lines


def _render_note(
    *,
    key: str,
    title: str,
    page_id: str,
    page_url: str,
    page: dict[str, Any],
    body: list[str],
) -> str:
    synced_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    content = "\n".join(body).strip()
    return (
        "---\n"
        f"key: {key}\n"
        f"title: {title}\n"
        f"source: {page_url}\n"
        f"page_id: {page_id}\n"
        f"synced_at: {synced_at}\n"
        "---\n\n"
        f"# {title}\n\n"
        f"> Source: [{_page_title(page)}]({page_url})\n\n"
        f"{content}\n"
    )


def _sync_one(
    note: dict[str, str],
    *,
    token: str,
    api_version: str,
    output_dir: Path,
    dry_run: bool,
) -> Path:
    key = note["key"]
    page_id = note["page_id"]
    api_page_id = _api_page_id(page_id)
    title = note.get("title") or key
    filename = note.get("filename") or f"{key}.md"
    page_url = f"https://app.notion.com/p/{page_id.replace('-', '')}"
    page = _request_json(
        "https://api.notion.com/v1/pages/" + urllib.parse.quote(api_page_id, safe=""),
        token=token,
        api_version=api_version,
    )
    body = _fetch_blocks(api_page_id, token=token, api_version=api_version)
    rendered = _render_note(
        key=key,
        title=title,
        page_id=page_id,
        page_url=page_url,
        page=page,
        body=body,
    )
    destination = (output_dir / filename).resolve()
    if destination.parent != output_dir.resolve():
        raise NotionSyncError(f"Registry filename escapes output directory: {filename}")
    if not dry_run:
        output_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=output_dir,
            prefix=f".{destination.stem}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            handle.write(rendered)
            temporary = Path(handle.name)
        temporary.replace(destination)
    return destination


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--key", action="append", help="Sync only this registry key (repeatable)")
    parser.add_argument("--page-id", help="Sync one page ID without using the registry")
    parser.add_argument("--title", help="Title used with --page-id")
    parser.add_argument("--filename", help="Output filename used with --page-id")
    parser.add_argument("--token", help="Notion integration token (prefer NOTION_TOKEN)")
    parser.add_argument("--api-version", default=DEFAULT_API_VERSION)
    parser.add_argument("--dry-run", action="store_true")
    return parser


def _load_registry(path: Path) -> list[dict[str, str]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise NotionSyncError(f"Invalid prompt registry: {path}: {exc}") from exc
    notes = payload.get("notes")
    if not isinstance(notes, list) or not all(isinstance(item, dict) for item in notes):
        raise NotionSyncError(f"Prompt registry must contain a notes list: {path}")
    return [{str(key): str(value) for key, value in item.items()} for item in notes]


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    token = (args.token or os.getenv("NOTION_TOKEN") or "").strip()
    if not token:
        print("[!] Set NOTION_TOKEN or pass --token; the token is never stored in notes.", file=sys.stderr)
        return 2
    try:
        if args.page_id:
            notes = [
                {
                    "key": args.key[0] if args.key else Path(args.filename or "prompt.md").stem,
                    "title": args.title or args.page_id,
                    "page_id": args.page_id,
                    "filename": args.filename or f"{Path(args.page_id).stem}.md",
                }
            ]
        else:
            notes = _load_registry(args.registry)
            if args.key:
                wanted = set(args.key)
                notes = [note for note in notes if note.get("key") in wanted]
                missing = wanted - {note.get("key") for note in notes}
                if missing:
                    raise NotionSyncError("Unknown prompt key(s): " + ", ".join(sorted(missing)))
        if not notes:
            raise NotionSyncError("No prompt notes selected")
        for note in notes:
            destination = _sync_one(
                note,
                token=token,
                api_version=args.api_version,
                output_dir=args.output_dir.expanduser().resolve(),
                dry_run=args.dry_run,
            )
            action = "would write" if args.dry_run else "wrote"
            print(f"[✓] {action} {destination}")
        return 0
    except (NotionSyncError, OSError, ValueError) as exc:
        print(f"[!] {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
