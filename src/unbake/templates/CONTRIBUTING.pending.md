# Contributing

## Install

```sh
python3 -m pip install git+https://github.com/melalawi/abu-cake-unbake-64
```

## New project

Run these commands inside this repo.
Put your ROMs in `roms/`.
Input filenames can be arbitrary.
Setup reads the ROM headers to name each version.

```sh
unbake setup
```

Each cross-version item gets one name.
It shows compiler evidence and asks for confirmation.
It prints the policy path and names any missing input.
It proves every version before the project is ready.
Setup then writes instructions with your ROM paths.
If setup asks for `names_from` choose a version.
Run `unbake setup --names-from VERSION`.

## Next command

```sh
unbake map
unbake solve
unbake next
```

Run map and solve after setup succeeds.
Map reads every version.
Solve builds shared types from the measured facts.
Every command ends with the next command to run.
`unbake next` prints it again.
