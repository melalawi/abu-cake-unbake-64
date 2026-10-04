import errno
import hashlib
import os
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import Mock, patch

from unbake.cache import Cache, key, parsed
from unbake.config import Held


class CacheTests(unittest.TestCase):
    def test_shared_parse_reuses_copies_but_observes_every_edit(self) -> None:
        other = self.directory / "copy.o"
        other.write_bytes(self.source.read_bytes())
        with patch("unbake.cache._parsed", {}), patch("unbake.cache._remembered", {}):
            make = Mock(side_effect=["first", "edited", "other version"])
            self.assertEqual(parsed("fixture", self.source, make, extra="us", share=True), "first")
            self.assertEqual(parsed("fixture", other, make, extra="us", share=True), "first")
            previous = other.stat()
            other.write_bytes(b"Object bytes")
            os.utime(other, ns=(previous.st_atime_ns, previous.st_mtime_ns))
            self.assertEqual(parsed("fixture", other, make, extra="us", share=True), "edited")
            self.assertEqual(parsed("fixture", other, make, extra="eu", share=True), "other version")
            self.assertEqual(make.call_count, 3)

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name).resolve()
        self.cache = Cache(self.directory / "cache")
        self.source = self.directory / "source.o"
        self.source.write_bytes(b"object bytes")
        self.identity = key(self.source, "flags")

    def test_keys_hash_file_bytes_and_part_boundaries(self) -> None:
        self.assertEqual(key(self.source), key(self.source.read_bytes()))
        self.assertEqual(key("hello"), key(b"hello"))
        self.assertNotEqual(key("ab", "c"), key("a", "bc"))
        self.assertNotEqual(key("ab"), key("ab", ""))
        self.assertEqual(key(), hashlib.sha256().hexdigest())
        self.assertEqual(len(self.identity), 64)
        previous = key(self.source)
        self.source.write_bytes(b"different object")
        self.assertNotEqual(previous, key(self.source))

    def test_missing_key_file_is_named(self) -> None:
        missing = self.directory / "missing.o"
        with self.assertRaises(Held) as raised:
            key(missing)
        self.assertIn(str(missing), raised.exception.reason)

    def test_unsupported_key_part_is_named(self) -> None:
        with self.assertRaises(Held) as raised:
            key(3)
        self.assertIn("part 0", raised.exception.reason)

    def test_get_put_layout_and_independent_copy(self) -> None:
        self.assertIsNone(self.cache.get("cc", self.identity))
        result = self.cache.put("cc", self.identity, self.source)
        self.assertEqual(result, self.cache.root / "cc" / self.identity[:2] / self.identity)
        self.assertEqual(self.cache.get("cc", self.identity), result)
        self.assertEqual(result.read_bytes(), b"object bytes")
        self.source.write_bytes(b"updated source")
        self.assertEqual(result.read_bytes(), b"object bytes")

    def test_get_allows_atomic_publication_after_missing_stat(self) -> None:
        target = self.cache.path("cc", self.identity)
        original_stat = Path.stat
        missing = True

        def publish(path: Path, *args: Any, **kwargs: Any) -> Any:
            nonlocal missing
            if path == target and missing:
                missing = False
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(b"published object")
                raise FileNotFoundError(errno.ENOENT, "No such file", str(path))
            return original_stat(path, *args, **kwargs)

        with patch.object(Path, "stat", publish):
            self.assertIsNone(self.cache.get("cc", self.identity))
        self.assertEqual(self.cache.get("cc", self.identity), target)
        self.assertEqual(target.read_bytes(), b"published object")

    def test_get_still_refuses_directory_artifacts(self) -> None:
        target = self.cache.path("cc", self.identity)
        target.mkdir(parents=True)
        with self.assertRaisesRegex(Held, "expected cached file"):
            self.cache.get("cc", self.identity)

    def test_put_publishes_complete_copy_via_atomic_replace(self) -> None:
        from unbake import cache

        original_replace = cache.os.replace
        replacements = []

        def replace(source: Path, destination: int) -> None:
            self.assertEqual(Path(source).read_bytes(), self.source.read_bytes())
            self.assertEqual(Path(source).parent, Path(destination).parent)
            self.assertFalse(Path(destination).exists())
            replacements.append((source, destination))
            original_replace(source, destination)

        with patch.object(cache.os, "replace", side_effect=replace):
            result = self.cache.put("cc", self.identity, self.source)
        self.assertEqual(len(replacements), 1)
        self.assertEqual(list(result.parent.iterdir()), [result])

    def test_failed_put_keeps_previous_artifact_and_cleans_temporary(self) -> None:
        existing = self.cache.put("cc", self.identity, self.source)
        with (
            patch("unbake.cache.atomic_files.copyfile", side_effect=OSError("copy refused")),
            self.assertRaises(Held),
        ):
            self.cache.put("cc", self.identity, self.source)
        self.assertEqual(existing.read_bytes(), b"object bytes")
        self.assertEqual(list(existing.parent.iterdir()), [existing])

    def test_missing_put_source_is_named(self) -> None:
        missing = self.directory / "missing.o"
        with self.assertRaises(Held) as raised:
            self.cache.put("cc", self.identity, missing)
        self.assertIn(str(missing), raised.exception.reason)

    def test_produce_reuses_artifact_without_calling_make(self) -> None:
        calls = []

        def make(output: Any) -> Any:
            self.assertFalse(output.exists())
            calls.append(output)
            output.write_bytes(b"built object")

        result = self.cache.produce("as", self.identity, make)
        reused = self.cache.produce("as", self.identity, make)
        self.assertEqual(reused, result)
        self.assertEqual(len(calls), 1)
        self.assertEqual(result.read_bytes(), b"built object")
        self.assertEqual(list(result.parent.iterdir()), [result])

    def test_produce_failure_publishes_nothing(self) -> None:
        def make(output: Any) -> Any:
            output.write_bytes(b"partial")
            raise Held("cc", "compiler refused")

        with self.assertRaises(Held) as raised:
            self.cache.produce("cc", self.identity, make)
        self.assertEqual(raised.exception.phase, "cc")
        self.assertIsNone(self.cache.get("cc", self.identity))
        self.assertEqual(list(self.cache.path("cc", self.identity).parent.iterdir()), [])

    def test_missing_producer_output_is_held(self) -> None:
        with self.assertRaises(Held) as raised:
            self.cache.produce("extract", self.identity, lambda output: None)
        self.assertIn("make output", raised.exception.reason)
        self.assertIsNone(self.cache.get("extract", self.identity))

    def test_invalid_kind_and_key_refuse_path_traversal(self) -> None:
        for kind, content_key, field in (
            ("../cc", self.identity, "kind"),
            ("", self.identity, "kind"),
            ("cc", "../bad", "key"),
            ("cc", "", "key"),
        ):
            with self.subTest(kind=kind, key=content_key):
                with self.assertRaises(Held) as raised:
                    self.cache.get(kind, content_key)
                self.assertIn(field, raised.exception.reason)

    def test_all_contract_kinds_are_separate(self) -> None:
        paths = [self.cache.put(kind, self.identity, self.source) for kind in ("cc", "as", "link", "score", "extract")]
        self.assertEqual(len(set(paths)), 5)


if __name__ == "__main__":
    unittest.main()
