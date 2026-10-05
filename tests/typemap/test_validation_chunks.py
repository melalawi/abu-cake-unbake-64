"""Validation jobs: about one per worker, never an empty or oversized chunk."""

import unittest

from unbake.typemap.database import chunk_width


class ChunkWidth(unittest.TestCase):
    def jobs(self, rows: int, versions: int, workers: int) -> int:
        width = chunk_width(rows, versions, workers)
        return -(-rows // width) * versions

    def test_five_versions_on_twelve_workers_split_into_about_one_job_per_worker(self) -> None:
        self.assertEqual(self.jobs(600, 5, 12), 15)
        self.assertEqual(chunk_width(600, 5, 12), 200)

    def test_a_single_version_uses_every_worker(self) -> None:
        self.assertEqual(self.jobs(600, 1, 12), 12)

    def test_more_versions_than_workers_stay_one_job_each(self) -> None:
        self.assertEqual(chunk_width(600, 5, 2), 600)

    def test_few_rows_never_make_a_zero_width_chunk(self) -> None:
        self.assertEqual(chunk_width(1, 1, 12), 1)
        self.assertEqual(chunk_width(3, 1, 12), 1)

    def test_no_versions_is_not_a_division_by_zero(self) -> None:
        self.assertEqual(chunk_width(4, 0, 4), 1)


if __name__ == "__main__":
    unittest.main()
