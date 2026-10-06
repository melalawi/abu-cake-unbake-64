"""The register adapter imports the configured decompiler, not its adjacent m2c.py."""

import json
import runpy
import sys
from pathlib import Path
from unittest.mock import patch

from tests.kit import TempCase
from unbake.decomp import m2c_registers


class M2cLauncherTests(TempCase):
    def test_entrypoint_imports_and_arguments_survive_the_register_adapter(self):
        package = self.root / "installed" / "m2c"
        package.mkdir(parents=True)
        (package / "__init__.py").write_text("")
        (package / "arch_mips.py").write_text(
            "class MipsArch:\n simple_temp_regs=[]\n temp_regs=[]\n saved_regs=[]\n all_regs=[]\n"
        )
        (package / "asm_instruction.py").write_text("class Register(str): pass\n")
        entry = self.root / "bin" / "m2c"
        entry.parent.mkdir()
        output = self.root / "result.json"
        entry.write_text(
            "import json,sys\nfrom m2c.arch_mips import MipsArch\nfrom pathlib import Path\n"
            f"Path({str(output)!r}).write_text(json.dumps([sys.argv, MipsArch.all_regs]))\n"
        )
        wrapper = Path(m2c_registers.__file__)
        with (
            patch.object(sys, "argv", [str(wrapper), str(entry), "--function", "alpha"]),
            patch.object(sys, "path", [str(wrapper.parent), str(package.parent), *sys.path]),
            patch.dict(sys.modules),
        ):
            for name in list(sys.modules):
                if name == "m2c" or name.startswith("m2c."):
                    del sys.modules[name]
            runpy.run_path(str(wrapper), run_name="__main__")
        argv, registers = json.loads(output.read_text())
        self.assertEqual(argv, [str(entry), "--function", "alpha"])
        self.assertEqual(registers, [f"f{i}" for i in range(32, 96)])
