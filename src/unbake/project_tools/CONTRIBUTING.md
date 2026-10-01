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
unbake setup --names-from @NAMES_FROM@
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
unbake next
unbake draft FUNCTION
unbake try FILE
unbake submit FILE
```

Use the function suggested by `next`.
Use the file printed by `draft`.
Edit that file and run `try` again.
Submit the exact file after it matches every holding version.
Submit proves the ROMs before it publishes the C.

Every command ends with the next command to run.
`unbake next` prints it again.
