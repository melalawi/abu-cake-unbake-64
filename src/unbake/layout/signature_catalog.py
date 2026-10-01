"""Build an offline catalog from pinned n64sym JSON signature data.

Run: python -m unbake.layout.signature_catalog INPUT OUTPUT
--source-url URL --commit FULL_COMMIT --sha256 INPUT_SHA256 --license-file LICENSE
The upstream MIT notice is embedded; neither building nor matching accesses a network.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any

from unbake.layout.boundary_signatures import load
from unbake.project.config import Held

RELOCATION_MASKS = {"hi16": 0xFFFF, "lo16": 0xFFFF, "targ26": 0x03FFFFFF}


def build(input_path: Path, output: Path, *, source_url: str, commit: str, sha256: str, license_file: Path) -> None:
    """Verify the supplied pin, translate relocation offsets, and preserve provenance."""
    try:
        data = input_path.read_bytes()
        if not re.fullmatch(r"[0-9a-f]{40}", commit):
            raise ValueError("commit: required full pinned commit")
        if not re.fullmatch(r"[0-9a-f]{64}", sha256) or hashlib.sha256(data).hexdigest() != sha256:
            raise ValueError("sha256: input does not match pinned digest")
        if not source_url.startswith("https://"):
            raise ValueError("source-url: required HTTPS provenance")
        notice = license_file.read_text()
        if "Permission is hereby granted, free of charge" not in notice:
            raise ValueError("license-file: required upstream MIT notice")
        rows: list[dict[str, Any]] = []
        for name, size, head, body, relocations in json.loads(data):
            if not isinstance(size, int) or size < 8 or size % 4:
                raise ValueError(f"{name}: size must be at least eight aligned bytes")
            masks = [0] * (size // 4)
            for kind, _target, offsets in relocations:
                if kind not in RELOCATION_MASKS:
                    raise ValueError(f"{name}: unsupported relocation {kind}")
                for offset in offsets:
                    if not isinstance(offset, int) or offset % 4 or not 0 <= offset < size:
                        raise ValueError(f"{name}: relocation offset {offset} outside function")
                    masks[offset // 4] |= RELOCATION_MASKS[kind]
            rows.append(
                {
                    "name": name,
                    "size": size,
                    "crc_head": head,
                    "crc_body": body,
                    "masks": [f"{mask:08x}" for mask in masks],
                }
            )
        document = {
            "source": f"{source_url}@{commit} sha256:{sha256}",
            "provenance": {"url": source_url, "commit": commit, "sha256": sha256, "license": "MIT", "notice": notice},
            "signatures": rows,
        }
        # Validate with the consumer before publishing, without writing through symlinks.
        if output.is_symlink() or any(parent.is_symlink() for parent in output.parents):
            raise ValueError(f"{output.name}: refusing symlink output")
        text = json.dumps(document, sort_keys=True, indent=2) + "\n"
        temporary = output.with_name(output.name + ".pending")
        if temporary.exists() or temporary.is_symlink():
            raise ValueError(f"{temporary.name}: staging path already exists")
        try:
            temporary.write_text(text)
            load(temporary)
            temporary.replace(output)
        finally:
            temporary.unlink(missing_ok=True)
    except (OSError, ValueError, TypeError, KeyError) as error:
        raise Held("boundary", f"signature catalog: {error}") from error


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--source-url", required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--sha256", required=True)
    parser.add_argument("--license-file", required=True, type=Path)
    args = parser.parse_args()
    try:
        build(
            args.input,
            args.output,
            source_url=args.source_url,
            commit=args.commit,
            sha256=args.sha256,
            license_file=args.license_file,
        )
    except Held as error:
        parser.exit(1, f"{error}\n")


if __name__ == "__main__":
    main()
