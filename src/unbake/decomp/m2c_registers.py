"""Run the configured m2c entry point with additional analysis-only FPR pairs."""

import importlib
import runpy
import sys


def main() -> None:
    MipsArch = importlib.import_module("m2c.arch_mips").MipsArch
    Register = importlib.import_module("m2c.asm_instruction").Register

    # f(32 + 2*n) represents independent physical fn; its odd complement
    # exists only for m2c's FR=0 double handling. Preserve call-clobber classes.
    temporary = [Register(f"f{index}") for index in range(32, 72)]
    saved = [Register(f"f{index}") for index in range(72, 96)]
    MipsArch.simple_temp_regs = [*MipsArch.simple_temp_regs, *temporary]
    MipsArch.temp_regs = [*MipsArch.temp_regs, *temporary]
    MipsArch.saved_regs = [*MipsArch.saved_regs, *saved]
    MipsArch.all_regs = [*MipsArch.all_regs, *temporary, *saved]
    executable = sys.argv.pop(1)
    sys.argv[0] = executable
    runpy.run_path(executable, run_name="__main__")


if __name__ == "__main__":
    main()
