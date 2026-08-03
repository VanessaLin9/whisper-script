import json
import tempfile
import unittest
from pathlib import Path

from src.postprocessing.preparer import prepare_file, prepare_transcript


class TranscriptPreparerTests(unittest.TestCase):
    def test_preserves_fragment_text_and_removes_only_adjacent_duplicates(self):
        source = "  第一段  \n第一段\n第二段\n第一段\n\n"

        prepared, stats = prepare_transcript(source, paragraph_chars=40)

        self.assertEqual(prepared, "第一段，第二段，第一段\n")
        self.assertEqual(stats.input_fragments, 4)
        self.assertEqual(stats.output_fragments, 3)
        self.assertEqual(stats.adjacent_duplicates_removed, 1)

    def test_rejects_semantically_risky_small_paragraph_limit(self):
        with self.assertRaises(ValueError):
            prepare_transcript("內容", paragraph_chars=39)

    def test_file_output_is_fail_closed_and_manifest_is_auditable(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "meeting.txt"
            output = root / "meeting_prepared.txt"
            source.write_text("Alpha\nAlpha\nBeta\n", encoding="utf-8")

            prepared, manifest, stats = prepare_file(source, output)

            self.assertEqual(prepared.read_text(encoding="utf-8"), "Alpha，Beta\n")
            payload = json.loads(manifest.read_text(encoding="utf-8"))
            self.assertEqual(payload["stats"]["adjacent_duplicates_removed"], 1)
            self.assertEqual(payload["source"], str(source.resolve()))
            self.assertEqual(stats.output_fragments, 2)
            with self.assertRaises(FileExistsError):
                prepare_file(source, output)

    def test_never_allows_source_overwrite(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "meeting.txt"
            source.write_text("內容\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                prepare_file(source, source, force=True)


if __name__ == "__main__":
    unittest.main()
