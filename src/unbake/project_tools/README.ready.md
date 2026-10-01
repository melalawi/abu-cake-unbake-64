# @TITLE@

## Progress

@PROGRESS@

## Building

Put your own ROM dumps at the paths in [CONTRIBUTING.md](CONTRIBUTING.md).
Run these commands inside this repo.

```sh
unbake setup
make check
```

Plain `make` and `make check` cover every version.

## Next command

```sh
unbake map
unbake solve
unbake next
```

Map reads the whole program across every version.
Solve builds shared types from the measured facts.
Follow the printed command through `draft` then `try` then `submit`.
Read [CONTRIBUTING.md](CONTRIBUTING.md) for the full steps.
