"""Publication catalogues actual private/project providers without moving their homes."""

import copy
from collections import Counter
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from tests.project_fixture import ProjectCase
from unbake.config import Held
from unbake.layout import apply, index, map

FIXTURE = Path(__file__).parents[1] / "fixtures/fold_provider_paths"
FUNCTION = "func_800B1520_us"
HEADER = "span_1000/code_800F45C8.h"


class FoldProviderPathTests(ProjectCase):
    versions = ("us",)

    def setUp(self):
        super().setUp()
        self.shared = self.project.include[0]
        self.private = self.project.work / FUNCTION / "include"
        self.private.mkdir(parents=True)
        self.project = replace(self.project, work_include=(self.private,))
        self.original = (FIXTURE / "code_800F45C8.h").read_text()
        self.authored = (FIXTURE / "menu_render.h").read_text()
        old = next(line for line in self.original.splitlines() if line.startswith("extern int func_800F4DFC"))
        new = next(line for line in self.authored.splitlines() if line.startswith("int func_800F4DFC"))
        self.staged = self.original.replace(old, new)
        self.source = (FIXTURE / f"{FUNCTION}.c").read_text()
        self.home = self.shared / HEADER
        self.home.parent.mkdir(parents=True)
        self.home.write_text(self.original)
        (self.shared / "menu_render.h").write_text(self.authored)
        (self.shared / "gfx.h").write_text("typedef struct Gfx { unsigned int words[2]; } Gfx;\n")
        self.lookup = {
            "schema": 1,
            "symbols": {"func_800F4DFC": HEADER},
            "clusters": {},
            "headers": {HEADER: "a" * 64},
        }
        self.ownership = map.Map(32, (map.Group("code_800ABFD0", "span_1000", "default", (FUNCTION,)),))

    def rewrite(self, outputs):
        return apply.source(self.project, self.source, FUNCTION, outputs, ownership=self.ownership, previous=set())

    def test_real_project_provider_and_authored_payload_keep_paths_and_bounded_work(self):
        payload = self.root / "authored-payload.h"
        payload.write_text(self.authored)
        outputs = {self.home: self.staged.encode(), self.shared / "menu_render.h": payload}
        before_outputs = dict(outputs)
        before_lookup = copy.deepcopy(self.lookup)
        before_files = {p: p.read_bytes() for p in self.project.root.rglob("*") if p.is_file()}
        reads = Counter()
        text_reads = Counter()
        bytes_read = Counter()
        read_bytes = Path.read_bytes
        read_text = Path.read_text

        def read(path):
            reads[path] += 1
            data = read_bytes(path)
            bytes_read[path] += len(data)
            return data

        def read_body(path, *args, **kwargs):
            text_reads[path] += 1
            data = read_text(path, *args, **kwargs)
            bytes_read[path] += len(data.encode())
            return data

        with (
            patch.object(index, "load", return_value=self.lookup) as load,
            patch.object(index, "overlay", wraps=index.overlay) as overlay,
            patch.object(apply, "imported", wraps=apply.imported) as imported,
            patch.object(Path, "read_bytes", read),
            patch.object(Path, "read_text", read_body),
            patch.object(Path, "rglob", side_effect=AssertionError("no tree scans")) as scans,
            patch.object(Path, "write_bytes", side_effect=AssertionError("no writes")) as write_bytes,
            patch.object(Path, "write_text", side_effect=AssertionError("no writes")) as write_text,
            patch("subprocess.run", side_effect=AssertionError("no external tools")) as tools,
        ):
            result = self.rewrite(outputs)
        self.assertEqual(load.call_count, 1)
        self.assertEqual(overlay.call_count, 1)
        self.assertEqual(overlay.call_args.args[1], {HEADER: self.staged, "menu_render.h": self.authored})
        self.assertEqual(imported.call_count, 1)
        self.assertEqual(imported.call_args.args[1:], (self.project.include, outputs))
        self.assertEqual(reads, {payload: 1})
        self.assertEqual(text_reads, {payload: 1, self.shared / "types.h": 1, self.shared / "gfx.h": 1})
        self.assertEqual(
            bytes_read,
            {
                payload: 2 * len(self.authored.encode()),
                self.shared / "types.h": len(before_files[self.shared / "types.h"]),
                self.shared / "gfx.h": len(before_files[self.shared / "gfx.h"]),
            },
        )
        for operation in (scans, write_bytes, write_text, tools):
            operation.assert_not_called()
        self.assertIn('#include "menu_render.h"', result)
        self.assertIn(f'#include "{HEADER}"', result)
        self.assertEqual(result[result.index("void ") :], self.source[self.source.index("void ") :])
        self.assertEqual(outputs, before_outputs)
        self.assertEqual(self.lookup, before_lookup)
        self.assertEqual({p: p.read_bytes() for p in self.project.root.rglob("*") if p.is_file()}, before_files)
        self.assertFalse((self.private / HEADER).exists())
        self.assertFalse(index.path(self.project).exists())

    def test_another_root_shape_uses_second_work_root_and_path_payload(self):
        overlay_root = self.root / "overlay headers"
        overlay_root.mkdir()
        self.project = replace(self.project, work_include=(self.private, overlay_root))
        home = overlay_root / HEADER
        payload = self.root / "rendered.h"
        payload.write_text(self.staged)
        with (
            patch.object(index, "load", return_value=self.lookup),
            patch.object(index, "overlay", wraps=index.overlay) as overlay,
        ):
            result = self.rewrite({home: payload})
        self.assertEqual(overlay.call_args.args[1], {HEADER: self.staged})
        self.assertIn(f'#include "{HEADER}"', result)
        self.assertFalse(home.exists())
        self.assertEqual(self.home.read_text(), self.original)

    def test_private_provider_wins_regardless_of_output_order(self):
        home = self.private / HEADER
        for outputs in (
            {self.home: self.original.encode(), home: self.staged.encode()},
            {home: self.staged.encode(), self.home: self.original.encode()},
        ):
            with (
                self.subTest(paths=list(outputs)),
                patch.object(index, "load", return_value=self.lookup),
                patch.object(index, "overlay", wraps=index.overlay) as overlay,
            ):
                self.rewrite(outputs)
            self.assertEqual(overlay.call_args.args[1], {HEADER: self.staged})
            self.assertFalse(home.exists())

    def test_unstaged_private_provider_shadows_project_edit_without_reading_payload(self):
        home = self.private / HEADER
        home.parent.mkdir(parents=True)
        home.write_text(self.staged)
        payload = self.root / "must-not-be-read.h"
        with (
            patch.object(index, "load", return_value=self.lookup),
            patch.object(index, "overlay", wraps=index.overlay) as overlay,
        ):
            self.rewrite({self.home: payload})
        self.assertEqual(overlay.call_args.args[1], {HEADER: self.staged})
        self.assertFalse(payload.exists())

    def test_private_link_uses_staged_project_bytes(self):
        home = self.private / HEADER
        home.parent.mkdir(parents=True)
        home.symlink_to(self.home)
        with (
            patch.object(index, "load", return_value=self.lookup),
            patch.object(index, "overlay", wraps=index.overlay) as overlay,
        ):
            result = self.rewrite({self.home: self.staged.encode()})
        self.assertEqual(overlay.call_args.args[1], {HEADER: self.staged})
        self.assertIn(f'#include "{HEADER}"', result)
        self.assertTrue(home.is_symlink())
        self.assertEqual(self.home.read_text(), self.original)

    def test_provider_outside_every_root_refuses(self):
        outside = self.root / "foreign/contract.h"
        with (
            patch.object(index, "load", return_value=self.lookup),
            self.assertRaisesRegex(Held, "layout.provider:.*outside"),
        ):
            self.rewrite({outside: self.staged.encode()})
        self.assertFalse(outside.exists())

    def test_shared_provider_does_not_suppress_local_contract_conflict(self):
        self.source = "extern float func_800F4DFC(void);\n" + self.source
        with (
            patch.object(index, "load", return_value=self.lookup),
            self.assertRaisesRegex(Held, "layout.redeclaration.func_800F4DFC"),
        ):
            self.rewrite({self.home: self.staged.encode()})

    def test_missing_staged_payload_still_refuses(self):
        payload = self.root / "missing.h"
        with patch.object(index, "load", return_value=self.lookup), self.assertRaises(FileNotFoundError):
            self.rewrite({self.home: payload})
