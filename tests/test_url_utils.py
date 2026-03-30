import unittest

from lib.url_utils import dedupe_key, is_valid_http_url, normalize_url


class UrlUtilsTests(unittest.TestCase):
    def test_normalize_url_strips_scheme_and_trailing_slash(self):
        self.assertEqual(
            normalize_url("https://Example.com/jobs/123/"),
            "example.com/jobs/123",
        )

    def test_is_valid_http_url(self):
        self.assertTrue(is_valid_http_url("https://example.com/a"))
        self.assertTrue(is_valid_http_url("http://example.com"))
        self.assertFalse(is_valid_http_url("ftp://example.com"))
        self.assertFalse(is_valid_http_url("example.com/no-scheme"))

    def test_dedupe_key_is_case_insensitive(self):
        a = dedupe_key("Senior AI Engineer", "https://Example.com/jobs/1/")
        b = dedupe_key("senior ai engineer", "http://example.com/jobs/1")
        self.assertEqual(a, b)


if __name__ == "__main__":
    unittest.main()
