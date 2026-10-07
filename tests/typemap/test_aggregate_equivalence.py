"""Small route 4 payload: the published Acmd signature survives proven expansion."""

from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase

from tests.project_fixture import ProjectCase
from unbake.config import Held
from unbake.layout import redeclarations
from unbake.project.setup import TEMPLATES
from unbake.typemap import database, declaration_evidence, regeneration
from unbake.typemap.header_names import type_identity

FUNCTION = "func_802BE514_de"
PUBLISHED = "extern Acmd *func_802BE514_de(void *filter, s16 *outp, s32 outCount, s32 sampleOffset, Acmd *p);"
AGGREGATE = "union { struct { unsigned int w0; unsigned int w1; } words; long long int force_union_align; }"
PROVEN = (
    AGGREGATE
    + " * func_802BE514_de(void * filter, signed short * outp, int outCount, int sampleOffset, "
    + AGGREGATE
    + " * p);"
)
SCALARS = "typedef signed short s16; typedef int s32;"


class AggregateIdentityTests(TestCase):
    def test_real_acmd_anonymous_typedef_and_proven_expansion_agree(self):
        aliases = redeclarations.aliases([SCALARS, (TEMPLATES / "acmd.h").read_text()])
        self.assertTrue(redeclarations.equivalent(PUBLISHED, PROVEN, aliases))
        self.assertEqual(type_identity("Acmd *", aliases), type_identity(AGGREGATE + " *", aliases))

    def test_tagged_acmd_and_nested_tag_aliases_agree_by_structure(self):
        context = (
            "typedef struct Awords {unsigned int w0; unsigned int w1;} Awords;"
            "typedef union Acmd {Awords words; long long force_union_align;} Acmd;"
        )
        mapping = redeclarations.aliases([SCALARS, context])
        self.assertTrue(redeclarations.equivalent(PUBLISHED, PROVEN, mapping))
        # Complete inline definitions override the context, even under the same tag.
        self.assertFalse(redeclarations.equivalent("extern union Acmd *p;", "extern union Acmd {int x;} *p;", mapping))

    def test_anonymous_layouts_do_not_share_a_null_identity(self):
        for changed in (
            AGGREGATE.replace("unsigned int w1", "unsigned short w1"),
            AGGREGATE.replace("w1;", "w1[2];"),
            AGGREGATE.replace("w1;", "w1 : 4;"),
            AGGREGATE.replace("union", "struct", 1),
        ):
            with self.subTest(changed=changed):
                self.assertNotEqual(type_identity(AGGREGATE, {}), type_identity(changed, {}))

    def test_typedef_chains_callbacks_arrays_and_primitive_synonyms(self):
        mapping = redeclarations.aliases(
            ["typedef unsigned int Word; typedef struct {Word items[2];} Payload; typedef Payload Packet;"]
        )
        self.assertTrue(
            redeclarations.equivalent(
                "extern Packet *call(Packet values[3], int (*notify)(Packet *));",
                "extern struct {unsigned items[2];} *call(struct {unsigned int items[2];} *p, "
                "signed int (*cb)(struct {unsigned int items[2];} *));",
                mapping,
            )
        )
        self.assertTrue(redeclarations.equivalent("int f();", "extern signed int f(void);", {}))
        self.assertFalse(redeclarations.equivalent("int f();", "int f(int);", {}))

    def test_recursive_tags_terminate_without_collapsing_distinct_layouts(self):
        mapping = redeclarations.aliases(["typedef struct Node Node; struct Node {int value; Node *next;};"])
        self.assertTrue(redeclarations.equivalent("extern Node *head;", "extern struct Node *head;", mapping))
        self.assertFalse(
            redeclarations.equivalent(
                "extern Node *head;", "extern struct Node {short value; Node *next;} *head;", mapping
            )
        )

    def test_validation_names_both_real_contradictions(self):
        project = SimpleNamespace(include=(Path("/include"),))
        for changed in (
            PROVEN.replace("unsigned int w1", "unsigned short w1"),
            PROVEN.replace("int outCount", "float outCount"),
        ):
            with (
                self.subTest(changed=changed),
                self.assertRaisesRegex(
                    Held,
                    r"(?s)headers.declaration: func_802BE514_de: published.*Acmd.*incompatible with proven.*"
                    + FUNCTION,
                ),
            ):
                declaration_evidence.validate_published(
                    project,
                    {
                        "functions": {
                            FUNCTION: {"state": "known", "prototype": changed, "provenance": {"kind": "proven"}}
                        }
                    },
                    {"audio.h": PUBLISHED},
                    context=(SCALARS, (TEMPLATES / "acmd.h").read_text()),
                )


class PublishedAcmdRenderingTests(ProjectCase):
    def test_render_keeps_the_published_name_and_does_not_write_live_headers(self):
        include = self.project.include[0]
        (include / "acmd.h").write_text((TEMPLATES / "acmd.h").read_text())
        (include / "types.h").write_text(SCALARS)
        published = {"audio.h": PUBLISHED}
        value = {kind: {} for kind in ("structs", "functions", "globals", "arrays")}
        value["functions"][FUNCTION] = {"state": "known", "prototype": PROVEN, "provenance": {"kind": "proven"}}
        value["published_declarations"] = published.copy()
        before = {path: path.read_bytes() for path in include.glob("*.h")}
        outputs = database._render(self.project, value, None, regeneration.Session(self.project, None))
        bodies = "\n".join(data.decode() for path, data in outputs.items() if path.suffix == ".h")
        self.assertIn(PUBLISHED, bodies)
        self.assertNotIn(PROVEN, bodies)
        self.assertEqual(value["published_declarations"], published)
        self.assertEqual({path: path.read_bytes() for path in before}, before)
