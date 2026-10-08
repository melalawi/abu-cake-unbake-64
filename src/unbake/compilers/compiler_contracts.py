"""Portable semantic preprocessor contract shared with ordinary Make.

Native result ownership stays with process.NativeResult. This validator consumes
its fields, or the same fields at the standalone process boundary.
"""

from __future__ import annotations

import re

IDO_DIRECTIVE = r"^cfe: Warning 605: (.+?): (\d+): #\s*error\b([^\n]*)"


def validate_preprocessed(stdout: str, stderr: str, exit_code: int | None, contract: str) -> str:
    if contract not in {"gcc-1", "ido-1"}:
        raise ValueError(f"preprocess.contract: unknown {contract}")
    if exit_code != 0:
        raise ValueError(f"preprocess.native: exit {exit_code}")
    if contract == "ido-1":
        match = re.search(IDO_DIRECTIVE, stderr, re.M)
        if match:
            raise ValueError("compile.preprocessor_directive: active #error: " + match[3].strip())
    return stdout


def main() -> int:
    import argparse
    import subprocess
    import sys
    from pathlib import Path

    parser = argparse.ArgumentParser()
    parser.add_argument("--contract", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("argv", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.argv[1:] if args.argv[:1] == ["--"] else args.argv
    result = subprocess.run(command, capture_output=True)
    sys.stderr.buffer.write(result.stderr)
    try:
        validate_preprocessed(
            result.stdout.decode(errors="replace"),
            result.stderr.decode(errors="replace"),
            result.returncode,
            args.contract,
        )
    except ValueError as error:
        sys.stderr.write(str(error) + "\n")
        return result.returncode or 1
    args.output.write_bytes(result.stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
