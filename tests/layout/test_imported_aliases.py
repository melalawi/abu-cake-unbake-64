"""Real routed declarations keep foundational typedefs through the private/shared include view."""

from collections import Counter
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from tests.project_fixture import ProjectCase
from unbake import process
from unbake.config import Held
from unbake.fold import declarations
from unbake.layout import apply, map, redeclarations
from unbake.layout.header_context import Headers

FIXTURES = Path(__file__).parents[1] / "fixtures" / "imported_typedef_aliases"
ROUTES = (
    ("battletanx", "func_800B1520_us", "func_800B9C0C_us", "span_1000", "code_800ABFD0"),
    ("ragewars", "func_8042854C_de", "func_8042863C_de", "span_16E000", "code_804264F0"),
)


class ImportedAliasTests(ProjectCase):
    versions = ("de", "eu", "eu-x", "us", "us-rev1")

    def install(self, route, *, linked=True, owner=True):
        label, function, callee, segment, group = route
        shared = self.project.include[0]
        private = self.project.work / function / "include"
        private.mkdir(parents=True, exist_ok=True)
        (shared / "types.h").write_bytes((FIXTURES / "types.h").read_bytes())
        if linked:
            (private / "types.h").symlink_to(shared / "types.h")
        header = private / segment / f"{group}.h"
        header.parent.mkdir(parents=True, exist_ok=True)
        header.write_bytes((FIXTURES / f"{label}.h").read_bytes())
        ownership = map.Map(32, (map.Group(group if owner else "missing", segment, "default", (function,)),))
        (self.project.root / "layout.toml").write_bytes(map.encoded(ownership))
        project = replace(self.project, work_include=(private,))
        lookup = {"symbols": {callee: f"{segment}/{group}.h"}, "headers": {}, "type_headers": {}}
        return project, (FIXTURES / f"{label}.c").read_text(), ownership, lookup

    def test_layout_routes(self):
        for route in ROUTES:
            for linked in (False, True):
                with self.subTest(route=route[0], linked=linked):
                    # Each reduction has its own private view, including the link/no-link variants.
                    project, source, ownership, lookup = self.install(route, linked=False)
                    types = project.include[0] / "types.h"
                    if linked and not types.exists():
                        types.symlink_to(project.include[-1] / "types.h")
                    found = {}
                    result = apply.source(
                        project,
                        source,
                        route[1],
                        {},
                        ownership=ownership,
                        lookup=lookup,
                        previous=set(),
                        disagreements=found,
                    )
                    self.assertEqual(found, {})
                    self.assertEqual(
                        result[result.index("void " + route[1]) :], source[source.index("void " + route[1]) :]
                    )
                    self.assertNotIn(route[2], result[: result.index("void " + route[1])])

    def test_fold_fallback_preserves_all_five_versions_without_builds(self):
        project, source, ownership, _ = self.install(ROUTES[1], owner=False)
        with (
            patch.object(map, "load", return_value=ownership),
            patch.object(declarations.entries, "owners", return_value=[]) as owners,
            patch.object(
                process.subprocess, "run", side_effect=AssertionError("declaration folding starts no tools")
            ) as run,
            patch.object(apply, "imported", wraps=apply.imported) as imported,
        ):
            folded = declarations.fold_source(
                project,
                self.host,
                Headers.read(project),
                ROUTES[1][1],
                source,
                self.versions,
                prove_headers=False,
            )
        self.assertEqual(owners.call_count, 5)
        self.assertEqual(imported.call_count, 1)
        self.assertEqual(imported.call_args.args[1], project.include)
        run.assert_not_called()
        self.assertEqual(folded.headers, [])
        self.assertEqual(folded.removed_rows, {})
        self.assertEqual(folded.source[folded.source.index("void ") :], source[source.index("void ") :])
        self.assertNotIn("extern s32", folded.source)

    def test_linked_typedef_is_read_once_with_repeated_imports(self):
        project, source, _, _ = self.install(ROUTES[0])
        read_text = Path.read_text
        reads = Counter()

        def read(path, *args, **kwargs):
            reads[path.resolve()] += 1
            return read_text(path, *args, **kwargs)

        with patch.object(Path, "read_text", read):
            bodies = apply.imported(source + source, project.include, {})
        self.assertEqual(len(bodies), 2)
        self.assertEqual(sorted(reads.values()), [1, 1])
        self.assertEqual(redeclarations.aliases(bodies)["s32"], "int")
        self.assertEqual(redeclarations.strip(source, bodies).count("func_800B9C0C_us"), 1)

    def test_real_conflicting_abis_still_refuse(self):
        project, source, ownership, lookup = self.install(ROUTES[1])
        original = "extern int func_8042863C_de(int id);"
        header = project.include[0] / "span_16E000/code_804264F0.h"
        for prototype in (
            "extern unsigned int func_8042863C_de(int id);",
            "extern short func_8042863C_de(int id);",
            "extern float func_8042863C_de(int id);",
            "extern int func_8042863C_de(unsigned int id);",
            "extern int func_8042863C_de(void *id);",
            "extern int func_8042863C_de(int id, int extra);",
            "extern int func_8042863C_de(int id, ...);",
        ):
            with (
                self.subTest(prototype=prototype),
                self.assertRaisesRegex(Held, "layout.redeclaration.func_8042863C_de"),
            ):
                apply.source(
                    project,
                    source,
                    ROUTES[1][1],
                    {header: header.read_text().replace(original, prototype).encode()},
                    ownership=ownership,
                    lookup=lookup,
                    previous=set(),
                )
        self.assertIn(original, header.read_text())

    def test_argument_pointer_conflict_still_refuses(self):
        project, source, ownership, lookup = self.install(ROUTES[0])
        with self.assertRaisesRegex(Held, "layout.redeclaration.func_800B9C0C_us"):
            apply.source(
                project,
                source.replace("(void *);", "(int *);"),
                ROUTES[0][1],
                {},
                ownership=ownership,
                lookup=lookup,
                previous=set(),
            )

    def test_route6_real_return_conflict_still_refuses(self):
        types = (FIXTURES / "types.h").read_text()
        local = (FIXTURES / "battletanx_return_conflict.c").read_text()
        shared = (FIXTURES / "battletanx_return_conflict.h").read_text()
        with self.assertRaisesRegex(Held, "layout.redeclaration.func_800B1264_us"):
            redeclarations.strip(local, [types, shared])

    def test_staged_typedef_bytes_and_files_override_shared_disk(self):
        project, source, _, _ = self.install(ROUTES[0])
        shared = project.include[-1] / "types.h"
        conflicting = shared.read_bytes().replace(b"signed int s32", b"unsigned int s32")
        staged = self.root / "staged-types.h"
        staged.write_bytes(conflicting)
        for output in (conflicting, staged):
            with self.subTest(kind=type(output).__name__):
                bodies = apply.imported(source, project.include, {shared: output})
                self.assertEqual(redeclarations.aliases(bodies)["s32"], "unsigned int")
                with self.assertRaisesRegex(Held, "layout.redeclaration.func_800B9C0C_us"):
                    redeclarations.strip(source, bodies)

    def test_private_typedef_shadows_shared_for_root_imports(self):
        project, source, _, _ = self.install(ROUTES[0], linked=False)
        private = project.include[0] / "types.h"
        staged = (FIXTURES / "types.h").read_bytes().replace(b"signed int s32", b"unsigned int s32")
        bodies = apply.imported(source, project.include, {private: staged})
        self.assertEqual(len(bodies), 2)
        self.assertEqual(redeclarations.aliases(bodies)["s32"], "unsigned int")

    def test_outside_include_roots_does_not_supply_aliases(self):
        project, source, _, _ = self.install(ROUTES[0], linked=False)
        outside = self.root / "outside.h"
        outside.write_bytes((FIXTURES / "types.h").read_bytes())
        (project.include[0] / "types.h").symlink_to(outside)
        # With only the private root authorized, an unrelated symlink target is never imported.
        bodies = apply.imported(source, project.include[0], {})
        self.assertNotIn("s32", redeclarations.aliases(bodies))
        with self.assertRaisesRegex(Held, "layout.redeclaration.func_800B9C0C_us"):
            redeclarations.strip(source, bodies)

    def test_all_foundational_aliases_reconcile_without_name_exceptions(self):
        project, _, _, _ = self.install(ROUTES[0])
        types = apply.imported('#include "types.h"\n', project.include, {})
        mapping = redeclarations.aliases(types)
        self.assertEqual(len(mapping), 10)
        for name, target in mapping.items():
            with self.subTest(alias=name):
                local = f"extern {name} value_{name};\n"
                shared = f"extern {target} value_{name};\n"
                self.assertEqual(redeclarations.strip(local, [*types, shared]).strip(), "")
