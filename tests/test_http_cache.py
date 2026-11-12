import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

import requests

from src.ingestion.http_cache import CACHE_TIMESTAMP_HEADER, request_with_cache


def response(status: int, content: bytes, headers: dict | None = None):
    result = requests.Response()
    result.status_code = status
    result._content = content
    result.headers.update(headers or {})
    return result


class HttpCacheTests(unittest.TestCase):
    def test_reuses_response_and_preserves_original_retrieval_time(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            fetch = Mock(return_value=response(200, b'{"ok":true}'))
            kwargs = {
                "method": "GET",
                "url": "https://example.test/search",
                "params": {"term": "hypertension"},
                "cache_dir": Path(temp_dir),
            }

            first = request_with_cache(fetch, **kwargs)
            second = request_with_cache(fetch, **kwargs)

            self.assertEqual(fetch.call_count, 1)
            self.assertEqual(first.content, second.content)
            self.assertEqual(
                first.headers[CACHE_TIMESTAMP_HEADER],
                second.headers[CACHE_TIMESTAMP_HEADER],
            )

    def test_credentials_do_not_change_cache_key_or_enter_cache(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            fetch = Mock(return_value=response(200, b"public response"))
            base = {
                "method": "GET",
                "url": "https://example.test/search",
                "cache_dir": Path(temp_dir),
            }
            request_with_cache(fetch, params={"term": "x", "api_key": "secret-a"}, **base)
            request_with_cache(fetch, params={"term": "x", "api_key": "secret-b"}, **base)

            self.assertEqual(fetch.call_count, 1)
            cache_content = next(Path(temp_dir).glob("*.json")).read_text()
            self.assertNotIn("secret-a", cache_content)
            self.assertNotIn("secret-b", cache_content)

    def test_corrupt_or_digest_mismatched_cache_is_refetched(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            cache_dir = Path(temp_dir)
            fetch = Mock(return_value=response(200, b"fresh"))
            kwargs = {
                "method": "GET",
                "url": "https://example.test/data",
                "params": {},
                "cache_dir": cache_dir,
            }
            request_with_cache(fetch, **kwargs)
            cache_path = next(cache_dir.glob("*.json"))
            cache_path.write_text("{bad json", encoding="utf-8")

            refreshed = request_with_cache(fetch, **kwargs)

            self.assertEqual(refreshed.content, b"fresh")
            self.assertEqual(fetch.call_count, 2)

    def test_non_success_responses_are_not_cached(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            fetch = Mock(return_value=response(503, b"unavailable"))
            kwargs = {
                "method": "GET",
                "url": "https://example.test/data",
                "params": {},
                "cache_dir": Path(temp_dir),
            }

            request_with_cache(fetch, **kwargs)
            request_with_cache(fetch, **kwargs)

            self.assertEqual(fetch.call_count, 2)


if __name__ == "__main__":
    unittest.main()
