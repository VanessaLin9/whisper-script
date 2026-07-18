#!/usr/bin/env python3
"""Offline tests for Meeting Workspace / Output Manager."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from src.output_manager import (
    DEFAULT_ARTIFACTS,
    SourceDescriptor,
    SourceKind,
    WorkspaceError,
    WorkspaceStage,
    assert_no_plan_conflicts,
    create_workspace,
    default_outputs_arg,
    plan_workspace,
    prepare_local_workspace,
    resolve_outputs,
)
from src.output_manager.paths import sanitize_stem
from src.transcription.types import ArtifactKind


class ArtifactPolicyTests(unittest.TestCase):
    def test_defaults_are_txt_srt_json_only(self) -> None:
        self.assertEqual(
            DEFAULT_ARTIFACTS,
            frozenset({ArtifactKind.TXT, ArtifactKind.SRT, ArtifactKind.JSON}),
        )
        self.assertNotIn(ArtifactKind.VTT, DEFAULT_ARTIFACTS)
        self.assertEqual(default_outputs_arg(), "json,srt,txt")

    def test_resolve_outputs_none_uses_defaults(self) -> None:
        self.assertEqual(resolve_outputs(None), DEFAULT_ARTIFACTS)

    def test_resolve_outputs_subset_and_vtt_opt_in(self) -> None:
        requested = frozenset({ArtifactKind.TXT, ArtifactKind.VTT})
        self.assertEqual(resolve_outputs(requested), requested)


class PathTests(unittest.TestCase):
    def test_sanitize_stem_handles_chinese_spaces_and_unsafe(self) -> None:
        self.assertEqual(sanitize_stem("市民大道八段544–590號.m4a"), "市民大道八段544–590號")
        self.assertEqual(sanitize_stem("Voice Memo.m4a"), "Voice_Memo")
        self.assertEqual(sanitize_stem("a/b:c*.wav"), "b-c")


class LocalReferenceTests(unittest.TestCase):
    def test_local_reference_does_not_copy_audio(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "inbox" / "市民大道 測試.m4a"
            source.parent.mkdir(parents=True)
            payload = b"raw-audio-bytes"
            source.write_bytes(payload)
            before = source.read_bytes()

            workspace = prepare_local_workspace(
                source,
                root / "records",
                datetime(2026, 7, 18, 22, 38),
            )

            self.assertEqual(workspace.source_kind, SourceKind.LOCAL_REFERENCE)
            self.assertEqual(workspace.audio_path.resolve(), source.resolve())
            self.assertEqual(source.read_bytes(), before)
            self.assertEqual(source.read_bytes(), payload)
            # No duplicate audio copy inside workspace.
            audio_copies = [
                path
                for path in workspace.workspace_dir.iterdir()
                if path.is_file() and path.suffix.lower() in {".m4a", ".wav", ".mp3"}
            ]
            self.assertEqual(audio_copies, [])
            self.assertTrue(workspace.metadata_path.is_file())
            meta = json.loads(workspace.metadata_path.read_text(encoding="utf-8"))
            self.assertEqual(meta["source_kind"], "local_reference")
            self.assertEqual(Path(meta["source_original_path"]), source.resolve())
            self.assertFalse(meta["retained_in_workspace"])
            self.assertEqual(
                workspace.workspace_dir.name,
                "2026-07-18_2238_市民大道_測試",
            )

    def test_failure_paths_do_not_modify_local_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "clip.wav"
            source.write_bytes(b"keep-me")
            plan = plan_workspace(
                root / "records",
                SourceDescriptor(SourceKind.LOCAL_REFERENCE, source),
                datetime(2026, 7, 17, 15, 0),
            )
            plan.metadata_path.parent.mkdir(parents=True)
            plan.metadata_path.write_text("{}", encoding="utf-8")

            with self.assertRaises(WorkspaceError) as ctx:
                create_workspace(plan)
            self.assertEqual(ctx.exception.stage, WorkspaceStage.CHECK_CONFLICTS)
            self.assertEqual(source.read_bytes(), b"keep-me")


class ManagedSourceTests(unittest.TestCase):
    def test_managed_download_persists_into_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            downloaded = root / "tmp-download.m4a"
            downloaded.write_bytes(b"drive-bytes")
            plan = plan_workspace(
                root / "records",
                SourceDescriptor(
                    SourceKind.MANAGED_DOWNLOAD,
                    downloaded,
                    original_name="meeting.m4a",
                ),
                datetime(2026, 7, 17, 15, 0),
            )
            workspace = create_workspace(plan)
            self.assertTrue(workspace.audio_path.is_file())
            self.assertEqual(workspace.audio_path.read_bytes(), b"drive-bytes")
            self.assertTrue(
                workspace.audio_path.resolve().is_relative_to(
                    workspace.workspace_dir.resolve()
                )
            )
            self.assertNotEqual(workspace.audio_path.resolve(), downloaded.resolve())
            meta = json.loads(workspace.metadata_path.read_text(encoding="utf-8"))
            self.assertTrue(meta["retained_in_workspace"])

    def test_partial_managed_copy_failure_leaves_no_destination(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            downloaded = root / "tmp-download.m4a"
            downloaded.write_bytes(b"drive-bytes")
            plan = plan_workspace(
                root / "records",
                SourceDescriptor(SourceKind.MANAGED_DOWNLOAD, downloaded),
                datetime(2026, 7, 17, 15, 0),
            )

            def partial_copy(src: Path, dst: Path) -> None:
                Path(dst).write_bytes(b"partial")
                raise OSError("disk full")

            with patch(
                "src.output_manager.workspace.shutil.copy2",
                side_effect=partial_copy,
            ):
                with self.assertRaises(WorkspaceError) as ctx:
                    create_workspace(plan)
            self.assertEqual(ctx.exception.stage, WorkspaceStage.PERSIST_SOURCE)
            self.assertEqual(downloaded.read_bytes(), b"drive-bytes")
            self.assertFalse(plan.metadata_path.exists())
            assert plan.managed_audio_path is not None
            self.assertFalse(plan.managed_audio_path.exists())
            leftovers = list(plan.workspace_dir.glob(".*.tmp-*"))
            self.assertEqual(leftovers, [])

    def test_partial_metadata_failure_removes_managed_audio(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            downloaded = root / "tmp-download.m4a"
            downloaded.write_bytes(b"drive-bytes")
            plan = plan_workspace(
                root / "records",
                SourceDescriptor(SourceKind.MANAGED_DOWNLOAD, downloaded),
                datetime(2026, 7, 17, 15, 0),
            )

            def partial_metadata(path: Path, text: str) -> None:
                path.write_text("{partial", encoding="utf-8")
                raise OSError("disk full")

            with patch(
                "src.output_manager.workspace.atomic_write_text",
                side_effect=partial_metadata,
            ):
                with self.assertRaises(WorkspaceError) as ctx:
                    create_workspace(plan)
            self.assertEqual(ctx.exception.stage, WorkspaceStage.WRITE_METADATA)
            self.assertEqual(downloaded.read_bytes(), b"drive-bytes")
            self.assertFalse(plan.metadata_path.exists())
            assert plan.managed_audio_path is not None
            self.assertFalse(plan.managed_audio_path.exists())

    def test_managed_recording_outside_workspace_not_marked_retained(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            recorded = root / "outside" / "meeting.wav"
            recorded.parent.mkdir(parents=True)
            recorded.write_bytes(b"rec")
            plan = plan_workspace(
                root / "records",
                SourceDescriptor(SourceKind.MANAGED_RECORDING, recorded),
                datetime(2026, 7, 17, 15, 0),
            )
            self.assertIsNone(plan.managed_audio_path)
            self.assertEqual(plan.audio_path_for_core, recorded)
            workspace = create_workspace(plan)
            meta = json.loads(workspace.metadata_path.read_text(encoding="utf-8"))
            self.assertFalse(meta["retained_in_workspace"])
            self.assertEqual(recorded.read_bytes(), b"rec")

    def test_managed_recording_inside_workspace_is_marked_retained(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan = plan_workspace(
                root / "records",
                SourceDescriptor(
                    SourceKind.MANAGED_RECORDING,
                    root / "placeholder.wav",
                    original_name="meeting.wav",
                ),
                datetime(2026, 7, 17, 15, 0),
            )
            recorded = plan.workspace_dir / "meeting.wav"
            recorded.parent.mkdir(parents=True)
            recorded.write_bytes(b"rec")
            plan = plan_workspace(
                root / "records",
                SourceDescriptor(SourceKind.MANAGED_RECORDING, recorded),
                datetime(2026, 7, 17, 15, 0),
            )
            workspace = create_workspace(plan)
            meta = json.loads(workspace.metadata_path.read_text(encoding="utf-8"))
            self.assertTrue(meta["retained_in_workspace"])
            self.assertEqual(workspace.audio_path.resolve(), recorded.resolve())


class ArtifactAndConflictTests(unittest.TestCase):
    def test_default_plan_has_no_vtt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "a.wav"
            source.write_bytes(b"x")
            plan = plan_workspace(
                root / "out",
                SourceDescriptor(SourceKind.LOCAL_REFERENCE, source),
                datetime(2026, 7, 17, 15, 0),
            )
            self.assertEqual(set(plan.planned_artifacts), set(DEFAULT_ARTIFACTS))
            self.assertNotIn(ArtifactKind.VTT, plan.planned_artifacts)

    def test_vtt_opt_in_appears_in_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "a.wav"
            source.write_bytes(b"x")
            plan = plan_workspace(
                root / "out",
                SourceDescriptor(SourceKind.LOCAL_REFERENCE, source),
                datetime(2026, 7, 17, 15, 0),
                outputs=frozenset({ArtifactKind.TXT, ArtifactKind.VTT}),
            )
            self.assertIn(ArtifactKind.VTT, plan.planned_artifacts)
            self.assertNotIn(ArtifactKind.SRT, plan.planned_artifacts)

    def test_conflict_rejected_before_create_side_effects(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "a.wav"
            source.write_bytes(b"x")
            plan = plan_workspace(
                root / "out",
                SourceDescriptor(SourceKind.LOCAL_REFERENCE, source),
                datetime(2026, 7, 17, 15, 0),
            )
            plan.planned_artifacts[ArtifactKind.TXT].parent.mkdir(parents=True)
            plan.planned_artifacts[ArtifactKind.TXT].write_text("old", encoding="utf-8")
            with self.assertRaises(WorkspaceError) as ctx:
                assert_no_plan_conflicts(plan)
            self.assertEqual(ctx.exception.stage, WorkspaceStage.CHECK_CONFLICTS)


class MissingSourceTests(unittest.TestCase):
    def test_missing_local_source_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            missing = root / "nope.wav"
            plan = plan_workspace(
                root / "out",
                SourceDescriptor(SourceKind.LOCAL_REFERENCE, missing),
                datetime(2026, 7, 17, 15, 0),
            )
            with self.assertRaises(WorkspaceError) as ctx:
                create_workspace(plan)
            self.assertEqual(ctx.exception.stage, WorkspaceStage.VALIDATE)


if __name__ == "__main__":
    unittest.main()
