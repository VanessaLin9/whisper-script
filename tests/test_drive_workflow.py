#!/usr/bin/env python3
"""Offline tests for Checkpoint 04.2 Drive → workspace → transcription workflow."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from src.drive import DownloadError, DownloadResult, DownloadStage
from src.transcription.types import (
    ArtifactKind,
    ProgressEvent,
    ProgressStatus,
    Stage,
    TranscribeRequest,
    TranscribeResult,
    TranscriptionError,
)
from src.workflow import (
    DriveTranscribeRequest,
    DriveTranscribeWorkflow,
    WorkflowError,
    WorkflowStage,
)
from src.workflow.types import WorkflowProgressEvent


class FakeDownloader:
    def __init__(
        self,
        result: DownloadResult | None = None,
        *,
        error: Exception | None = None,
    ) -> None:
        self._result = result
        self._error = error
        self.calls: list[str] = []

    def download(self, drive_url: str) -> DownloadResult:
        self.calls.append(drive_url)
        if self._error is not None:
            raise self._error
        assert self._result is not None
        return self._result


def _write_temp_audio(directory: Path, name: str = "meeting.m4a", body: bytes = b"AUDIO") -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_bytes(body)
    return path


def _request(output_root: Path, **overrides: object) -> DriveTranscribeRequest:
    base = dict(
        drive_url="https://drive.google.com/file/d/abc123XYZ_-99/view",
        output_root=output_root,
        language="en",
        model="tiny",
        model_path=Path("/tmp/model.bin"),
        whisper_cli=Path("/tmp/whisper-cli"),
        threads=2,
        meeting_time=datetime(2026, 7, 18, 12, 34, tzinfo=timezone.utc),
        outputs=frozenset({ArtifactKind.TXT, ArtifactKind.SRT, ArtifactKind.JSON}),
        normalize=True,
    )
    base.update(overrides)
    return DriveTranscribeRequest(**base)  # type: ignore[arg-type]


def _fake_success_transcribe(
    request: TranscribeRequest,
    *,
    on_progress=None,
) -> TranscribeResult:
    if on_progress is not None:
        on_progress(
            ProgressEvent(Stage.NORMALIZE, ProgressStatus.STARTED)
        )
        on_progress(
            ProgressEvent(Stage.NORMALIZE, ProgressStatus.FINISHED)
        )
        on_progress(
            ProgressEvent(Stage.TRANSCRIBE, ProgressStatus.STARTED)
        )
        on_progress(
            ProgressEvent(Stage.TRANSCRIBE, ProgressStatus.FINISHED)
        )
        on_progress(
            ProgressEvent(Stage.VALIDATE_ARTIFACTS, ProgressStatus.STARTED)
        )
        on_progress(
            ProgressEvent(Stage.VALIDATE_ARTIFACTS, ProgressStatus.FINISHED)
        )
    artifacts = {}
    for kind in request.outputs:
        path = request.output_dir / f"{request.stem}_transcription.{kind.value}"
        path.write_text(f"{kind.value}-ok\n", encoding="utf-8")
        artifacts[kind] = path
    norm = None
    if request.normalize:
        norm = request.output_dir / f"{request.stem}_norm16k.wav"
        norm.write_bytes(b"RIFF")
    now = datetime.now(timezone.utc)
    return TranscribeResult(
        raw_audio_path=request.audio_path,
        normalized_audio_path=norm,
        artifacts=artifacts,
        model=request.model,
        language=request.language,
        started_at=now,
        finished_at=now,
        output_dir=request.output_dir,
        stem=request.stem,
    )


class DriveWorkflowTests(unittest.TestCase):
    def test_success_stage_order_and_result_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "dl").mkdir()
            audio_tmp = _write_temp_audio(root / "dl")
            downloader = FakeDownloader(
                DownloadResult(
                    file_id="abc123XYZ_-99",
                    temp_path=audio_tmp,
                    filename="meeting.m4a",
                    content_type="audio/mp4",
                    size_bytes=audio_tmp.stat().st_size,
                )
            )
            events: list[tuple[WorkflowStage, ProgressStatus]] = []
            workflow = DriveTranscribeWorkflow(
                downloader=downloader,
                transcribe_fn=_fake_success_transcribe,
            )
            result = workflow.run(
                _request(root / "out"),
                on_progress=lambda e: events.append((e.stage, e.status)),
            )

            self.assertEqual(downloader.calls, [
                "https://drive.google.com/file/d/abc123XYZ_-99/view"
            ])
            self.assertTrue(result.workspace_dir.is_dir())
            self.assertTrue(result.raw_audio_path.is_file())
            self.assertEqual(result.raw_audio_path.read_bytes(), b"AUDIO")
            self.assertTrue(result.raw_audio_path.name.endswith(".m4a"))
            self.assertTrue(result.raw_transcript_path.is_file())
            self.assertEqual(
                set(result.artifacts),
                {ArtifactKind.TXT, ArtifactKind.SRT, ArtifactKind.JSON},
            )
            self.assertFalse(audio_tmp.exists(), "temp download should be cleaned")

            meta = json.loads(
                (result.workspace_dir / "source_meta.json").read_text(encoding="utf-8")
            )
            self.assertEqual(meta["source_kind"], "managed_download")
            self.assertTrue(meta["retained_in_workspace"])

            started = [s for s, st in events if st == ProgressStatus.STARTED]
            self.assertEqual(
                started[:2],
                [WorkflowStage.DOWNLOAD, WorkflowStage.WORKSPACE],
            )
            self.assertIn(WorkflowStage.NORMALIZE, started)
            self.assertIn(WorkflowStage.TRANSCRIBE, started)
            self.assertIn(WorkflowStage.OUTPUT, started)

    def test_download_failure_does_not_start_transcription(self) -> None:
        calls: list[str] = []

        def boom_transcribe(*_a, **_k):  # noqa: ANN001
            calls.append("transcribe")
            raise AssertionError("transcribe must not run")

        downloader = FakeDownloader(
            error=DownloadError(DownloadStage.DOWNLOAD, "permission denied", status_code=403)
        )
        workflow = DriveTranscribeWorkflow(
            downloader=downloader,
            transcribe_fn=boom_transcribe,
        )
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(WorkflowError) as ctx:
                workflow.run(_request(Path(tmp) / "out"))
            self.assertEqual(ctx.exception.stage, WorkflowStage.DOWNLOAD)
            self.assertEqual(calls, [])
            self.assertEqual(list((Path(tmp) / "out").glob("*")), [])

    def test_workspace_conflict_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            out = root / "out"
            # First success to create workspace
            audio1 = _write_temp_audio(root / "dl1", "meeting.m4a", b"ONE")
            wf1 = DriveTranscribeWorkflow(
                downloader=FakeDownloader(
                    DownloadResult(
                        file_id="abc123XYZ_-99",
                        temp_path=audio1,
                        filename="meeting.m4a",
                        content_type="audio/mp4",
                        size_bytes=3,
                    )
                ),
                transcribe_fn=_fake_success_transcribe,
            )
            first = wf1.run(_request(out))
            stale = first.raw_audio_path.read_bytes()

            audio2 = _write_temp_audio(root / "dl2", "meeting.m4a", b"TWO")
            transcribe_calls: list[str] = []

            def no_transcribe(*_a, **_k):  # noqa: ANN001
                transcribe_calls.append("x")
                raise AssertionError("must not transcribe on conflict")

            wf2 = DriveTranscribeWorkflow(
                downloader=FakeDownloader(
                    DownloadResult(
                        file_id="abc123XYZ_-99",
                        temp_path=audio2,
                        filename="meeting.m4a",
                        content_type="audio/mp4",
                        size_bytes=3,
                    )
                ),
                transcribe_fn=no_transcribe,
            )
            with self.assertRaises(WorkflowError) as ctx:
                wf2.run(_request(out))
            self.assertEqual(ctx.exception.stage, WorkflowStage.WORKSPACE)
            self.assertEqual(transcribe_calls, [])
            self.assertEqual(first.raw_audio_path.read_bytes(), stale)
            self.assertFalse(audio2.exists())

    def test_transcription_failure_keeps_raw_audio(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            audio_tmp = _write_temp_audio(root / "dl")

            events: list[WorkflowProgressEvent] = []

            def fail_transcribe(request: TranscribeRequest, *, on_progress=None):
                if on_progress is not None:
                    on_progress(
                        ProgressEvent(Stage.TRANSCRIBE, ProgressStatus.STARTED)
                    )
                    on_progress(
                        ProgressEvent(
                            Stage.TRANSCRIBE,
                            ProgressStatus.FAILED,
                            "whisper failed",
                        )
                    )
                raise TranscriptionError(Stage.TRANSCRIBE, "whisper failed", exit_code=1)

            workflow = DriveTranscribeWorkflow(
                downloader=FakeDownloader(
                    DownloadResult(
                        file_id="abc123XYZ_-99",
                        temp_path=audio_tmp,
                        filename="meeting.m4a",
                        content_type="audio/mp4",
                        size_bytes=5,
                    )
                ),
                transcribe_fn=fail_transcribe,
            )
            with self.assertRaises(WorkflowError) as ctx:
                workflow.run(
                    _request(root / "out"),
                    on_progress=events.append,
                )
            self.assertEqual(ctx.exception.stage, WorkflowStage.TRANSCRIBE)

            failed = [e for e in events if e.status == ProgressStatus.FAILED]
            self.assertEqual(len(failed), 1)
            self.assertEqual(failed[0].stage, WorkflowStage.TRANSCRIBE)
            self.assertEqual(
                [(e.stage, e.status) for e in events],
                [
                    (WorkflowStage.DOWNLOAD, ProgressStatus.STARTED),
                    (WorkflowStage.DOWNLOAD, ProgressStatus.FINISHED),
                    (WorkflowStage.WORKSPACE, ProgressStatus.STARTED),
                    (WorkflowStage.WORKSPACE, ProgressStatus.FINISHED),
                    (WorkflowStage.TRANSCRIBE, ProgressStatus.STARTED),
                    (WorkflowStage.TRANSCRIBE, ProgressStatus.FAILED),
                ],
            )

            workspaces = list((root / "out").iterdir())
            self.assertEqual(len(workspaces), 1)
            workspace = workspaces[0]
            managed = list(workspace.glob("*.m4a"))
            self.assertEqual(len(managed), 1)
            self.assertEqual(managed[0].read_bytes(), b"AUDIO")
            meta = workspace / "source_meta.json"
            self.assertTrue(meta.is_file())
            # No successful transcript artifacts
            self.assertEqual(list(workspace.glob("*_transcription.*")), [])

    def test_empty_outputs_is_typed_workspace_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            audio_tmp = _write_temp_audio(root / "dl")
            calls: list[str] = []

            def no_transcribe(*_a, **_k):  # noqa: ANN001
                calls.append("transcribe")
                raise AssertionError("must not transcribe")

            workflow = DriveTranscribeWorkflow(
                downloader=FakeDownloader(
                    DownloadResult(
                        file_id="abc123XYZ_-99",
                        temp_path=audio_tmp,
                        filename="meeting.m4a",
                        content_type="audio/mp4",
                        size_bytes=5,
                    )
                ),
                transcribe_fn=no_transcribe,
            )
            events: list[WorkflowProgressEvent] = []
            with self.assertRaises(WorkflowError) as ctx:
                workflow.run(
                    _request(root / "out", outputs=frozenset()),
                    on_progress=events.append,
                )
            self.assertEqual(ctx.exception.stage, WorkflowStage.WORKSPACE)
            self.assertIsInstance(ctx.exception.cause, ValueError)
            self.assertEqual(calls, [])
            self.assertFalse(audio_tmp.exists())
            self.assertEqual(
                [e.status for e in events if e.stage == WorkflowStage.WORKSPACE],
                [ProgressStatus.STARTED, ProgressStatus.FAILED],
            )

    def test_unexpected_workspace_exception_is_typed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            audio_tmp = _write_temp_audio(root / "dl")
            calls: list[str] = []

            def no_transcribe(*_a, **_k):  # noqa: ANN001
                calls.append("transcribe")
                raise AssertionError("must not transcribe")

            workflow = DriveTranscribeWorkflow(
                downloader=FakeDownloader(
                    DownloadResult(
                        file_id="abc123XYZ_-99",
                        temp_path=audio_tmp,
                        filename="meeting.m4a",
                        content_type="audio/mp4",
                        size_bytes=5,
                    )
                ),
                transcribe_fn=no_transcribe,
            )
            with mock.patch(
                "src.workflow.drive_transcribe.create_workspace",
                side_effect=RuntimeError("disk exploded"),
            ):
                with self.assertRaises(WorkflowError) as ctx:
                    workflow.run(_request(root / "out"))
            self.assertEqual(ctx.exception.stage, WorkflowStage.WORKSPACE)
            self.assertIn("disk exploded", ctx.exception.message)
            self.assertEqual(calls, [])
            self.assertFalse(audio_tmp.exists())

    def test_srt_only_request_still_returns_txt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            audio_tmp = _write_temp_audio(root / "dl")
            workflow = DriveTranscribeWorkflow(
                downloader=FakeDownloader(
                    DownloadResult(
                        file_id="abc123XYZ_-99",
                        temp_path=audio_tmp,
                        filename="meeting.m4a",
                        content_type="audio/mp4",
                        size_bytes=5,
                    )
                ),
                transcribe_fn=_fake_success_transcribe,
            )
            result = workflow.run(
                _request(root / "out", outputs=frozenset({ArtifactKind.SRT}))
            )
            self.assertTrue(result.raw_transcript_path.is_file())
            self.assertIn(ArtifactKind.TXT, result.artifacts)
            self.assertIn(ArtifactKind.SRT, result.artifacts)

    def test_unexpected_transcribe_exception_keeps_raw_audio(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            audio_tmp = _write_temp_audio(root / "dl")

            def boom(request: TranscribeRequest, *, on_progress=None):
                raise RuntimeError("core panicked")

            workflow = DriveTranscribeWorkflow(
                downloader=FakeDownloader(
                    DownloadResult(
                        file_id="abc123XYZ_-99",
                        temp_path=audio_tmp,
                        filename="meeting.m4a",
                        content_type="audio/mp4",
                        size_bytes=5,
                    )
                ),
                transcribe_fn=boom,
            )
            with self.assertRaises(WorkflowError) as ctx:
                workflow.run(_request(root / "out"))
            self.assertEqual(ctx.exception.stage, WorkflowStage.TRANSCRIBE)
            workspace = next((root / "out").iterdir())
            self.assertEqual((workspace / "meeting.m4a").read_bytes(), b"AUDIO")


if __name__ == "__main__":
    unittest.main()
