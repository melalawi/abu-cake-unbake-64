# AbuCakeUnbake64

Tools to help unbake a cake!

Install with `python3 -m pip install git+https://github.com/melalawi/abu-cake-unbake-64`.
Python 3.11 or later is required along with make and MIPS binutils.
Set tool paths and build limits in `~/.config/unbake/policy.toml`.
Set `cache_root = "/absolute/path/to/cache"` in that policy for reusable host build artifacts.
Setup prints the policy path and names missing fields.

Run `unbake init NAME` and put your ROM dumps in the printed folder.
Run `unbake setup` to review the layout and compiler proposal.
Confirm its digest to prove every cartridge.
Init and setup leave commits to you.
Function items span versions and C can use version conditionals.

Run `unbake map` then `unbake solve` to build the shared type context.
Follow `unbake next` through draft then try then submit; use `unbake next --new` for undrafted functions.
Try measures every containing version.
Submit folds shared declarations and proves the cartridges before publication.
Use `unbake submit --batch SOURCE...` to prove several sources together.
It compiles changed objects and relinks retained objects.
Refused sources are named and the proven passing subset publishes together.
Partial success exits 1.
Proof journals live in `.unbake/state/publications`.
Exact sources feed types back into solve.
Passing fuzzy sources keep assembly under `NON_MATCHING`.

Run `bin/test` then `bin/lint` and `bin/hygiene` for development.
Licensed under [GNU GPL version 3 or later](LICENSE).

Imported C can explicitly supply authored declaration evidence with
`[paths] declaration_evidence = ["/path/to/old/include"]` in `config.toml`.
The include trees are read only. Needed absent names are selected by declaration,
then use the ordinary aggregate collision/rename and shared layout fold. Extern
names require the live symbol/address inventory; ambiguous declarations hold.
Supplemental aliases, prototypes, enums and macros enter generated split components
and the normal solve/feedback database. No old header is installed. Evidence inputs
are content pinned, and changing them invalidates the solved type context.
Decompiler placeholder typedefs and `m2c_prelude.h` are excluded from evidence.

To admit declarations for sources that still need instruction work, run
`unbake solve --declarations-needed candidate.c other.c` with the evidence tree
configured. This records declared evidence through shared layout folding and solve,
without marking functions matched. Per-source refusals are recorded in
`build/types/declaration-admission.json`; a failed solve rolls back admission edits.
