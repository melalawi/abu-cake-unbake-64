"""File digests follow the bytes and the stat signature."""

import hashlib
import os

from tests.kit import TempCase
from unbake import inputs


class DigestTests(TempCase):
    def test_digest_is_sha256_of_bytes(self) -> None:
        path = self.root / "a.c"
        path.write_bytes(b"int a;\n")
        self.assertEqual(inputs.digest(path, algorithm="sha256", reuse=True), hashlib.sha256(b"int a;\n").hexdigest())

    def test_digest_follows_rewrites(self) -> None:
        path = self.root / "a.c"
        path.write_bytes(b"one")
        first = inputs.digest(path, algorithm="sha256", reuse=True)
        cases = [
            ("same size new bytes", b"two", True),
            ("same bytes rewritten", b"two", False),
            ("longer", b"three!", True),
            ("longer rewritten", b"three!", False),
        ]
        previous = first
        for label, data, changes in cases:
            with self.subTest(label):
                path.write_bytes(data)
                os.utime(path, ns=(10**18 + len(label), 10**18 + len(label)))
                current = inputs.digest(path, algorithm="sha256", reuse=True)
                self.assertEqual(current != previous, changes)
                self.assertEqual(current, hashlib.sha256(data).hexdigest())
                previous = current

    def test_digest_of_distinct_files_with_equal_bytes_is_equal(self) -> None:
        first, second = self.root / "x", self.root / "y"
        first.write_bytes(b"same")
        second.write_bytes(b"same")
        self.assertEqual(
            inputs.digest(first, algorithm="sha256", reuse=True), inputs.digest(second, algorithm="sha256", reuse=True)
        )
