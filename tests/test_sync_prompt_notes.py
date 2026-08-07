import unittest

from scripts.sync_prompt_notes import _api_page_id, _render_note


class PromptSyncTests(unittest.TestCase):
    def test_api_page_id_accepts_slug_prefixed_notion_id(self):
        self.assertEqual(
            _api_page_id("whisper-3a3df30c-c71c-8151-95bad39aeb1eb6e1"),
            "3a3df30c-c71c-8151-95bad39aeb1eb6e1",
        )

    def test_rendered_note_has_private_frontmatter_and_source(self):
        rendered = _render_note(
            key="inno",
            title="inno｜Inno Group／AI Team",
            page_id="39ddf30c-c71c-81b6-a658-f0ec07fd7e36",
            page_url="https://app.notion.com/p/39ddf30cc71c81b6a658f0ec07fd7e36",
            page={"properties": {"Name": {"type": "title", "title": [{"plain_text": "Prompt"}]}}},
            body=["## Terms", "- Athena"],
        )
        self.assertIn("key: inno", rendered)
        self.assertIn("source: https://app.notion.com/p/39ddf30cc71c81b6a658f0ec07fd7e36", rendered)
        self.assertIn("- Athena", rendered)


if __name__ == "__main__":
    unittest.main()
