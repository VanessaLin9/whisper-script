"""Deterministic, semantics-free transcript preparation for a later LLM pass."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True)
class PreparationStats:
    input_fragments: int
    output_fragments: int
    blank_lines_removed: int
    adjacent_duplicates_removed: int
    paragraph_count: int


def _normalized_fragment(raw: str) -> str:
    return re.sub(r"[ \t]+", " ", raw.strip())


def prepare_transcript(text: str, *, paragraph_chars: int = 240) -> tuple[str, PreparationStats]:
    """Prepare ASR fragments without making terminology or semantic decisions.

    The transformation is deliberately narrow: trim whitespace, remove blank lines,
    collapse only adjacent exact duplicates, and pack fragments into bounded
    paragraphs. Fragment text is otherwise preserved verbatim.
    """
    if paragraph_chars < 40:
        raise ValueError("paragraph_chars must be at least 40")

    raw_lines = text.splitlines()
    fragments: list[str] = []
    blank_lines_removed = 0
    adjacent_duplicates_removed = 0

    for raw in raw_lines:
        fragment = _normalized_fragment(raw)
        if not fragment:
            blank_lines_removed += 1
            continue
        if fragments and fragment == fragments[-1]:
            adjacent_duplicates_removed += 1
            continue
        fragments.append(fragment)

    paragraphs: list[str] = []
    current: list[str] = []
    current_size = 0
    for fragment in fragments:
        added_size = len(fragment) + (1 if current else 0)
        if current and current_size + added_size > paragraph_chars:
            paragraphs.append("，".join(current))
            current = []
            current_size = 0
        current.append(fragment)
        current_size += len(fragment) + (1 if len(current) > 1 else 0)
    if current:
        paragraphs.append("，".join(current))

    prepared = "\n\n".join(paragraphs)
    if prepared:
        prepared += "\n"

    stats = PreparationStats(
        input_fragments=sum(1 for line in raw_lines if line.strip()),
        output_fragments=len(fragments),
        blank_lines_removed=blank_lines_removed,
        adjacent_duplicates_removed=adjacent_duplicates_removed,
        paragraph_count=len(paragraphs),
    )
    return prepared, stats


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def prepare_file(
    source: Path,
    output: Path,
    *,
    paragraph_chars: int = 240,
    force: bool = False,
) -> tuple[Path, Path, PreparationStats]:
    source = source.resolve()
    output = output.resolve()
    manifest = output.with_suffix(output.suffix + ".manifest.json")

    if not source.is_file():
        raise FileNotFoundError(f"Source transcript not found: {source}")
    if source == output:
        raise ValueError("Output must not overwrite the source transcript")
    if not force and (output.exists() or manifest.exists()):
        raise FileExistsError(f"Output or manifest already exists: {output}")

    prepared, stats = prepare_transcript(
        source.read_text(encoding="utf-8"), paragraph_chars=paragraph_chars
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(prepared, encoding="utf-8")

    payload = {
        "source": str(source),
        "output": str(output),
        "source_sha256": _sha256(source),
        "output_sha256": _sha256(output),
        "paragraph_chars": paragraph_chars,
        "transformations": [
            "trim leading and trailing whitespace",
            "collapse internal spaces and tabs",
            "remove blank lines",
            "remove adjacent exact duplicate fragments",
            "pack fragments into bounded paragraphs with Chinese commas",
        ],
        "stats": asdict(stats),
    }
    manifest.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return output, manifest, stats


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Prepare a raw TXT transcript for a later LLM cleaning pass"
    )
    parser.add_argument("input", type=Path)
    parser.add_argument("-o", "--output", type=Path)
    parser.add_argument("--paragraph-chars", type=int, default=240)
    parser.add_argument("--force", action="store_true")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    output = args.output or args.input.with_name(f"{args.input.stem}_prepared.txt")
    prepared, manifest, stats = prepare_file(
        args.input,
        output,
        paragraph_chars=args.paragraph_chars,
        force=args.force,
    )
    print(f"Prepared transcript: {prepared}")
    print(f"Manifest: {manifest}")
    print(
        "Fragments: "
        f"{stats.input_fragments} -> {stats.output_fragments}; "
        f"adjacent duplicates removed: {stats.adjacent_duplicates_removed}"
    )


if __name__ == "__main__":
    main()
