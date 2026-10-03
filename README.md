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
