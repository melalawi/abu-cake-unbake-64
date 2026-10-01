"""Callee evidence follows the compiled VERSION's active references."""

import struct
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from tests.layout.test_xver import Project
from tests.support import tool
from unbake.decomp.needs import PlacementNeed, SymbolNeed
from unbake.layout import xver
from unbake.project.config import Held


class VersionCalleeTests(unittest.TestCase):
    def test_active_callees_use_local_placement_then_shared_correspondence(self) -> None:
        for mode in ("regional", "different", "shared"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                project = Project(root)
                artifacts = {}
                targets: dict[str, list[int]] = {}
                source = root / "caller.S"
                source.write_text(
                    ".set noreorder\n.text\n"
                    "#if defined(VERSION_EU_X) && defined(REGIONAL)\njal regional\n"
                    "#else\njal common\n#endif\nnop\njr $ra\nnop\n"
                )
                for version in project.versions:
                    local = version == "eu-x"
                    name = "regional" if local and mode == "regional" else "common"
                    row = "native" if local and mode == "shared" else name
                    caller = [0x0C000000 | (0x80200020 >> 2 & 0x03FFFFFF), 0, 0x03E00008, 0]
                    targets[version] = caller
                    callee = [0x24020002 if local and mode != "shared" else 0x24020001, 0x03E00008, 0]
                    project.layout(
                        version,
                        [(0x40, "asm", "caller"), (0x50, "data", "gap"), (0x60, "asm", row), (0x6C, "data", "tail")],
                    )
                    project.image(version, [(0x40, struct.pack(">4I", *caller)), (0x60, struct.pack(">3I", *callee))])
                    assembly = root / version / "caller.s"
                    compiled = subprocess.run(
                        [
                            tool("cpp"),
                            "-P",
                            f"-DVERSION_{version.upper().replace('-', '_')}",
                            *(["-DREGIONAL"] if mode == "regional" else []),
                            str(source),
                        ],
                        check=True,
                        capture_output=True,
                        text=True,
                    )
                    assembly.write_text(compiled.stdout)
                    obj = assembly.with_suffix(".o")
                    subprocess.run(
                        [tool("mips-linux-gnu-as"), "-EB", "-o", str(obj), str(assembly)],
                        check=True,
                        capture_output=True,
                    )
                    artifacts[version] = {
                        "unit": SimpleNamespace(path=obj),
                        "target_words": caller,
                        "span": SimpleNamespace(address=0x80200000),
                    }
                context = SimpleNamespace(
                    project=project, trial=SimpleNamespace(function="caller"), artifacts=artifacts
                )
                before = {
                    path: path.read_bytes()
                    for item in project.maps.values()
                    for path in (item.split, item.symbols, item.baserom)
                }
                result = xver.derive(context)
                symbols = [need for need in result if isinstance(need, SymbolNeed)]
                self.assertEqual(
                    [(need.version, need.name, need.address) for need in symbols],
                    [
                        ("us", "common", 0x80200020),
                        ("eu-x", "regional" if mode == "regional" else "common", 0x80200020),
                    ],
                )
                self.assertTrue(
                    any(
                        isinstance(need, PlacementNeed) and need.version == "eu-x" and need.function == symbols[1].name
                        for need in result
                    )
                )
                self.assertEqual(project.names_from, "us")
                self.assertEqual(before, {path: path.read_bytes() for path in before})
                targets["eu-x"][0] += 1
                with self.assertRaisesRegex(Held, r"VERSION eu-x callee .*no proved twin"):
                    xver.derive(context)
