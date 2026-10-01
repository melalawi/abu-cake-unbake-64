"""A single proved source lands across addresses and VERSION-local names."""

from tests.match.support import MatchFixture
from unbake.match import queue as match


class CrossVersionTests(MatchFixture):
    def test_matched_source_expands_only_to_identical_cartridges(self) -> None:
        for identical in (True, False):
            with self.subTest(identical=identical):
                if not identical:
                    self.tearDown()
                    self.doCleanups()
                    self.setUp()
                body = bytes.fromhex("2402002a2442000103e0000800000000")
                for version in self.versions:
                    cartridge = self.project.version(version)
                    text = cartridge.split.read_text().replace(
                        "    start: 0x1000\n", "    start: 0x1000\n    vram: 0x80001000\n"
                    )
                    image = bytearray(0x1040)
                    image[:4] = bytes.fromhex("80371240")
                    if version == "us":
                        text = text.replace("asm, text/alpha", "c, alpha")
                        image[0x1000:0x1010] = body
                    else:
                        target = "eu_alpha" if identical else "alpha"
                        text = text.replace("text/alpha", "eu_other").replace("asm, beta", f"asm, {target}")
                        cartridge.symbols.write_text(
                            f"eu_other = 0x80001000;\n{target} = 0x80001010;\ngamma = 0x80001020;\n"
                        )
                        image[0x1010:0x1020] = body if identical else bytes.fromhex("2402002b2442000103e0000800000000")
                    cartridge.split.write_text(text)
                    cartridge.baserom.write_bytes(image)
                source = self.draft("alpha", versions=["us"])
                (self.src / "alpha.c").write_bytes(source.read_bytes())
                match.submit(self.project, self.policy, source, versions=("us",))
                receipts = match.run(self.project, self.policy)
                self.assertTrue(any("alpha matched" in receipt for receipt in receipts), receipts)
                selected = ["us", "eu"] if identical else ["us"]
                self.assertEqual(self.matched()[0]["versions"], selected)
                cartridge = self.project.version("eu")
                if identical:
                    self.assertIn("[0x1010, c, alpha]", cartridge.split.read_text())
                    self.assertIn("alpha = 0x80001010;", cartridge.symbols.read_text())
                    self.assertNotIn("eu_alpha", cartridge.symbols.read_text())
                else:
                    self.assertIn("[0x1010, asm, alpha]", cartridge.split.read_text())
                    self.assertTrue(
                        any(
                            "alpha" in receipt and "eu" in receipt and "bytes differ" in receipt for receipt in receipts
                        )
                    )
