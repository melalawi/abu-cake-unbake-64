# AbuCakeUnbake64

Tools to help unbake a cake!

Install with `python3 -m pip install git+https://github.com/melalawi/abu-cake-unbake-64`.
Requires Python 3.11 or later, make and MIPS binutils.
Set tool paths and build limits in `~/.config/unbake/policy.toml`.
Set `cache_root = "/absolute/path/to/cache"` for reusable host build artifacts.

Run `unbake init NAME` and put your ROM dumps in the printed folder.
Run `unbake setup` to review the layout and compiler proposal.
Confirm its digest to prove every cartridge. Init and setup leave commits to you.
Use `unbake setup --refresh-helpers` to refresh a ready project's helpers and recipes.
Unchanged files keep their timestamps. This skips cartridge setup and proof.

Run `unbake map` then `unbake solve` to build the shared type context.
Follow `unbake next` through draft, try and submit.
Use `unbake next --new` for undrafted functions.
Try measures every containing version.
Submit folds shared declarations and proves the cartridges before publication.
Use `unbake submit --batch SOURCE...` to prove several sources together.
It compiles changed objects and relinks retained objects.
Refused sources are named and the proven passing subset publishes together.
Partial success exits 1. Proof journals live in `.unbake/state/publications`.
Exact sources feed types back into solve.
Passing fuzzy sources keep assembly under `NON_MATCHING`.

Imported C can use `[paths] declaration_evidence = ["/path/to/old/include"]`
in `config.toml` for authored declarations. These include trees are read only.
Run `unbake solve --declarations-needed candidate.c other.c` to admit declarations
without marking functions matched. Ambiguous declarations hold.
Per-source refusals appear in `build/types/declaration-admission.json`.
A failed solve rolls back admission edits.

Run `bin/test`, `bin/lint` and `bin/hygiene` for development.
Licensed under [GNU GPL version 3 or later](LICENSE).
