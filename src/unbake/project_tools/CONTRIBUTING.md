# Contributing to @TITLE@

## Install

```sh
python3 -m pip install git+https://github.com/melalawi/abu-cake-unbake-64
```

## New project

Run these commands inside this repo.
Put your own ROM dumps at these paths.

@ROM_INPUTS@

```sh
unbake setup
```

Setup shows compiler evidence and asks for confirmation.
It prints the policy path and names any missing input.
It checks every ROM before the project is ready.

```sh
make check
```

Plain `make` and `make check` cover every version.
Use `VERSION=<version>` to select one version.
Compiler files live in `@TOOLS@`.
Their hashes are recorded in `@PINS@`.

## Next command

```sh
unbake map
unbake solve
unbake next
unbake draft FUNCTION
unbake try FILE
unbake submit FILE
```

Map reads the whole program across every version.
Solve builds shared types from the measured facts.
Use the item suggested by `next`.
Use the file printed by `draft`.
Draft states which containing version it uses.
It uses the shared type context.
Edit that file and run `try` again.
Submit the exact file after it matches every holding version.
Submit proves the ROMs before it publishes the C.
It feeds proven facts back into solve.
Affected neighbours are marked for another draft.

Every command ends with the next command to run.
`unbake next` prints it again.
