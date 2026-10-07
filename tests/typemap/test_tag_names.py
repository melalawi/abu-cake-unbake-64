"""Route 3 RageWars tag spellings must not own ordinary typedef names."""

from pathlib import Path

from tests.project_fixture import ProjectCase
from unbake.typemap import database, header_names

NAMES = ("Vec3", "func_8021C9B4_S3", "func_80293B0C_S1", "State_func_8044D054_de")


class TagNamesTests(ProjectCase):
    def test_authored_forward_and_complete_tags_are_separate_from_typedefs(self):
        for name in NAMES:
            for kind, body in (("struct", "float x; float y; float z;"), ("union", "int word;"), ("enum", "READY")):
                for suffix in (";", " {" + body + "};"):
                    with self.subTest(name=name, kind=kind, suffix=suffix):
                        source = kind + " " + name + suffix
                        parser = header_names._Declarations(source)
                        parsed = parser.parse()
                        ordinary, tags = header_names._header_owned((Path("forward.h"), source))
                        self.assertEqual(parser.names, set())
                        self.assertEqual(ordinary, [])
                        self.assertEqual(parsed.typedefs, set())
                        self.assertEqual(tags, [name] if "{" in suffix else [])

    def test_source_and_include_ownership_keeps_both_namespaces(self):
        source = self.project.src / "alpha.c"
        header = self.project.include[0] / "forward.h"
        header.write_text("struct Vec3; union Packet {int word;}; enum Mode {READY};\n")
        source.write_text('#include "forward.h"\nstruct Vec3; void alpha(void) {}\n')
        consumers, tags = {}, {}
        reserved = header_names.source_names(
            self.project, self.project.include[0] / "common/types.h", None, consumers=consumers, consumer_tags=tags
        )
        self.assertEqual(reserved, set())
        self.assertEqual(consumers[source], set())
        self.assertEqual(tags[source], {"Packet", "Mode"})
        header.write_text("typedef struct Vec3 Vec3; extern int global;\n")
        header_names.source_names(self.project, header, None, consumers=consumers, consumer_tags=tags)
        self.assertEqual(consumers[source], {"Vec3", "global"})

    def test_real_source_local_typedef_still_blocks_the_shared_alias(self):
        source = self.project.src / "alpha.c"
        for kind in ("struct", "union", "enum"):
            with self.subTest(kind=kind):
                text = f"typedef {kind} Vec3 Vec3; void alpha(void) {{}}\n"
                owned, _ = header_names._owned((self.project, None, source, text))
                self.assertEqual(owned, ["Vec3"])
                self.assertNotIn("typedef", header_names.rewrite("typedef int Vec3;", {}, set(owned)))

    def test_private_typedef_conflicts_do_not_claim_same_spelled_tags(self):
        for kind in ("struct", "union", "enum"):
            with self.subTest(kind=kind):
                tagged = f"extern {kind} Vec3 *point;"
                self.assertFalse(database.source_private(tagged, set(), {"Vec3"}))
                self.assertTrue(database.source_private(tagged, {"Vec3"}, set()))
        self.assertTrue(database.source_private("extern Vec3 *point;", set(), {"Vec3"}))
        self.assertFalse(database.source_private("extern Vec3 *point;", {"Vec3"}, set()))
