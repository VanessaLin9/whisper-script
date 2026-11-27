"""Python workflow entry points must work without invoking a shell."""

from __future__ import annotations

import contextlib
import io
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pipelines import multilang_batch
from src import workflow_cli

ROOT = Path(__file__).resolve().parents[1]
FAKE_BIN = ROOT / 'tests' / 'fake_bin'


class WorkflowCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        self.whisper_root = self.root / 'whisper'
        bin_dir = self.whisper_root / 'build' / 'bin'
        bin_dir.mkdir(parents=True)
        (bin_dir / 'whisper-cli').symlink_to(FAKE_BIN / 'whisper-cli')
        model_dir = self.whisper_root / 'models'
        model_dir.mkdir()
        (model_dir / 'ggml-small.bin').write_text('model', encoding='utf-8')
        self.records = self.root / 'records'
        (self.repo / '.env').write_text(
            f'WHISPER_ROOT={self.whisper_root}\nMEETING_RECORDS_DIR={self.records}\n'
            'PREFERRED_MODEL=small\nDEFAULT_LANGUAGE=zh\nTHREADS=2\n', encoding='utf-8')
        self.path = str(FAKE_BIN) + os.pathsep + os.environ.get('PATH', '')
        self.calls = []
        original_popen = subprocess.Popen

        def checked_popen(command=None, *args, **kwargs):
            if command is None:
                command = kwargs.pop("args")
            self.assertFalse(kwargs.get('shell'))
            executable = Path(str(command[0])).name.lower()
            self.assertNotIn(executable, {'bash', 'sh', 'bash.exe', 'sh.exe'})
            self.calls.append(command)
            return original_popen(command, *args, **kwargs)

        self.popen_patch = patch.object(subprocess, 'Popen', side_effect=checked_popen)
        self.popen_patch.start()
        self.addCleanup(self.popen_patch.stop)
        self.env_patch = patch.dict(os.environ, {'PATH': self.path})
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)
        self.repo_patch = patch.object(workflow_cli, 'REPO_ROOT', self.repo)
        self.repo_patch.start()
        self.addCleanup(self.repo_patch.stop)

    def test_single_file_python_entry(self) -> None:
        source = self.root / '2026-07-17_1500_demo.wav'
        source.write_bytes(b'audio')
        with patch.object(sys, 'stdin', io.StringIO(str(source) + '\n')), \
             patch.object(workflow_cli, 'copy_to_clipboard') as clipboard, \
             patch.object(workflow_cli, 'show_in_folder') as show:
            with contextlib.redirect_stdout(io.StringIO()):
                status = workflow_cli.main(['transcribe-english'])
        self.assertEqual(status, 0)
        self.assertEqual(len(list(self.records.rglob('*_transcription.txt'))), 1)
        clipboard.assert_called_once()
        show.assert_called_once()
        self.assertTrue(self.calls)

    def test_batch_python_and_pipeline_entries_natural_order(self) -> None:
        segments = self.root / 'segments'
        segments.mkdir()
        for name in ('segment_10.wav', 'segment_2.wav', 'segment_1.wav'):
            (segments / name).write_bytes(b'audio')
        with contextlib.redirect_stdout(io.StringIO()) as output:
            status = workflow_cli.main(['multi-lang', str(segments)])
        self.assertEqual(status, 0)
        self.assertLess(output.getvalue().find('Transcribing: segment_2'), output.getvalue().find('Transcribing: segment_10'))
        self.assertEqual(len(list((segments / 'transcripts').glob('*.txt'))), 3)
        self.assertFalse((segments / 'failed_segments.log').exists())
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(multilang_batch.run_transcribe(segments, self.root / 'unused'))


if __name__ == '__main__':
    unittest.main()
