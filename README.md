# AbuCakeUnbake64

Tools to help unbake a cake!

> This is a personal toolkit. It is not officially maintained and may change or break without notice.

## Install

```sh
python3 -m pip install git+https://github.com/melalawi/abu-cake-unbake-64
```

## New project

```sh
unbake init NAME
```

Init creates the named repo and prints its ROM folder.
Put your ROMs there.
Run these commands inside the repo.

```sh
unbake setup
```

Setup reads every ROM header.
Each cross-version item gets one name.
It shows compiler evidence and asks for one confirmation of the whole proposal.
Clear regional winners need no `--compiler` flags; a tied region needs an explicit choice.
It proves every version before the project is ready.
Setup prints the policy path and names any missing input.
Machine paths go in `~/.config/unbake/policy.toml`.
If setup asks for `names_from` choose a version.
Run `unbake setup --names-from VERSION`.

## Next command

```sh
unbake map
unbake solve
unbake next
```

Map reads the whole program across every version.
Solve builds shared types from the measured facts.
Every command ends with the next command to run.
`unbake next` prints it again.
Follow it through `draft` then `try` then `submit`.
Exact submits feed proven facts back into solve.
Passing fuzzy drafts publish under `NON_MATCHING`, retaining their assembly rows.
They require at least 90% exact words in every containing version, clean source,
and only register, order or relocation differences.

To reserve existing authored work for a later port, put `unbake-exclusions.json`
in the project root: `{"schema": 1, "functions": ["function_name"]}`.
`next` skips excluded items (including editable work and redrafts); `draft`
refuses them by name. Aliases exclude the entire item. Both commands accept
`--exclude FILE` to override the project manifest. Missing explicit files,
malformed manifests, duplicate entries and unknown function names are refused.

## Dependencies

- Python 3.11 or later
- [AbuMasN64](https://github.com/melalawi/abu-mas-n64) v0.1.0 at the commit pinned in `pyproject.toml`
- make
- MIPS binutils (as, ld, objcopy) and a C preprocessor
- [splat](https://github.com/ethteck/splat) version 0.50.0
- [m2c](https://github.com/matt-kempster/m2c) at `708d2d2cb2698f091a92492b328f73b24209f72d`
- [decomp-permuter](https://github.com/simonlindholm/decomp-permuter) at `059609d4aec73eb0650726772954e1ad575825f8`
- [objdiff](https://github.com/encounter/objdiff) CLI version 3.8.1

Setup checks compiler downloads against pinned SHA-256 hashes.

## License

Released under [GNU GPL version 3 or later](LICENSE).

For authored batches, `unbake submit --batch FILE...` admits each explicitly named
latest trial, proves exact candidates together, and reports held files by name.
Only requested functions are published. Passing fuzzy candidates retain their
assembly rows and require a fresh current-input trial after an exact batch.
