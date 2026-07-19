#!/usr/bin/env python3
"""Offline tests for Checkpoint 07.2: subprocess / core / workflow cancellation."""

from __future__ import annotations

import io
import signal
import subprocess
import sys
import tempfile
import threading
import unittest
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence
from unittest import mock

from src.common import CancellationController, OperationCancelled
from src.drive import DownloadResult
from src.transcription import (
    ArtifactKind,
    ProgressStatus,
    Stage,
    TranscribeRequest,
    TranscribeResult,
    TranscriptionError,
    transcribe,
)
from src.transcription.subprocess_runner import (
    DefaultSubprocessRunner,
    StreamingSubprocessRunner,
    _escalate_terminate,
)
from src.transcription.subprocess_runner import CommandResult
from src.workflow import (
    DriveTranscribeRequest,
    DriveTranscribeWorkflow,
    WorkflowCancelled,
    WorkflowStage,
)
from src.workflow.types import WorkflowProgressEvent


@dataclass
class FakePopen:
    """Injectable process double for terminate → wait → kill → reap tests."""

    args: object = None
    kwargs: dict = field(default_factory=dict)
    pid: int = 4242
    returncode: int | None = None
    signals: list[str] = field(default_factory=list)
    wait_timeouts: list[float | None] = field(default_factory=list)
    ignore_terminate: bool = False
    stdout: io.StringIO | None = None
    stderr: io.StringIO | None = None
    block_communicate: threading.Event | None = None

    def __post_init__(self) -> None:
        if self.stdout is None:
            self.stdout = io.StringIO("progress\n")
        if self.stderr is None:
            self.stderr = io.StringIO("")

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        self.wait_timeouts.append(timeout)
        if self.returncode is not None:
            return self.returncode
        if timeout is not None and self.ignore_terminate:
            raise subprocess.TimeoutExpired(cmd="fake", timeout=timeout)
        self.returncode = -9 if "kill" in self.signals else -15
        return self.returncode

    def terminate(self) -> None:
        self.signals.append("terminate")
        if not self.ignore_terminate:
            self.returncode = -15
            if self.block_communicate is not None:
                self.block_communicate.set()

    def kill(self) -> None:
        self.signals.append("kill")
        self.returncode = -9
        if self.block_communicate is not None:
            self.block_communicate.set()

    def apply_group_signal(self, sig: signal.Signals) -> None:
        if sig == signal.SIGTERM:
            self.terminate()
        else:
            self.kill()

    def communicate(self, input=None, timeout=None):  # noqa: ANN001
        del input, timeout
        if self.block_communicate is not None:
            self.block_communicate.wait(timeout=5)
        out = self.stdout.getvalue() if self.stdout is not None else ""
        err = self.stderr.getvalue() if self.stderr is not None else ""
        if self.returncode is None:
            # Do not invent success if escalation already started.
            if "kill" in self.signals:
                self.returncode = -9
            elif "terminate" in self.signals:
                self.returncode = -15
            else:
                self.returncode = 0
        return out, err


@dataclass
class FakeRunner:
    fail_normalize: bool = False
    fail_whisper: bool = False
    raise_on_normalize: BaseException | None = None
    raise_on_whisper: BaseException | None = None
    calls: list[list[str]] | None = None

    def __post_init__(self) -> None:
        if self.calls is None:
            self.calls = []

    def run(
        self,
        command: Sequence[str],
        *,
        cwd: Path | None = None,
        cancellation=None,
        cancel_stage: str = "transcribe",
    ) -> CommandResult:
        del cwd
        if cancellation is not None:
            cancellation.throw_if_cancelled(cancel_stage)
        argv = list(command)
        self.calls.append(argv)
        binary = Path(argv[0]).name

        if binary == "ffmpeg":
            if self.raise_on_normalize is not None:
                raise self.raise_on_normalize
            if self.fail_normalize:
                return CommandResult(returncode=1, stdout="", stderr="ffmpeg boom")
            output = Path(argv[-1])
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(b"RIFF....WAVEfmt fake")
            return CommandResult(returncode=0, stdout="", stderr="")

        if binary == "whisper-cli":
            if self.raise_on_whisper is not None:
                raise self.raise_on_whisper
            if self.fail_whisper:
                return CommandResult(returncode=2, stdout="", stderr="whisper boom")
            output_file = None
            for index, item in enumerate(argv):
                if item == "--output-file" and index + 1 < len(argv):
                    output_file = argv[index + 1]
            assert output_file is not None
            base = Path(output_file)
            base.parent.mkdir(parents=True, exist_ok=True)
            for kind in ArtifactKind:
                if f"--output-{kind.value}" in argv:
                    Path(f"{base}.{kind.value}").write_text(
                        f"{kind.value} content\n",
                        encoding="utf-8",
                    )
            return CommandResult(returncode=0, stdout="", stderr="")

        return CommandResult(returncode=127, stdout="", stderr=f"unknown binary: {binary}")


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

    def download(self, drive_url: str, *, cancellation=None) -> DownloadResult:
        self.calls.append(drive_url)
        if cancellation is not None:
            cancellation.throw_if_cancelled("download")
        if self._error is not None:
            raise self._error
        assert self._result is not None
        return self._result


def _workflow_request(output_root: Path, **overrides: object) -> DriveTranscribeRequest:
    base = dict(
        drive_url="https://drive.google.com/file/d/abc123XYZ_-99/view",
        output_root=output_root,
        language="en",
        model="tiny",
        model_path=Path("/tmp/model.bin"),
        whisper_cli=Path("/tmp/whisper-cli"),
        threads=2,
        meeting_time=datetime(2026, 7, 18, 12, 34, tzinfo=timezone.utc),
        outputs=frozenset({ArtifactKind.TXT, ArtifactKind.SRT}),
        normalize=True,
    )
    base.update(overrides)
    return DriveTranscribeRequest(**base)  # type: ignore[arg-type]


def _fake_success_transcribe(
    request: TranscribeRequest,
    *,
    on_progress=None,
    cancellation=None,
) -> TranscribeResult:
    if cancellation is not None:
        cancellation.throw_if_cancelled("transcribe")
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


def _patch_killpg(created: list[FakePopen]):
    def killpg(pid: int, sig: signal.Signals) -> None:
        del pid
        for proc in created:
            if proc.pid:
                proc.apply_group_signal(sig)
                return
        raise ProcessLookupError(sig)

    return mock.patch(
        "src.transcription.subprocess_runner.os.killpg",
        side_effect=killpg,
    )


class EscalateTerminateTests(unittest.TestCase):
    def test_terminate_wait_kill_reap_order(self) -> None:
        proc = FakePopen(ignore_terminate=True)
        returncode = _escalate_terminate(
            proc,  # type: ignore[arg-type]
            wait_seconds=0.01,
            use_process_group=False,
        )
        self.assertEqual(proc.signals, ["terminate", "kill"])
        self.assertEqual(returncode, -9)
        self.assertIsNotNone(proc.poll())
        self.assertTrue(any(t is not None for t in proc.wait_timeouts))

    def test_process_group_signals_on_posix(self) -> None:
        if sys.platform == "win32":
            self.skipTest("process group contract is posix-only")
        created: list[FakePopen] = []
        proc = FakePopen(ignore_terminate=True)
        created.append(proc)
        with _patch_killpg(created) as killpg:
            returncode = _escalate_terminate(
                proc,  # type: ignore[arg-type]
                wait_seconds=0.01,
                use_process_group=True,
            )
        self.assertGreaterEqual(killpg.call_count, 2)
        self.assertEqual(proc.signals, ["terminate", "kill"])
        self.assertEqual(returncode, -9)


class SubprocessCancellationTests(unittest.TestCase):
    def test_default_runner_cancel_during_communicate(self) -> None:
        created: list[FakePopen] = []
        gate = threading.Event()

        def factory(*args, **kwargs):  # noqa: ANN001
            proc = FakePopen(
                args=args,
                kwargs=kwargs,
                ignore_terminate=True,
                block_communicate=gate,
            )
            created.append(proc)
            return proc

        controller = CancellationController()
        runner = DefaultSubprocessRunner(
            process_factory=factory,
            terminate_wait_seconds=0.01,
        )
        errors: list[BaseException] = []

        def worker() -> None:
            try:
                with _patch_killpg(created):
                    runner.run(
                        ["fake-bin"],
                        cancellation=controller.token,
                        cancel_stage="normalize",
                    )
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        thread = threading.Thread(target=worker)
        thread.start()
        for _ in range(100):
            if created:
                break
            thread.join(timeout=0.01)
        self.assertEqual(len(created), 1)
        if sys.platform != "win32":
            self.assertTrue(created[0].kwargs.get("start_new_session"))
        self.assertTrue(controller.cancel())
        thread.join(timeout=2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], OperationCancelled)
        self.assertEqual(errors[0].stage, "normalize")
        self.assertEqual(created[0].signals, ["terminate", "kill"])
        self.assertIsNotNone(created[0].poll())

    def test_streaming_cancel_reaps_process(self) -> None:
        created: list[FakePopen] = []

        class BlockingStdout(io.StringIO):
            def __init__(self) -> None:
                super().__init__("chunk-one\n")
                self._reads = 0
                self.gate = threading.Event()

            def read(self, size: int = -1) -> str:  # noqa: A003
                self._reads += 1
                if self._reads == 1:
                    return "chunk-one\n"
                self.gate.wait(timeout=2)
                return ""

        def factory(*args, **kwargs):  # noqa: ANN001
            proc = FakePopen(args=args, kwargs=kwargs, stdout=BlockingStdout())
            created.append(proc)
            return proc

        controller = CancellationController()
        sink = io.StringIO()
        runner = StreamingSubprocessRunner(
            sink=sink,
            process_factory=factory,
            terminate_wait_seconds=0.05,
        )
        errors: list[BaseException] = []

        def worker() -> None:
            try:
                with _patch_killpg(created):
                    runner.run(
                        ["fake-whisper"],
                        cancellation=controller.token,
                        cancel_stage="transcribe",
                    )
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        thread = threading.Thread(target=worker)
        thread.start()
        for _ in range(100):
            if sink.getvalue():
                break
            thread.join(timeout=0.01)
        self.assertIn("chunk-one", sink.getvalue())
        self.assertTrue(controller.cancel())
        assert isinstance(created[0].stdout, BlockingStdout)
        created[0].stdout.gate.set()
        thread.join(timeout=2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], OperationCancelled)
        self.assertIn("terminate", created[0].signals)
        self.assertIsNotNone(created[0].poll())


class CoreCancellationTests(unittest.TestCase):
    def _request(self, root: Path, **overrides: object) -> TranscribeRequest:
        audio = root / "a.wav"
        audio.write_bytes(b"source")
        model = root / "m.bin"
        model.write_bytes(b"m")
        whisper = root / "whisper-cli"
        whisper.write_text("#!/bin/sh\n", encoding="utf-8")
        whisper.chmod(0o755)
        base = dict(
            audio_path=audio,
            language="zh",
            model="small",
            model_path=model,
            whisper_cli=whisper,
            threads=1,
            output_dir=root / "out",
            stem="meeting",
            outputs=frozenset({ArtifactKind.TXT}),
            normalize=True,
            keep_normalized=True,
            ffmpeg=Path("ffmpeg"),
        )
        base.update(overrides)
        return TranscribeRequest(**base)  # type: ignore[arg-type]

    def test_cancel_before_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            controller = CancellationController()
            controller.cancel()
            events: list[ProgressStatus] = []
            with self.assertRaises(OperationCancelled) as ctx:
                transcribe(
                    self._request(root),
                    runner=FakeRunner(),
                    cancellation=controller.token,
                    on_progress=lambda e: events.append(e.status),
                )
            self.assertEqual(ctx.exception.stage, Stage.VALIDATE_INPUT.value)
            self.assertEqual(events.count(ProgressStatus.CANCELLED), 1)
            self.assertTrue((root / "a.wav").is_file())

    def test_cancel_during_normalize_clears_partial_keeps_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            controller = CancellationController()

            class CancelNormalizeRunner(FakeRunner):
                def run(self, command, *, cwd=None, cancellation=None, cancel_stage="transcribe"):
                    if Path(list(command)[0]).name == "ffmpeg":
                        controller.cancel()
                    return super().run(
                        command,
                        cwd=cwd,
                        cancellation=cancellation,
                        cancel_stage=cancel_stage,
                    )

            with self.assertRaises(OperationCancelled) as ctx:
                transcribe(
                    self._request(root),
                    runner=CancelNormalizeRunner(),
                    cancellation=controller.token,
                )
            self.assertEqual(ctx.exception.stage, Stage.NORMALIZE.value)
            self.assertTrue((root / "a.wav").is_file())
            out = root / "out"
            if out.exists():
                self.assertEqual(list(out.glob("*_norm16k.wav")), [])
                self.assertEqual(list(out.glob("*_transcription.*")), [])

    def test_cancel_during_transcribe_clears_partial_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            controller = CancellationController()

            class CancelWhisperRunner(FakeRunner):
                def run(self, command, *, cwd=None, cancellation=None, cancel_stage="transcribe"):
                    argv = list(command)
                    if Path(argv[0]).name == "whisper-cli":
                        base = None
                        for index, item in enumerate(argv):
                            if item == "--output-file" and index + 1 < len(argv):
                                base = Path(argv[index + 1])
                        if base is not None:
                            partial = Path(f"{base}.txt")
                            partial.parent.mkdir(parents=True, exist_ok=True)
                            partial.write_text("partial\n", encoding="utf-8")
                        controller.cancel()
                    return super().run(
                        command,
                        cwd=cwd,
                        cancellation=cancellation,
                        cancel_stage=cancel_stage,
                    )

            with self.assertRaises(OperationCancelled) as ctx:
                transcribe(
                    self._request(root),
                    runner=CancelWhisperRunner(),
                    cancellation=controller.token,
                )
            self.assertEqual(ctx.exception.stage, Stage.TRANSCRIBE.value)
            self.assertTrue((root / "a.wav").is_file())
            out = root / "out"
            self.assertEqual(list(out.glob("*_transcription.*")), [])
            self.assertEqual(list(out.glob("*_norm16k.wav")), [])

    def test_success_then_cancel_is_noop_for_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            controller = CancellationController()
            result = transcribe(
                self._request(root),
                runner=FakeRunner(),
                cancellation=controller.token,
            )
            self.assertTrue(controller.cancel())
            for path in result.artifacts.values():
                self.assertTrue(path.is_file())
            self.assertTrue(result.raw_audio_path.is_file())

    def test_source_mutation_after_artifacts_cleans_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            audio = root / "a.wav"
            audio.write_bytes(b"source")

            class MutateSourceRunner(FakeRunner):
                def run(self, command, *, cwd=None, cancellation=None, cancel_stage="transcribe"):
                    argv = list(command)
                    result = super().run(
                        command,
                        cwd=cwd,
                        cancellation=cancellation,
                        cancel_stage=cancel_stage,
                    )
                    if Path(argv[0]).name == "whisper-cli":
                        audio.write_bytes(b"mutated-source")
                    return result

            with self.assertRaises(TranscriptionError) as ctx:
                transcribe(
                    self._request(root, normalize=False),
                    runner=MutateSourceRunner(),
                )
            self.assertEqual(ctx.exception.stage, Stage.VALIDATE_ARTIFACTS)
            self.assertIn("modified", ctx.exception.message)
            out = root / "out"
            self.assertEqual(list(out.glob("*_transcription.*")), [])
            self.assertEqual(audio.read_bytes(), b"mutated-source")


class WorkflowCancellationTests(unittest.TestCase):
    def test_download_cancel_no_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            audio_tmp = root / "dl" / "meeting.m4a"
            audio_tmp.parent.mkdir(parents=True)
            audio_tmp.write_bytes(b"AUDIO")
            controller = CancellationController()
            controller.cancel()
            events: list[WorkflowProgressEvent] = []
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
            with self.assertRaises(WorkflowCancelled) as ctx:
                workflow.run(
                    _workflow_request(root / "out"),
                    on_progress=events.append,
                    cancellation=controller.token,
                )
            self.assertEqual(ctx.exception.stage, WorkflowStage.DOWNLOAD)
            self.assertIsNone(ctx.exception.workspace_dir)
            out = root / "out"
            self.assertTrue(not out.exists() or list(out.glob("*")) == [])
            self.assertEqual(
                sum(1 for e in events if e.status == ProgressStatus.CANCELLED),
                1,
            )
            self.assertFalse(any(e.status == ProgressStatus.FAILED for e in events))

    def test_cancel_after_download_before_workspace_cleans_temp(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            audio_tmp = root / "dl" / "meeting.m4a"
            audio_tmp.parent.mkdir(parents=True)
            audio_tmp.write_bytes(b"AUDIO")
            controller = CancellationController()

            class CancelAfterDownload(FakeDownloader):
                def download(self, drive_url: str, *, cancellation=None) -> DownloadResult:
                    result = super().download(drive_url, cancellation=cancellation)
                    controller.cancel()
                    return result

            events: list[WorkflowProgressEvent] = []
            workflow = DriveTranscribeWorkflow(
                downloader=CancelAfterDownload(
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
            with self.assertRaises(WorkflowCancelled) as ctx:
                workflow.run(
                    _workflow_request(root / "out"),
                    on_progress=events.append,
                    cancellation=controller.token,
                )
            self.assertEqual(ctx.exception.stage, WorkflowStage.WORKSPACE)
            self.assertFalse(audio_tmp.exists())
            out = root / "out"
            self.assertTrue(not out.exists() or list(out.glob("*")) == [])
            self.assertEqual(
                sum(1 for e in events if e.status == ProgressStatus.CANCELLED),
                1,
            )

    def test_cancel_during_normalize_via_core_preserves_raw(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            audio_tmp = root / "dl" / "meeting.m4a"
            audio_tmp.parent.mkdir(parents=True)
            audio_tmp.write_bytes(b"AUDIO")
            model = root / "model.bin"
            model.write_bytes(b"m")
            whisper = root / "whisper-cli"
            whisper.write_text("#!/bin/sh\n", encoding="utf-8")
            whisper.chmod(0o755)
            controller = CancellationController()

            class CancelNormalizeRunner(FakeRunner):
                def run(self, command, *, cwd=None, cancellation=None, cancel_stage="transcribe"):
                    if Path(list(command)[0]).name == "ffmpeg":
                        controller.cancel()
                    return super().run(
                        command,
                        cwd=cwd,
                        cancellation=cancellation,
                        cancel_stage=cancel_stage,
                    )

            def real_transcribe(request, *, on_progress=None, cancellation=None):
                return transcribe(
                    request,
                    runner=CancelNormalizeRunner(),
                    on_progress=on_progress,
                    cancellation=cancellation,
                )

            events: list[WorkflowProgressEvent] = []
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
                transcribe_fn=real_transcribe,
            )
            with self.assertRaises(WorkflowCancelled) as ctx:
                workflow.run(
                    _workflow_request(
                        root / "out",
                        model_path=model,
                        whisper_cli=whisper,
                    ),
                    on_progress=events.append,
                    cancellation=controller.token,
                )
            self.assertEqual(ctx.exception.stage, WorkflowStage.NORMALIZE)
            assert ctx.exception.raw_audio_path is not None
            self.assertTrue(ctx.exception.raw_audio_path.is_file())
            assert ctx.exception.workspace_dir is not None
            self.assertTrue((ctx.exception.workspace_dir / "source_meta.json").is_file())
            self.assertEqual(
                list(ctx.exception.workspace_dir.glob("*_norm16k.wav")),
                [],
            )
            self.assertEqual(
                list(ctx.exception.workspace_dir.glob("*_transcription.*")),
                [],
            )
            self.assertIn(ctx.exception.raw_audio_path, ctx.exception.retained_paths)
            self.assertEqual(
                sum(1 for e in events if e.status == ProgressStatus.CANCELLED),
                1,
            )

    def test_cancel_during_transcribe_via_core_preserves_raw(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            audio_tmp = root / "dl" / "meeting.m4a"
            audio_tmp.parent.mkdir(parents=True)
            audio_tmp.write_bytes(b"AUDIO")
            model = root / "model.bin"
            model.write_bytes(b"m")
            whisper = root / "whisper-cli"
            whisper.write_text("#!/bin/sh\n", encoding="utf-8")
            whisper.chmod(0o755)
            controller = CancellationController()

            class CancelWhisperRunner(FakeRunner):
                def run(self, command, *, cwd=None, cancellation=None, cancel_stage="transcribe"):
                    argv = list(command)
                    if Path(argv[0]).name == "whisper-cli":
                        base = None
                        for index, item in enumerate(argv):
                            if item == "--output-file" and index + 1 < len(argv):
                                base = Path(argv[index + 1])
                        if base is not None:
                            Path(f"{base}.txt").write_text("partial\n", encoding="utf-8")
                        controller.cancel()
                    return super().run(
                        command,
                        cwd=cwd,
                        cancellation=cancellation,
                        cancel_stage=cancel_stage,
                    )

            def real_transcribe(request, *, on_progress=None, cancellation=None):
                return transcribe(
                    request,
                    runner=CancelWhisperRunner(),
                    on_progress=on_progress,
                    cancellation=cancellation,
                )

            events: list[WorkflowProgressEvent] = []
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
                transcribe_fn=real_transcribe,
            )
            with self.assertRaises(WorkflowCancelled) as ctx:
                workflow.run(
                    _workflow_request(
                        root / "out",
                        model_path=model,
                        whisper_cli=whisper,
                    ),
                    on_progress=events.append,
                    cancellation=controller.token,
                )
            self.assertEqual(ctx.exception.stage, WorkflowStage.TRANSCRIBE)
            assert ctx.exception.workspace_dir is not None
            self.assertEqual(
                list(ctx.exception.workspace_dir.glob("*_transcription.*")),
                [],
            )
            assert ctx.exception.raw_audio_path is not None
            self.assertTrue(ctx.exception.raw_audio_path.is_file())
            self.assertEqual(
                sum(1 for e in events if e.status == ProgressStatus.CANCELLED),
                1,
            )

    def test_success_then_cancel_does_not_delete_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            audio_tmp = root / "dl" / "meeting.m4a"
            audio_tmp.parent.mkdir(parents=True)
            audio_tmp.write_bytes(b"AUDIO")
            controller = CancellationController()
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
                _workflow_request(root / "out"),
                cancellation=controller.token,
            )
            self.assertTrue(controller.cancel())
            self.assertTrue(result.raw_transcript_path.is_file())
            for path in result.artifacts.values():
                self.assertTrue(path.is_file())

    def test_legacy_downloader_and_transcribe_signatures_without_cancellation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            audio_tmp = root / "dl" / "meeting.m4a"
            audio_tmp.parent.mkdir(parents=True)
            audio_tmp.write_bytes(b"AUDIO")

            class LegacyDownloader:
                def __init__(self, result: DownloadResult) -> None:
                    self._result = result
                    self.calls: list[str] = []

                def download(self, drive_url: str) -> DownloadResult:
                    self.calls.append(drive_url)
                    return self._result

            def legacy_transcribe(request: TranscribeRequest, *, on_progress=None) -> TranscribeResult:
                return _fake_success_transcribe(request, on_progress=on_progress)

            downloader = LegacyDownloader(
                DownloadResult(
                    file_id="abc123XYZ_-99",
                    temp_path=audio_tmp,
                    filename="meeting.m4a",
                    content_type="audio/mp4",
                    size_bytes=5,
                )
            )
            workflow = DriveTranscribeWorkflow(
                downloader=downloader,  # type: ignore[arg-type]
                transcribe_fn=legacy_transcribe,
            )
            result = workflow.run(_workflow_request(root / "out"))
            self.assertEqual(
                downloader.calls,
                ["https://drive.google.com/file/d/abc123XYZ_-99/view"],
            )
            self.assertTrue(result.raw_transcript_path.is_file())


if __name__ == "__main__":
    unittest.main()
