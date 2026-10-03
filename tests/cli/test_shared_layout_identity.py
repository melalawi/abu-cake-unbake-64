"""Installed CLI publication resolves complete authored layouts independently of tags."""

import json
import unittest

from tests.cli import test_publication_boundary as fixture
from unbake.layout.structs_parser import Parser


class SharedLayoutIdentityCliTests(unittest.TestCase):
    names = ("alpha", "beta", "gamma", "delta")
    setUp = fixture.PublicationBoundaryCliTests.setUp
    write_policy = fixture.PublicationBoundaryCliTests.write_policy
    cli = fixture.PublicationBoundaryCliTests.cli
    make = fixture.PublicationBoundaryCliTests.make

    def test_colliding_and_equal_layouts_publish_in_one_batch(self):
        for source, tag, field in zip(
            self.sources, ("Owner", "Owner", "Owner", "Other"), ("int", "short", "int", "short"), strict=True
        ):
            member = "other" if tag == "Other" else "value"
            link = "link" if tag == "Other" else "next"
            source.write_text(
                f"/* Keep the spelling {tag} in commentary. */\n"
                f"typedef struct {tag} {{ {field} {member}; struct {tag} *{link}; }} {tag};\n"
                f"int {source.stem}(void) {{ return sizeof((({tag} *)0)->{link}->{member}) / "
                f"sizeof((({tag} *)0)->{member}); }}\n"
            )
            self.cli("try", source)
        output = self.cli("submit", "--batch", *self.sources)
        self.assertIn("rename Owner -> Owner_", output)
        self.assertIn("rename Other -> Owner_", output)
        headers = sorted((self.root / "include/shared").glob("*.h"))
        records = [record for path in headers for record in Parser(path.read_text()).parse() if record.fields]
        owners = [record for record in records if record.name == "Owner" or record.name.startswith("Owner_")]
        self.assertEqual(len(owners), 2)
        renamed = next(record.name for record in owners if record.name != "Owner")
        published = [(self.project.src / source.name).read_text() for source in self.sources]
        self.assertIn("((Owner *)0)->next->value", published[0])
        self.assertIn(f"(({renamed} *)0)->next->value", published[1])
        self.assertIn("((Owner *)0)->next->value", published[2])
        self.assertIn(f"(({renamed} *)0)->next->value", published[3])
        self.assertIn("spelling Other in commentary", published[3])
        for text in published:
            self.assertNotIn("typedef", text)
            self.assertNotIn("{ short value", text)
            self.assertNotIn("{ int value", text)
        for record in owners:
            self.assertEqual(record.fields[1].type, f"struct {record.name} *")
        events = [
            json.loads(line)
            for path in (self.root / ".unbake/state/publications").glob("*.jsonl")
            for line in path.read_text().splitlines()
        ]
        proofs = [event for event in events if event["event"] == "proof"]
        self.assertEqual(len(proofs), 1)
        self.assertEqual(len(proofs[0]["sources"]), 4)
        self.assertIn(": OK", self.make())

    def test_reused_by_value_type_uses_existing_shared_tag_without_adding_aliases(self):
        shared = self.project.include[0] / "shared/established.h"
        shared.write_text("#ifndef ESTABLISHED_H\n#define ESTABLISHED_H\nstruct Established { int value; };\n#endif\n")
        source = self.sources[0]
        source.write_text(
            "typedef struct Queue {int field;} Queue;\n"
            "typedef struct Holder {Queue queue; int count;} Holder;\n"
            "int alpha(void) {return sizeof(Holder) / sizeof(Holder);}\n"
        )
        self.cli("try", source)
        output = self.cli("submit", source)
        self.assertIn("rename Queue -> Established", output)
        self.assertIn("rename Queue.field -> Established.value", output)
        self.assertNotIn("typedef", shared.read_text())
        generated = (self.project.include[0] / "shared/alpha.h").read_text()
        self.assertIn('#include "shared/established.h"', generated)
        self.assertIn("struct Established queue;", generated)
        self.assertNotIn("typedef", (self.project.src / source.name).read_text())
        self.assertIn(": OK", self.make())

    def test_cleanup_rewrites_collision_in_editable_source_and_staged_headers(self):
        shared = self.project.include[0] / "shared/owner.h"
        shared.write_text("#ifndef OWNER_H\n#define OWNER_H\ntypedef struct Owner {int value;} Owner;\n#endif\n")
        self.cli("solve")
        before = shared.read_bytes()
        source = self.sources[0]
        source.write_text(
            "typedef struct Owner {short value;} Owner;\nint alpha(void) {return sizeof(Owner) / sizeof(Owner);}\n"
        )
        output = self.cli("decomp", "cleanup", source)
        self.assertIn("rename Owner -> Owner_", output)
        self.assertIn("sizeof(Owner_", source.read_text())
        self.assertNotIn("typedef", source.read_text())
        self.assertEqual(shared.read_bytes(), before)
        generated = self.project.include[0] / "shared/alpha.h"
        self.assertFalse(generated.exists())
        self.assertTrue((source.parent / "overlay/include/shared/alpha.h").is_file())
        self.cli("try", source)
        self.cli("submit", source)
        self.assertTrue(generated.is_file())
        self.assertEqual(shared.read_bytes(), before)
        self.assertIn(": OK", self.make())

    def test_equal_named_definitions_in_one_source_fold_without_duplicate_tags(self):
        source = self.sources[0]
        source.write_text(
            "typedef struct First {int value; struct First *next;} First;\n"
            "typedef struct Second {int other; struct Second *tail;} Second;\n"
            "int alpha(void) {return sizeof(((Second *)0)->other) / sizeof(((First *)0)->value);}\n"
        )
        self.cli("try", source)
        output = self.cli("submit", source)
        self.assertIn("rename Second -> First", output)
        header = (self.project.include[0] / "shared/alpha.h").read_text()
        self.assertEqual(len(Parser(header).parse()), 1)
        self.assertNotIn("Second", header)
        published = (self.project.src / source.name).read_text()
        self.assertIn("((First *)0)->value", published)
        self.assertIn(": OK", self.make())

    def test_generated_type_reuse_by_value_survives_the_submit_solve_refresh(self):
        generated = self.project.include[0] / "shared/types/borrowed.h"
        generated.parent.mkdir(exist_ok=True)
        generated.write_text("#ifndef BORROWED_H\n#define BORROWED_H\nstruct Borrowed {int value;};\n#endif\n")
        source = self.sources[0]
        source.write_text(
            "typedef struct Queue {int field;} Queue;\n"
            "typedef struct Holder {Queue queue; int count;} Holder;\n"
            "int alpha(void) {return sizeof(Holder) / sizeof(Holder);}\n"
        )
        self.cli("try", source)
        output = self.cli("submit", source)
        self.assertNotIn("HELD(types)", output)
        self.assertIn("rename Queue -> Borrowed", output)
        rendered = "\n".join(path.read_text() for path in (self.project.include[0] / "shared/types").glob("*.h"))
        self.assertIn("struct Borrowed", rendered)
        self.assertIn("struct Borrowed queue;", (self.project.include[0] / "shared/alpha.h").read_text())
        self.assertIn(": OK", self.make())

    def test_array_alias_reuses_the_equal_shared_array_layout(self):
        shared = self.project.include[0] / "shared/bytes.h"
        shared.write_text("#ifndef BYTES_H\n#define BYTES_H\nstruct First {char data[4];};\n#endif\n")
        source = self.sources[0]
        source.write_text(
            "typedef char Bytes[4];\n"
            "typedef struct Other {Bytes payload;} Other;\n"
            "int alpha(void) {return sizeof(((Other *)0)->payload) / 4;}\n"
        )
        self.cli("try", source)
        output = self.cli("submit", source)
        self.assertIn("rename Other -> First", output)
        published = (self.project.src / source.name).read_text()
        self.assertIn("((struct First *)0)->data", published)
        generated = (self.project.include[0] / "shared/alpha.h").read_text()
        self.assertIn("typedef char Bytes[4];", generated)
        self.assertEqual(Parser(generated).parse(), [])
        self.assertIn('#include "shared/bytes.h"', published)
        self.assertIn('#include "shared/alpha.h"', published)
        self.assertNotIn("typedef", published)
        self.assertEqual(
            shared.read_text(), "#ifndef BYTES_H\n#define BYTES_H\nstruct First {char data[4];};\n#endif\n"
        )
        self.assertIn(": OK", self.make())
