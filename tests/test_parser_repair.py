import sys
import types
import unittest

# scripts.job_link_completion imports main.py, which exits if selenium is not installed.
# Inject a lightweight stub so these parser-only tests remain unit-level and dependency-free.
if "main" not in sys.modules:
    sys.modules["main"] = types.SimpleNamespace(OUTPUT_DIR=None, _LOG_PATH=None)

from scripts.job_link_completion import JobLinkCompletionLoop, _extract_json_value


class _CollectorStub:
    def __init__(self, responses):
        self.responses = list(responses)

    def ask(self, prompt: str) -> str:
        _ = prompt
        if self.responses:
            return self.responses.pop(0)
        return ""


class ParserRepairTests(unittest.TestCase):
    def test_extract_json_value_reads_fenced_json(self):
        text = """```json\n[{\"title\":\"A\",\"url\":\"https://x\"}]\n```"""
        parsed = _extract_json_value(text)
        self.assertIsInstance(parsed, list)
        self.assertEqual(parsed[0]["title"], "A")

    def test_repair_array_uses_collector_to_fix_invalid_json(self):
        loop = JobLinkCompletionLoop()
        loop.collector = _CollectorStub(['[{"title":"A","url":"https://example.com","company":"C","location":"Remote"}]'])
        rows = loop._repair_array("not json")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["url"], "https://example.com")

    def test_repair_array_falls_back_to_regex_urls(self):
        loop = JobLinkCompletionLoop()
        bad = "links: https://a.example/jobs/1 and https://b.example/jobs/2"
        # Exhaust repair attempts with non-JSON responses.
        loop.collector = _CollectorStub([bad, bad, bad])
        rows = loop._repair_array("still not json")
        self.assertEqual(len(rows), 2)
        self.assertTrue(rows[0]["url"].startswith("https://"))


if __name__ == "__main__":
    unittest.main()
