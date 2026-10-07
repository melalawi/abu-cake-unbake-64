"""Two same-unit/version comparisons must retain their own compiled bytes."""

import re
import struct
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from tests.project_fixture import ProjectCase
from unbake import process, runner
from unbake.compilers import drivers
from unbake.config import Held
from unbake.process import named
from unbake.work import compare


class CompileLifetimeTests(ProjectCase):
    def native(self, argv, work, phase, **kwargs):
        if "-E" in argv:
            source = Path(argv[-1])
            return (source if source.is_absolute() else self.project.root / source).read_text()
        value = int(re.search(r"return (\d+)", (work / "alpha.i").read_text())[1])
        (work / "alpha.o").write_bytes(struct.pack(">III", 0x24020000 | value, 0x03E00008, 0))
        return ""

    def test_concurrent_comparisons_of_distinct_sources_keep_their_own_exactness(self):
        files = []
        for value in (1, 2):
            path = self.root / str(value) / "alpha.c"
            path.parent.mkdir()
            path.write_text(f"int alpha(void) {{ return {value}; }}\n")
            files.append(path)
        together = threading.Barrier(2, timeout=2)
        objects = []

        def link(project, host, obj, version, row, file):
            objects.append(obj)
            together.wait()  # both compiles completed before either reads its object
            return obj.read_bytes(), []

        with (
            patch.object(process, "run_tool", side_effect=self.native),
            patch.object(
                drivers,
                "run_preprocess",
                side_effect=lambda project, argv, phase, **kw: self.native(argv, project.root, phase, **kw),
            ),
            patch.object(runner, "link_function", side_effect=link),
            ThreadPoolExecutor(max_workers=2) as workers,
        ):
            futures = [
                workers.submit(compare.measure, self.project, self.host, file, versions=("us",)) for file in files
            ]
            first, second = [future.result() for future in futures]
        self.assertTrue(first.exact)
        self.assertFalse(second.exact)
        self.assertNotEqual(objects[0], objects[1])
        self.assertEqual([obj.stem for obj in objects], ["alpha", "alpha"])
        self.assertTrue(all(not obj.exists() for obj in objects))

    def test_consumer_fault_releases_only_its_temporary_object_and_keeps_the_cas(self):
        file = self.root / "alpha.c"
        file.write_text("int alpha(void) { return 1; }\n")
        refusal = Held(
            named("link.undefined", "link.undefined: missing canonical symbol", owner="fixture", stage="link")
        )
        with (
            patch.object(process, "run_tool", side_effect=self.native),
            patch.object(
                drivers,
                "run_preprocess",
                side_effect=lambda project, argv, phase, **kw: self.native(argv, project.root, phase, **kw),
            ),
        ):
            with (
                self.assertRaises(Held) as caught,
                runner.compile_unit(self.project, self.host, file, "us", unit="alpha") as obj,
            ):
                expected = obj.read_bytes()
                raise refusal
            self.assertIs(caught.exception, refusal)
            self.assertFalse(obj.exists())
            with runner.compile_unit(self.project, self.host, file, "us", unit="alpha") as again:
                self.assertEqual(again.read_bytes(), expected)
                self.assertNotEqual(again, obj)
        self.assertFalse(again.exists())
