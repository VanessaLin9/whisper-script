import tempfile
import unittest
from pathlib import Path

from src.prompt_profiles import load_prompt_profiles


class PromptProfileTests(unittest.TestCase):
    def test_local_note_title_overrides_fallback_and_records_path(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            notes_dir = Path(temp_dir)
            note = notes_dir / "inno.md"
            note.write_text(
                "---\n"
                "key: inno\n"
                "title: inno｜Private local vocabulary\n"
                "source: https://app.notion.com/p/example\n"
                "---\n\n# Prompt\n",
                encoding="utf-8",
            )

            profiles = load_prompt_profiles(notes_dir)

        self.assertEqual(profiles["inno"]["label"], "inno｜Private local vocabulary")
        self.assertEqual(profiles["inno"]["source"], "https://app.notion.com/p/example")
        self.assertEqual(profiles["inno"]["local_path"], str(note.resolve()))
        self.assertIn("whisper", profiles)
        self.assertIn("new", profiles)

    def test_missing_directory_keeps_safe_fallbacks(self):
        profiles = load_prompt_profiles(Path("/tmp/does-not-exist-whisper-script"))
        self.assertEqual(profiles["inno"]["source"], "39ddf30c-c71c-81b6-a658-f0ec07fd7e36")
        self.assertNotIn("local_path", profiles["inno"])


if __name__ == "__main__":
    unittest.main()
