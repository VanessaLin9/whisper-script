"""Plan and create meeting workspaces without running transcription."""

from __future__ import annotations

import json
import os
import shutil
import uuid
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

_CREATE_LOCK_NAME = ".create.lock"


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


def _cleanup_path(path: Path | None) -> None:
    if path is None:
        return
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def _unique_temp_sibling(path: Path) -> Path:
    return path.with_name(f".{path.name}.tmp-{os.getpid()}-{uuid.uuid4().hex}")


def _write_all(fd: int, data: bytes) -> None:
    """Write every byte; treat short/zero progress as failure."""
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError("os.write made no progress")
        view = view[written:]


class _WorkspaceCreateLock:
    """Per-workspace exclusive create lock (``O_CREAT|O_EXCL``)."""

    def __init__(self, workspace_dir: Path) -> None:
        self.path = workspace_dir / _CREATE_LOCK_NAME
        self._fd: int | None = None

    def acquire(self) -> None:
        try:
            self._fd = os.open(
                self.path,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
            )
        except FileExistsError as exc:
            raise WorkspaceError(
                WorkspaceStage.CHECK_CONFLICTS,
                f"Workspace create already in progress or completed: {self.path}",
                cause=exc,
            ) from exc
        except OSError as exc:
            raise WorkspaceError(
                WorkspaceStage.CREATE,
                f"Failed to acquire workspace create lock: {self.path}",
                cause=exc,
            ) from exc
        try:
            _write_all(self._fd, f"{os.getpid()}\n".encode("utf-8"))
        except OSError as exc:
            self.release()
            raise WorkspaceError(
                WorkspaceStage.CREATE,
                f"Failed to write workspace create lock: {self.path}",
                cause=exc,
            ) from exc

    def release(self) -> None:
        if self._fd is None:
            return
        try:
            os.close(self._fd)
        finally:
            self._fd = None
            # Only the acquirer created this path via O_EXCL.
            _cleanup_path(self.path)


def exclusive_write_text(path: Path, text: str) -> None:
    """Create ``path`` only if missing; never overwrite an existing file."""
    data = text.encode("utf-8")
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as exc:
        raise WorkspaceError(
            WorkspaceStage.CHECK_CONFLICTS,
            f"Workspace outputs already exist; refusing to overwrite: {path}",
            cause=exc,
        ) from exc
    try:
        _write_all(fd, data)
    except Exception:
        _cleanup_path(path)
        raise
    finally:
        try:
            os.close(fd)
        except OSError:
            _cleanup_path(path)
            raise


def exclusive_copy_file(source: Path, destination: Path) -> None:
    """Copy ``source`` into a new ``destination`` without clobbering."""
    tmp: Path | None = _unique_temp_sibling(destination)
    try:
        assert tmp is not None
        shutil.copy2(source, tmp)
        try:
            os.link(tmp, destination)
        except FileExistsError as exc:
            raise WorkspaceError(
                WorkspaceStage.CHECK_CONFLICTS,
                f"Workspace outputs already exist; refusing to overwrite: {destination}",
                cause=exc,
            ) from exc
        except OSError:
            try:
                fd = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError as exc:
                raise WorkspaceError(
                    WorkspaceStage.CHECK_CONFLICTS,
                    f"Workspace outputs already exist; refusing to overwrite: {destination}",
                    cause=exc,
                ) from exc
            os.close(fd)
            os.replace(tmp, destination)
            tmp = None
    except WorkspaceError:
        raise
    except OSError:
        _cleanup_path(destination)
        raise
    finally:
        _cleanup_path(tmp)


# Back-compat aliases for tests/callers.
atomic_write_text = exclusive_write_text
atomic_copy_file = exclusive_copy_file


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
        exclusive_write_text(
            plan.metadata_path,
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        )
    except WorkspaceError:
        raise
    except OSError as exc:
        _cleanup_path(plan.metadata_path)
        raise WorkspaceError(
            WorkspaceStage.WRITE_METADATA,
            f"Failed to write workspace metadata: {plan.metadata_path}",
            cause=exc,
        ) from exc


def _persist_managed_download(plan: WorkspacePlan, source_path: Path) -> Path:
    assert plan.managed_audio_path is not None
    destination = plan.managed_audio_path
    try:
        exclusive_copy_file(source_path, destination)
    except WorkspaceError:
        raise
    except OSError as exc:
        _cleanup_path(destination)
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

    lock = _WorkspaceCreateLock(plan.workspace_dir)
    lock.acquire()
    created_paths: list[Path] = []
    audio_path = plan.audio_path_for_core
    try:
        # Re-check under the exclusive lock before any publish.
        assert_no_plan_conflicts(plan)

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
        # Only remove paths this invocation successfully published.
        for path in reversed(created_paths):
            _cleanup_path(path)
        raise
    finally:
        lock.release()

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
