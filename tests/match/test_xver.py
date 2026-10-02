"""Submission proves all named owners and leaves differently named units alone."""

from tests.match.support import MatchFixture
from unbake.match import queue as match
from unbake.project.config import Held


class CrossVersionTests(MatchFixture):
    def test_equal_named_owners_require_trials_on_both_versions(self) -> None:
        source = self.draft("alpha", versions=["us"])
        with self.assertRaisesRegex(Held, "submit.versions"):
            match.submit(self.project, self.policy, source)
        self.prove(source)
        receipts = match.publish_source(self.project, self.policy, source)
        self.assertTrue(any("matched on VERSION us, eu" in line for line in receipts), receipts)

    def test_different_named_function_is_not_an_implicit_owner(self) -> None:
        cartridge = self.project.version("eu")
        cartridge.split.write_text(cartridge.split.read_text().replace("text/alpha", "eu_alpha"))
        source = self.draft("alpha", versions=["us"])
        receipts = match.publish_source(self.project, self.policy, source)
        self.assertTrue(any("matched on VERSION us" in line for line in receipts), receipts)
        self.assertIn("asm, eu_alpha", cartridge.split.read_text())
        self.assertEqual(self.current(self.project, "eu"), self.original["eu"])

    def test_indexed_relocation_locates_a_peer_but_expansion_still_requires_exact_bytes(self) -> None:
        import struct

        from unbake.match.common import Draft
        from unbake.match.xver import expand

        for version in self.versions:
            row = self.project.version(version)
            name = "alpha" if version == "us" else "peer"
            row.split.write_text(
                "segments:\n  - name: main\n    type: code\n    start: 0x1000\n"
                "    vram: 0x80001000\n    subsegments:\n"
                f"      - [0x1000, asm, {name}]\n      - [0x1014, data, pool]\n  - [0x1040]\n"
            )
            row.symbols.write_text(f"{name} = 0x80001000;\n")
            image = bytearray(0x1040)
            image[:4] = bytes.fromhex("80371240")
            low = 0x100 if version == "us" else 0x200
            image[0x1000:0x1014] = struct.pack(">5I", 0x3C018000, 0x00220821, 0x8C220000 | low, 0x03E00008, 0)
            row.baserom.write_bytes(image)
        draft = Draft({"function": "alpha"}, b"", ("us",))
        receipts = []
        self.assertEqual(expand(self.project, draft, receipts).versions, ("us",))
        self.assertTrue(any("bytes differ" in line for line in receipts), receipts)
        self.project.version("eu").baserom.write_bytes(self.project.version("us").baserom.read_bytes())
        self.assertEqual(expand(self.project, draft, []).versions, self.versions)
