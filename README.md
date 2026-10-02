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
Try measures each containing version with its compiler candidates and retains
source-scoped receipts without changing project build inputs. Submit publishes
compiler choices, equivalence evidence, shared declarations, layout and exclusion
removals together, holding the publication lock through the cartridge proof.
Use `submit --batch SOURCE...` to fold and prove several receipts together.
A batch publishes its cartridge-proven passing subset and names refused sources.
Proof failures use object bytes, symbol bytes and link diagnostics to identify
faults; further isolation relinks retained objects. Refusals stream immediately,
and proof evidence and receipts persist in `.unbake/state/publications/*.jsonl`
even if the command stops. A partial publication returns a failing exit status.
Unrelated compiler evidence and layout entries do not invalidate a receipt.
Exact equivalents retain their proofs and a deterministic build choice.
Submit proves the cartridge and feeds types back into solve.
Passing fuzzy drafts keep their assembly under `NON_MATCHING`.
Projects can reserve authored items with `unbake-exclusions.json`.

## Development

Run `bin/test` then `bin/lint` and `bin/hygiene`.

## License

[GNU GPL version 3 or later](LICENSE).
