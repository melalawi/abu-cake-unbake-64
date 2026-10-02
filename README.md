# AbuCakeUnbake64

Tools to help unbake a cake!

This personal toolkit may change or break without notice.

## Install

```sh
python3 -m pip install git+https://github.com/melalawi/abu-cake-unbake-64
```

Python 3.11 or later is required.
The build uses make and MIPS binutils.
Tool paths belong in `~/.config/unbake/policy.toml`.
Setup names any missing policy field.
Supply `setup_version_jobs` explicitly in host policy to limit simultaneous version
extractions and builds. `cores` supplies a shared build CPU budget for those jobs.
The measured host policy uses `setup_version_jobs = 4`; supply that value explicitly.
Choose a lower version limit if the host has less memory.
Compiler downloads have pinned SHA-256 hashes.

## Start

```sh
unbake init NAME
cd NAME
```

Put ROMs in the folder printed by init.
Run `unbake setup` to read every ROM and review the compiler proposal.
Accept its digest to prove every version before work starts.
Init and setup leave commits to you.

## Work

```sh
unbake map
unbake solve
unbake next
```

Map measures the whole program across every version.
Solve incrementally refreshes map facts affected by symbol placements or layout
boundaries, then publishes shared types. Unchanged instructions retain their facts;
changed ROMs require a new whole-program map.

To recover a referenced function hidden in a generated data row, preview
`unbake split code FUNCTION --version V --start ROM_OFFSET --end ROM_OFFSET`.
It requires a direct code reference or a referenced code-pointer table and a
closed, gapless instruction body. Add `--apply` to prove the cartridge and publish
the layout and entry symbol together. Prefix and suffix data keep their bytes.
Next ranks cross-version items and prints the command to run.
Follow it through draft then try then submit.
Try measures each containing version with its compiler candidates.
Exact equivalents retain their proofs and a deterministic build choice.
Submit proves the cartridge and feeds types back into solve.
Passing fuzzy drafts keep their assembly under `NON_MATCHING`.
Projects can reserve authored items with `unbake-exclusions.json`.

## Development

Run `bin/test` then `bin/lint` and `bin/hygiene`.

## License

[GNU GPL version 3 or later](LICENSE).
