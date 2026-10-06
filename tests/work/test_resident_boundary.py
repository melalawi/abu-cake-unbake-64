"""Selection retains configured table identity before and after resident migration."""

import struct
from dataclasses import replace

from tests.kit import TempCase
from tests.project_fixture import make
from unbake.config import Held, ResidentMapping
from unbake.layout import rodata_owners, split
from unbake.work import plan, shape


class ResidentBoundaryTests(TempCase):
    def project(self, migrated, bias):
        # sll index; lui table; addu; lw target; jr target; nop; two returning cases.
        code = [
            0x00041080,
            0x3C01800C,
            0x00220821,
            0x8C220000,
            0x00400008,
            0,
            0x03E00008,
            0x24020001,
            0x03E00008,
            0x24020002,
        ]
        project, host = make(self.root / f"case-{migrated}-{bias}", code)
        version = project.version("us")
        image = version.baserom.read_bytes()
        start = len(image)
        table = struct.pack(">2I", (0x80001018 - bias) & 0xFFFFFFFF, (0x80001020 - bias) & 0xFFFFFFFF)
        version.baserom.write_bytes(image + table)
        yaml = version.split.read_text().rsplit("  - [", 1)[0]
        if migrated:
            yaml += (
                f"  - name: constants\n    type: code\n    start: 0x{start:X}\n"
                "    vram: 0x800C0000\n    subsegments:\n"
                f"      - [0x{start:X}, rodata, table]\n"
            )
        else:
            yaml += f"  - [0x{start:X}, bin, constants]\n"
        version.split.write_text(yaml + f"  - [0x{start + 8:X}]\n")
        project = replace(project, resident_mappings={"us": (ResidentMapping(0x800C0000, start, start + 8, bias),)})
        return project, host

    def test_configured_pointer_identity_survives_native_resident_migration(self):
        for migrated in (False, True):
            for bias in (0, 0x80000000):
                with self.subTest(migrated=migrated, bias=bias):
                    project, _ = self.project(migrated, bias)
                    row = split.functions(project, "us")[0]
                    target = shape.configured(project)[0][project.default_compiler]
                    proof = plan._placement(project, row, split.words(project, row), target)
                    self.assertTrue(proof.proven, proof.unproven)
                    self.assertIn("proved-local-jump-table", proof.tags)
                    tables = [item for item in rodata_owners.scan(project, "us").objects if item.kind == "jump table"]
                    self.assertEqual(
                        [(item.address, item.end, item.owners) for item in tables],
                        [(0x800C0000, 0x800C0008, {"alpha"})],
                    )

    def test_outside_table_targets_are_still_refused(self):
        project, _ = self.project(True, 0x80000000)
        version = project.version("us")
        image = version.baserom.read_bytes()
        version.baserom.write_bytes(image[:-4] + struct.pack(">I", 0x2000))
        row = split.functions(project, "us")[0]
        target = shape.configured(project)[0][project.default_compiler]
        proof = plan._placement(project, row, split.words(project, row), target)
        self.assertFalse(proof.proven)
        self.assertTrue(any("unresolved-indirect" in reason for reason in proof.unproven))

    def test_conflicting_native_backing_is_not_used_as_table_proof(self):
        project, _ = self.project(True, 0)
        mapping = project.resident_mappings["us"][0]
        project = replace(project, resident_mappings={"us": (replace(mapping, start=mapping.start - 4),)})
        row = split.functions(project, "us")[0]
        target = shape.configured(project)[0][project.default_compiler]
        with self.assertRaisesRegex(Held, "conflicting resident backing"):
            plan._placement(project, row, split.words(project, row), target)

    def test_changed_explicit_mapping_invalidates_the_placement_memo(self):
        project, _ = self.project(True, 0x80000000)
        row = split.functions(project, "us")[0]
        target = shape.configured(project)[0][project.default_compiler]
        data = split.words(project, row)
        self.assertTrue(plan._placement(project, row, data, target).proven)
        mapping = project.resident_mappings["us"][0]
        changed = replace(project, resident_mappings={"us": (replace(mapping, table_entry_bias=0),)})
        self.assertFalse(plan._placement(changed, row, data, target).proven)
