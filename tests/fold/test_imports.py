"""Imported sources use installed split and SDK providers, without body edits."""

from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from tests.project_fixture import ProjectCase
from unbake.fold import declarations, imports
from unbake.layout.header_context import Headers


class ImportTests(TestCase):
    def test_consumer_prerequisites(self):
        root = Path("/project/include")
        bodies = {
            "types.h": "typedef unsigned int u32;\n",
            "shared/types/record.h": "typedef struct Record { int value; } Record;\n",
            "shared/record.h": "typedef struct Record { int value; } Record;\ntypedef int Unrelated;\n",
            "shared/types/gfx.h": "typedef struct Gfx { u32 word; } Gfx;\n",
            "shared/decls/global.h": '#include "shared/types/record.h"\nextern Record global;\n',
            "shared/decls/call.h": '#include "shared/types/record.h"\nextern int call(Record *);\n',
            "gbi.h": "#define GBI_WRITE(p) (((Gfx *)(p))->word = (u32) 1)\n",
            "sdk.h": "enum Mode { SDK_MODE = 3 };\n#define SDK_VALUE 4\n",
            "sdk_type.h": "typedef struct SDKRecord {u32 value;} SDKRecord;\n",
            "sdk_call.h": "void SDKCall(SDKRecord *);\n",
            "shared/types/tag.h": "struct Canon {int value;};\n",
            "shared/types/alias.h": "typedef struct Canon Alias;\n",
            "shared/consumers/compat_Compat.h": "typedef int Compat;\n",
            "shared/consumers/other.h": '#include "shared/record.h"\n',
        }
        context = SimpleNamespace(texts={root / p: s for p, s in bodies.items()})
        project = SimpleNamespace(include=(root,), build=Path("/project/build"))
        old_lookup = {
            "headers": {name: "a" * 64 for name in ("shared/old/session.h", "shared/typemap.h", "shared/old.h")}
        }
        mock_index = patch("unbake.layout.index.load", return_value=old_lookup)
        mock_index.start()
        self.addCleanup(mock_index.stop)
        cases = (
            (
                "missing old header",
                '#include "shared/old/session.h"\n',
                "int alpha(void) {return call(&global);}\n",
                {"shared/decls/call.h", "shared/decls/global.h", "shared/types/record.h"},
            ),
            (
                "umbrella",
                '#include "shared/typemap.h"\n',
                "int alpha(Record *p) {return p->value;}\n",
                {"shared/types/record.h"},
            ),
            (
                "no imports",
                "",
                "int alpha(Record *p) {return call(p);}\n",
                {"shared/types/record.h", "shared/decls/call.h"},
            ),
            ("SDK macro", "", "void alpha(void *p) {GBI_WRITE(p);}\n", {"gbi.h", "shared/types/gfx.h", "types.h"}),
            ("SDK enum", "", "int alpha(void) {return SDK_MODE + SDK_VALUE;}\n", {"sdk.h"}),
            (
                "local ownership",
                "",
                "typedef int Record;\nextern int global;\nint alpha(Record p) {return global+p;}\n",
                set(),
            ),
            ("existing wrapper", '#include "shared/record.h"\n', "int alpha(Record *p) {return p->value;}\n", set()),
            ("missing foreign header", '#include "unknown.h"\n', "int alpha(void) {return 1;}\n", set()),
            ("unknown live type", '#include "shared/old.h"\n', "NewType alpha(NewType p) {return p;}\n", set()),
            ("macro condition", "", "#if SDK_VALUE\nint alpha(void) {return 1;}\n#endif\n", {"sdk.h"}),
        )
        cases += (
            (
                "alias body closure",
                "",
                "int alpha(Alias *p) {return p->value;}\n",
                {"shared/types/tag.h", "shared/types/alias.h"},
            ),
            (
                "consumer compatibility provider",
                "",
                "Compat alpha(Compat p) {return p;}\n",
                {"shared/consumers/compat_Compat.h"},
            ),
            ("SDK type dependency", "", "int alpha(SDKRecord *p) {return p->value;}\n", {"sdk_type.h", "types.h"}),
            (
                "plain SDK prototype",
                "",
                "void alpha(SDKRecord *p) {SDKCall(p);}\n",
                {"sdk_type.h", "sdk_call.h", "types.h"},
            ),
            ("local function declaration", "", "void SDKCall(void);\nvoid alpha(void) {SDKCall();}\n", set()),
            ("literal names", "", 'char *alpha(void) {return "SDK_VALUE Record";}\n', set()),
            ("comment directive", "", 'void alpha(void) { /*\n#include "shared/old.h"\n*/ }\n', set()),
        )
        for label, prefix, body, expected in cases:
            with self.subTest(label=label):
                result = imports.resolve(project, context, prefix + body, "alpha")
                added = set(imports._INCLUDE.findall(imports._without_comments(result))) - set(
                    imports._INCLUDE.findall(imports._without_comments(prefix))
                )
                self.assertEqual(added, expected)
                self.assertTrue(result.endswith(body))
                if label != "comment directive":
                    self.assertNotIn("shared/old", result)
                self.assertEqual(imports.resolve(project, context, result, "alpha"), result)
                if "sdk_type.h" in expected:
                    self.assertLess(result.index("types.h"), result.index("sdk_type.h"))
                if "gbi.h" in expected:
                    self.assertLess(result.index("shared/types/gfx.h"), result.index("gbi.h"))
                    self.assertLess(result.index("types.h"), result.index("gbi.h"))

    def test_sdk_configuration_precedes_recovered_macros(self):
        root = Path("/project/include")
        project = SimpleNamespace(include=(root,), build=Path("/project/build"))
        old_lookup = {
            "headers": {name: "a" * 64 for name in ("shared/old/session.h", "shared/typemap.h", "shared/old.h")}
        }
        mock_index = patch("unbake.layout.index.load", return_value=old_lookup)
        mock_index.start()
        self.addCleanup(mock_index.stop)
        context = SimpleNamespace(texts={root / "gbi.h": "#define GBI_WRITE(p) ((p)->word = 1)\n"})
        for directive in ("#define F3DEX_GBI_2\n", "#define F3DEX_GBI_2 \\\n1\n"):
            with self.subTest(directive=directive):
                body = "void alpha(void *p) {GBI_WRITE(p);}\n"
                source = directive + '#include "shared/old.h"\n' + body
                result = imports.resolve(project, context, source, "alpha")
                self.assertTrue(result.startswith(directive))
                self.assertTrue(result.endswith(body))
                self.assertIn('#include "gbi.h"', result)
                self.assertEqual(imports.resolve(project, context, result, "alpha"), result)

    def test_current_overlay_is_the_provider_and_cache_changes_with_edits(self):
        root = Path("/project/include")
        project = SimpleNamespace(include=(root,), build=Path("/project/build"))
        old_lookup = {
            "headers": {name: "a" * 64 for name in ("shared/old/session.h", "shared/typemap.h", "shared/old.h")}
        }
        mock_index = patch("unbake.layout.index.load", return_value=old_lookup)
        mock_index.start()
        self.addCleanup(mock_index.stop)
        header = root / "shared/types/current.h"
        for name in ("Before", "After"):
            with self.subTest(name=name):
                context = SimpleNamespace(texts={header: f"typedef int {name};\n"})
                body = f"int alpha({name} p) {{return p;}}\n"
                result = imports.resolve(project, context, body, "alpha")
                self.assertIn('#include "shared/types/current.h"', result)
                self.assertTrue(result.endswith(body))


class ImportFoldTests(ProjectCase):
    def test_try_and_submit_fold_share_import_resolution(self):
        header = self.project.include[0] / "canonical.h"
        header.write_text("typedef struct Record {int value;} Record;\n")
        body = "int alpha(Record *p) {return p->value;}\n"
        from unbake.layout import index

        index.update(self.project, {self.project.include[0] / "shared/old_only.h": ""})
        source = '#include "shared/old_only.h"\n' + body
        with patch.object(imports, "resolve", wraps=imports.resolve) as resolver:
            result = declarations.fold_source(
                self.project,
                self.host,
                Headers.read(self.project),
                "alpha",
                source,
                self.versions,
                prove_headers=False,
            )
        self.assertEqual(resolver.call_count, 2)
        self.assertTrue(result.source.endswith(body))
        self.assertNotIn("old_only", result.source)
        self.assertIn('#include "canonical.h"', result.source)
