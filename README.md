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
Choose the version limit for the memory available on the host.
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
Solve publishes shared types from those facts.
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
