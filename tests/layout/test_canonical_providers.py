"""Real folded consumers keep canonical imports despite an older layout index."""

import json
import re
import shutil
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tests.kit import TempCase
from unbake.cdecl import declarations
from unbake.config import Held
from unbake.fold import imports
from unbake.layout import apply, header_loss, index
from unbake.layout.headers import Layout
from unbake.layout.map import Group, Map
from unbake.typemap import declaration_evidence

FIXTURE = Path(__file__).parent / "fixtures/battletanx_providers"
CASES = (
    ("func_800E9894", "code_800E9200", "func_800A74AC_S3_Shared800A74AC", "code_800A6864"),
    ("func_800ED810", "code_800EC380", "func_800A2EA4_S1_Shared800A2EA4", "code_800A27D0"),
    ("func_800EEBEC", "code_800EDB20", "func_80091608_Status_Shared80091608", "code_8008F248"),
    ("func_80119C34", "code_80119B60", "func_8010DCD0_S2_Shared8010DCD0", "code_8010BFDC"),
    ("func_8011D100", "code_8011D0D0", "func_80078D90_S2_Shared80078D90", "code_80077930"),
)


class CanonicalProvidersTests(TempCase):
    def setUp(self):
        super().setUp()
        root = self.root / "project"
        shutil.copytree(FIXTURE / "include", root / "include")
        (root / "src").mkdir()
        self.project = SimpleNamespace(
            id="00000000-0000-4000-8000-000000000001",
            root=root,
            include=(root / "include",),
            src=root / "src",
            build=root / "build",
            cache=root / ".cache",
            work_include=(),
        )
        self.lookup = json.loads((FIXTURE / "index.json").read_text())
        self.contents = {path: path.read_text() for path in self.project.include[0].rglob("*.h")}

    def test_all_five_real_folded_consumers_keep_the_live_canonical_home(self):
        for unit, owner, canonical, provider in CASES:
            with self.subTest(unit=unit):
                home = f"span_1000/{provider}.h"
                original = (FIXTURE / "src" / f"{unit}.c").read_text()
                ownership = Map(32, (Group(owner, "span_1000", "default", (unit,)),))
                context = SimpleNamespace(texts=self.contents)
                with patch.object(index, "load", return_value=self.lookup):
                    resolved = imports.resolve(self.project, context, original, unit)
                    self.assertIn(f'#include "{home}"', resolved)
                    with patch.object(index, "overlay", wraps=index.overlay) as catalogue:
                        rewritten = apply.source(
                            self.project,
                            resolved,
                            unit,
                            {},
                            ownership=ownership,
                            previous=set(self.lookup["headers"]),
                        )
                self.assertIn(f'#include "{home}"', rewritten)
                changes = catalogue.call_args.args[1]
                self.assertIn(home, changes)
                self.assertLessEqual(len(changes), 6)
                self.assertEqual(catalogue.call_count, 1)
                # Include changes preserve every real function-body byte.
                self.assertEqual(rewritten[rewritten.index("{") :], original[original.index("{") :])
                bodies = apply.imported(rewritten, self.project.include, {})
                row = declarations("\n".join(bodies))
                self.assertIn(canonical, row.tags)
                if unit != "func_80119C34":
                    self.assertIn(canonical, row.typedefs)

    def test_import_insertion_keeps_existing_source_scope_and_retained_body(self):
        from unbake.work import attempts

        ownership = Map(32, (Group("owner", "main", "default", ("consumer",)),))
        lookup = {"headers": {}, "symbols": {}}
        for body in ('#include "authored.h"\nint consumer(void) {return 1;}\n', "int consumer(void) {return 1;}\n"):
            with self.subTest(body=body):
                before = attempts.guarded(body)
                after = apply.rewrite(Path("consumer.c"), before, "consumer", ownership, lookup, previous=set())
                retained = attempts.unguarded(after)
                self.assertIn('#include "main/owner.h"\n', retained)
                self.assertIn(body, retained)
                self.assertTrue(after.startswith(attempts.FUZZY_PREFIX))
                self.assertEqual(
                    apply.rewrite(
                        Path("consumer.c"),
                        after,
                        "consumer",
                        ownership,
                        {**lookup, "headers": {"main/owner.h": "hash"}},
                        previous=set(),
                    ),
                    after,
                )

    def test_staged_exports_replace_stale_symbols_and_import_the_separate_definition_home(self):
        old = {
            "schema": 1,
            "headers": {"main/alias.h": "a" * 64},
            "symbols": {"Gone": "main/alias.h"},
            "type_headers": {"Gone": ["main/alias.h"]},
            "clusters": {},
        }
        result = index.overlay(
            old, {"main/alias.h": "typedef struct Canon Alias;", "common/definition.h": "struct Canon { int field; };"}
        )
        self.assertNotIn("Gone", result["symbols"])
        self.assertNotIn("Gone", result["type_headers"])
        self.assertEqual(result["type_headers"]["Alias"], ["common/definition.h", "main/alias.h"])
        self.assertEqual(old["symbols"], {"Gone": "main/alias.h"})
        ownership = Map(32, (Group("owner", "main", "default", ("consumer",)),))
        rewritten = apply.rewrite(
            Path("consumer.c"),
            "int consumer(Alias *p) {return p->field;}",
            "consumer",
            ownership,
            result,
            previous=set(),
        )
        self.assertIn('#include "common/definition.h"', rewritten)
        self.assertIn('#include "main/alias.h"', rewritten)

    def test_staged_path_payloads_and_index_updates_export_complete_tags(self):
        include = self.project.include[0]
        alias, definition = include / "main/alias.h", include / "common/definition.h"
        changes = {alias: "typedef struct Canon Alias;", definition: "struct Canon {int field;};"}
        index.update(self.project, changes)
        result = index.load(self.project)
        self.assertEqual(result["type_headers"]["Alias"], ["common/definition.h", "main/alias.h"])
        self.assertEqual(result["type_headers"]["Canon"], ["common/definition.h"])
        staged = self.root / "staged.h"
        staged.write_text("struct New {int field;};")
        ownership = Map(32, (Group("owner", "main", "default", ("consumer",)),))
        text = apply.source(
            self.project,
            "int consumer(struct New *p) {return p->field;}",
            "consumer",
            {include / "main/new.h": staged},
            ownership=ownership,
            previous=set(),
        )
        self.assertIn('#include "main/new.h"', text)

    def test_real_conflicting_imports_still_refuse(self):
        include = self.project.include[0]
        (include / "conflict.h").write_text("extern float conflict(int arg);\n")
        ownership = Map(32, (Group("owner", "main", "default", ("consumer",)),))
        with self.assertRaisesRegex(Held, "conflict"):
            apply.source(
                self.project,
                '#include "conflict.h"\nextern int conflict(int arg);\nint consumer(void) {return conflict(0);}',
                "consumer",
                {},
                ownership=ownership,
                lookup=self.lookup,
                previous=set(),
            )

    def test_real_nested_pointer_contracts_and_aliases_remain_reachable(self):
        for unit, owner, nested in (
            ("func_800793D0", "code_80078E30", "func_800793D0_S2_Shared800793D0"),
            ("func_800EA1FC", "code_800E9B9C", "func_800E9894_S3_Shared800EA1FC"),
        ):
            with self.subTest(unit=unit):
                source = self.project.src / f"{unit}.c"
                source.write_bytes((FIXTURE / "src" / source.name).read_bytes())
                sources = {source: source.read_text()}
                home = self.project.include[0] / f"span_1000/{owner}.h"
                with patch.object(index, "headers", return_value={home}) as inventory:
                    components, homes = declaration_evidence.published_snapshot(self.project, sources=sources)
                self.assertEqual(inventory.call_count, 1)
                self.assertIn(nested, set().union(*(declarations(t).typedefs for t in components.values())))
                self.assertIn(nested, set().union(*(declarations(t).tags for t in components.values())))
                self.assertLessEqual(len(components), 5)
                scalar = self.project.include[0] / "types.h"
                components[scalar] = self.contents[scalar]
                ownership = Map(32, (Group(owner, "span_1000", "default", (unit,)),))
                layout = Layout(
                    components,
                    components,
                    self.project.include[0],
                    ownership=ownership,
                    sources=sources,
                    fixed_homes=homes,
                    authored={scalar},
                )
                text = apply.rewrite(
                    source, sources[source], unit, ownership, layout.index, previous=set(self.lookup["headers"])
                )
                outputs = {**layout.headers, source: text.encode()}
                header_loss.check(self.project, outputs)
                imported = declarations("\n".join(apply.imported(text, self.project.include, outputs)))
                self.assertIn(nested, imported.typedefs)
                self.assertIn(nested, imported.tags)
                # A surviving copy in an unrelated home cannot satisfy retention.
                alias = f"typedef struct {nested} {nested};"
                moved = {
                    p: re.sub(re.escape(alias), "", b.decode()).encode() if p.suffix == ".h" else b
                    for p, b in outputs.items()
                }
                moved[self.project.include[0] / "common/unreachable.h"] = alias.encode()
                with self.assertRaisesRegex(Held, "would lose reachable declarations.*" + nested):
                    header_loss.check(self.project, moved)
                source.unlink()
