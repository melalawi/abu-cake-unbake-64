# AbuCakeUnbake64

Tools to help unbake a cake!

> This is a personal toolkit. It is not officially maintained and may change or break without notice.

## Install

```sh
python3 -m pip install .
```

Machine paths go in `~/.config/unbake/policy.toml`.

## Dependencies

- Python 3.11 or later
- [AbuMasN64](https://github.com/melalawi/abu-mas-n64) v0.1.0 at the commit pinned in `pyproject.toml`
- make
- MIPS binutils (as, ld, objcopy) and a C preprocessor
- [splat](https://github.com/ethteck/splat) version 0.50.0
- [m2c](https://github.com/matt-kempster/m2c) at `708d2d2cb2698f091a92492b328f73b24209f72d`
- [decomp-permuter](https://github.com/simonlindholm/decomp-permuter) at `059609d4aec73eb0650726772954e1ad575825f8`
- [objdiff](https://github.com/encounter/objdiff) CLI version 3.8.1

`unbake setup` downloads each game's compilers from the registry in `src/unbake/project/compilers.toml` and checks every archive and file by SHA-256. SN64 toolchains run their pinned cc1 through AbuMasN64 v0.1.0 and the GNU MIPS assembler named in the policy. The `gcc-2.8.1-sn64` compiler is downloaded from the public gcc-papermario Linux release; only its pinned cc1 is installed.

## Workflow

1. `init` creates a project from ROMs.
2. `split` cuts and names functions.
3. `decomp` drafts and searches for C.
4. `match` lands identical C.
5. `report` updates progress.

## Development

Run checks with `ci/check`. GitLab users set the CI file path to `ci/gitlab.yml`.

## License

Released under [GNU GPL version 3 or later](LICENSE).
