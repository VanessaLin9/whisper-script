"""Plan and create meeting workspaces without running transcription."""

from __future__ import annotations

import json
import os
import shutil
from datetime import datetime
from pathlib import Path

from src.transcription.types import ArtifactKind

from .artifacts_policy import resolve_outputs
from .paths import (
    assert_safe_stem,
    metadata_path,
    normalized_audio_path,
    planned_artifact_paths,
    sanitize_stem,
    workspace_dirname,
)
from .types import (
    MeetingWorkspace,
    SourceDescriptor,
    SourceKind,
    WorkspaceError,
    WorkspacePlan,
    WorkspaceStage,
)


def _display_name(source: SourceDescriptor) -> str:
    if source.original_name:
        return source.original_name
    return source.path.name


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.expanduser().resolve().relative_to(root.expanduser().resolve())
    except ValueError:
        return False
    return True


def retained_in_workspace(workspace_dir: Path, audio_path: Path) -> bool:
    """True only when the core audio path actually lives under the workspace."""
    return _is_within(audio_path, workspace_dir)


def plan_workspace(
    output_root: Path,
    source: SourceDescriptor,
    meeting_time: datetime,
    *,
    outputs: frozenset[ArtifactKind] | None = None,
    normalize: bool = True,
) -> WorkspacePlan:
    """Pure planning: no mkdir, no copy, no metadata writes."""
    if output_root is None:
        raise WorkspaceError(WorkspaceStage.VALIDATE, "output_root is required")

    root = Path(output_root).expanduser()
    source_path = Path(source.path).expanduser()
    if not str(root):
        raise WorkspaceError(WorkspaceStage.VALIDATE, "output_root is required")

    resolved_outputs = resolve_outputs(outputs)
    safe_stem = sanitize_stem(_display_name(source))
    assert_safe_stem(safe_stem)
    time_label = meeting_time.strftime("%Y-%m-%d_%H%M")
    workspace_dir = root / workspace_dirname(time_label, safe_stem)
    meta = metadata_path(workspace_dir)
    artifacts = planned_artifact_paths(workspace_dir, safe_stem, resolved_outputs)
    norm = normalized_audio_path(workspace_dir, safe_stem) if normalize else None

    managed_audio: Path | None = None
    if source.kind == SourceKind.LOCAL_REFERENCE:
        audio_for_core = source_path
    elif source.kind == SourceKind.MANAGED_DOWNLOAD:
        managed_audio = workspace_dir / f"{safe_stem}{source_path.suffix}"
        audio_for_core = managed_audio
    elif source.kind == SourceKind.MANAGED_RECORDING:
        # Recording workflows own a single saved file; do not plan a second copy.
        audio_for_core = source_path
        managed_audio = None
    else:  # pragma: no cover - enum exhaustiveness
        raise WorkspaceError(
            WorkspaceStage.VALIDATE,
            f"Unsupported source kind: {source.kind!r}",
        )

    conflicts: list[Path] = [meta]
    if managed_audio is not None:
        conflicts.append(managed_audio)
    if norm is not None:
        conflicts.append(norm)
    conflicts.extend(artifacts.values())

    return WorkspacePlan(
        output_root=root,
        meeting_time=meeting_time,
        safe_stem=safe_stem,
        workspace_dir=workspace_dir,
        source=SourceDescriptor(
            kind=source.kind,
            path=source_path,
            original_name=source.original_name or source_path.name,
        ),
        audio_path_for_core=audio_for_core,
        transcript_stem=safe_stem,
        outputs=resolved_outputs,
        normalize=normalize,
        planned_artifacts=artifacts,
        normalized_audio_path=norm,
        metadata_path=meta,
        managed_audio_path=managed_audio,
        conflict_candidates=tuple(conflicts),
    )


def assert_source_readable(source: SourceDescriptor) -> Path:
    path = Path(source.path).expanduser().resolve()
    if not path.is_file():
        raise WorkspaceError(
            WorkspaceStage.VALIDATE,
            f"Source audio not found or not a file: {path}",
        )
    try:
        with path.open("rb") as handle:
            handle.read(1)
    except OSError as exc:
        raise WorkspaceError(
            WorkspaceStage.VALIDATE,
            f"Source audio is not readable: {path}",
            cause=exc,
        ) from exc
    return path


def assert_no_plan_conflicts(plan: WorkspacePlan) -> None:
    existing = [path for path in plan.conflict_candidates if path.exists()]
    if existing:
        joined = ", ".join(str(path) for path in existing)
        raise WorkspaceError(
            WorkspaceStage.CHECK_CONFLICTS,
            f"Workspace outputs already exist; refusing to overwrite: {joined}",
        )


def _temp_sibling(path: Path) -> Path:
    return path.with_name(f".{path.name}.tmp-{os.getpid()}")


def _cleanup_path(path: Path | None) -> None:
    if path is None:
        return
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def atomic_write_text(path: Path, text: str) -> None:
    """Write ``path`` via a same-directory temp file and ``os.replace``."""
    tmp = _temp_sibling(path)
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    except Exception:
        _cleanup_path(tmp)
        raise


def atomic_copy_file(source: Path, destination: Path) -> None:
    """Copy ``source`` to ``destination`` atomically (temp + replace)."""
    tmp = _temp_sibling(destination)
    try:
        shutil.copy2(source, tmp)
        os.replace(tmp, destination)
    except Exception:
        _cleanup_path(tmp)
        raise


def _write_metadata(plan: WorkspacePlan, audio_path: Path) -> None:
    payload = {
        "source_kind": plan.source.kind.value,
        "source_original_path": str(plan.source.path.expanduser().resolve()),
        "audio_path_for_core": str(audio_path),
        "meeting_time": plan.meeting_time.isoformat(timespec="minutes"),
        "safe_stem": plan.safe_stem,
        "outputs": sorted(kind.value for kind in plan.outputs),
        "normalize": plan.normalize,
        "retained_in_workspace": retained_in_workspace(plan.workspace_dir, audio_path),
    }
    try:
        atomic_write_text(
            plan.metadata_path,
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        )
    except OSError as exc:
        raise WorkspaceError(
            WorkspaceStage.WRITE_METADATA,
            f"Failed to write workspace metadata: {plan.metadata_path}",
            cause=exc,
        ) from exc


def _persist_managed_download(plan: WorkspacePlan, source_path: Path) -> Path:
    assert plan.managed_audio_path is not None
    destination = plan.managed_audio_path
    try:
        atomic_copy_file(source_path, destination)
    except OSError as exc:
        raise WorkspaceError(
            WorkspaceStage.PERSIST_SOURCE,
            f"Failed to persist managed source into workspace: {destination}",
            cause=exc,
        ) from exc
    return destination


def create_workspace(plan: WorkspacePlan) -> MeetingWorkspace:
    """Create the workspace directory, persist managed sources, write metadata.

    Local reference sources are never copied, moved, renamed, or deleted.
    """
    source_path = assert_source_readable(plan.source)
    # Re-bind plan paths against the resolved readable source for local refs.
    if plan.source.kind == SourceKind.LOCAL_REFERENCE:
        plan = WorkspacePlan(
            output_root=plan.output_root,
            meeting_time=plan.meeting_time,
            safe_stem=plan.safe_stem,
            workspace_dir=plan.workspace_dir,
            source=SourceDescriptor(
                kind=plan.source.kind,
                path=source_path,
                original_name=plan.source.original_name,
            ),
            audio_path_for_core=source_path,
            transcript_stem=plan.transcript_stem,
            outputs=plan.outputs,
            normalize=plan.normalize,
            planned_artifacts=plan.planned_artifacts,
            normalized_audio_path=plan.normalized_audio_path,
            metadata_path=plan.metadata_path,
            managed_audio_path=plan.managed_audio_path,
            conflict_candidates=plan.conflict_candidates,
        )

    assert_no_plan_conflicts(plan)

    try:
        plan.workspace_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise WorkspaceError(
            WorkspaceStage.CREATE,
            f"Failed to create workspace directory: {plan.workspace_dir}",
            cause=exc,
        ) from exc

    # Re-check after mkdir in case another writer raced in.
    assert_no_plan_conflicts(plan)

    audio_path = plan.audio_path_for_core
    created_paths: list[Path] = []
    try:
        if plan.source.kind == SourceKind.MANAGED_DOWNLOAD:
            audio_path = _persist_managed_download(plan, source_path)
            created_paths.append(audio_path)
        elif plan.source.kind == SourceKind.MANAGED_RECORDING:
            audio_path = source_path
        else:
            audio_path = source_path

        _write_metadata(plan, audio_path)
        created_paths.append(plan.metadata_path)
    except WorkspaceError:
        for path in reversed(created_paths):
            _cleanup_path(path)
        # Always clear destinations this invocation may have partially written,
        # even if they were never recorded in created_paths.
        _cleanup_path(plan.metadata_path)
        if plan.managed_audio_path is not None:
            _cleanup_path(plan.managed_audio_path)
        raise

    return MeetingWorkspace(
        plan=plan,
        workspace_dir=plan.workspace_dir,
        audio_path=audio_path,
        transcript_stem=plan.transcript_stem,
        outputs=plan.outputs,
        artifacts=dict(plan.planned_artifacts),
        normalized_audio_path=plan.normalized_audio_path,
        metadata_path=plan.metadata_path,
        source_kind=plan.source.kind,
        source_original_path=source_path,
    )


def prepare_local_workspace(
    audio_file: Path,
    output_root: Path,
    meeting_time: datetime,
    *,
    outputs: frozenset[ArtifactKind] | None = None,
    normalize: bool = True,
    original_name: str | None = None,
) -> MeetingWorkspace:
    """Convenience: plan + create for a local reference source."""
    source = SourceDescriptor(
        kind=SourceKind.LOCAL_REFERENCE,
        path=audio_file,
        original_name=original_name,
    )
    plan = plan_workspace(
        output_root,
        source,
        meeting_time,
        outputs=outputs,
        normalize=normalize,
    )
    return create_workspace(plan)
