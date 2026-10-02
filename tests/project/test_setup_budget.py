"""Real make processes share CPU slots without stranding a version's work."""

import json
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from unbake.project import setup_proof
from unbake.project.config import Held


class SetupBudgetTests(unittest.TestCase):
    def test_shared_slots_bound_concurrent_makes_and_remain_available_after_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            counter = root / "counter.json"
            counter.write_text("[0,0]")
            worker = root / "worker.py"
            worker.write_text(
                "import fcntl, json, time\n"
                "from pathlib import Path\n"
                "def change(delta):\n"
                " with Path('counter.json').open('r+') as stream:\n"
                "  fcntl.flock(stream, fcntl.LOCK_EX)\n"
                "  current, peak = json.load(stream)\n"
                "  current += delta\n"
                "  stream.seek(0)\n"
                "  json.dump([current, max(current, peak)], stream)\n"
                "  stream.truncate()\n"
                "change(1)\n"
                "try: time.sleep(0.15)\n"
                "finally: change(-1)\n"
            )
            targets = " ".join(f"job{number}" for number in range(24))
            makefile = root / "Makefile"
            makefile.write_text(
                f".PHONY: all failure {targets}\nall: {targets}\n{targets}:\n"
                f"\t@{sys.executable} {worker.name}\nfailure:\n\t@exit 1\n"
            )
            with setup_proof.job_slots(12, 3) as slots:

                def run(target: str = "all") -> str:
                    return setup_proof.run(
                        ["make", "-f", str(makefile), target],
                        root,
                        environment=slots.environment,
                        descriptors=slots.descriptors,
                    )

                run()
                current, peak = json.loads(counter.read_text())
                self.assertEqual(current, 0)
                self.assertGreater(peak, 4, "a single version must borrow idle CPU slots")
                self.assertLessEqual(peak, 12)
                counter.write_text("[0,0]")
                with ThreadPoolExecutor(max_workers=3) as executor:
                    futures = [executor.submit(run) for _ in range(3)]
                    for future in futures:
                        future.result()
                current, peak = json.loads(counter.read_text())
                self.assertEqual(current, 0)
                self.assertGreater(peak, 4)
                self.assertLessEqual(peak, 12)
                with self.assertRaises(Held):
                    run("failure")
                counter.write_text("[0,0]")
                run()
                current, peak = json.loads(counter.read_text())
                self.assertEqual(current, 0)
                self.assertGreater(peak, 4)
                self.assertLessEqual(peak, 12)
