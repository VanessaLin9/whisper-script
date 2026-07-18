#!/usr/bin/env python3
"""Offline unit tests for shared .env loading."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from env_loader import expand_documented_vars, load_env


class EnvLoaderTests(unittest.TestCase):
    def test_quoted_language_and_inline_comments(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env"
            env_path.write_text(
                "\n".join(
                    [
                        'DEFAULT_LANGUAGE="zh"',
                        'PREFERRED_MODEL="small"   # (tiny/base/small/medium/large)',
                        "WHISPER_ROOT=/tmp/whisper.cpp",
                        "",
                        "# comment only",
                        "THREADS=4",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            env = load_env(env_path)

            self.assertEqual(env["DEFAULT_LANGUAGE"], "zh")
            self.assertEqual(env["PREFERRED_MODEL"], "small")
            self.assertEqual(env["WHISPER_ROOT"], "/tmp/whisper.cpp")
            self.assertEqual(env["THREADS"], "4")
            self.assertNotIn('"', env["DEFAULT_LANGUAGE"])

    def test_home_expansion_and_spaces_in_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env"
            env_path.write_text(
                "\n".join(
                    [
                        'MEETING_RECORDS_DIR="$HOME/Meeting Records"',
                        'TRANSCRIPTS_DIR=~/MeetingRecords/Transcripts',
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            env = load_env(env_path)
            home = str(Path.home())

            self.assertEqual(env["MEETING_RECORDS_DIR"], f"{home}/Meeting Records")
            self.assertEqual(env["TRANSCRIPTS_DIR"], f"{home}/MeetingRecords/Transcripts")

    def test_does_not_expand_arbitrary_shell_fragments(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env"
            env_path.write_text(
                'WHISPER_ROOT="$HOME/$(echo pwned)/whisper.cpp"\n',
                encoding="utf-8",
            )

            env = load_env(env_path)
            home = str(Path.home())

            self.assertEqual(
                env["WHISPER_ROOT"],
                f"{home}/$(echo pwned)/whisper.cpp",
            )

    def test_env_example_parses_usable_values(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        example = repo_root / ".env.example"
        text = example.read_text(encoding="utf-8")
        text = text.replace("/Users/YourName/whisper.cpp", "/tmp/whisper.cpp")

        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env"
            env_path.write_text(text, encoding="utf-8")
            env = load_env(env_path)

            self.assertEqual(env["DEFAULT_LANGUAGE"], "zh")
            self.assertEqual(env["PREFERRED_MODEL"], "small")
            self.assertEqual(env["MIC_DEVICE"], ":0")
            self.assertTrue(env["MEETING_RECORDS_DIR"].endswith("/MeetingRecords"))
            self.assertNotIn("$(", expand_documented_vars("$HOME/ok"))


if __name__ == "__main__":
    unittest.main()
