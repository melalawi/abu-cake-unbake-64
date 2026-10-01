"""Installed-command acceptance with explicit ROM, policy and confirmation inputs.

Run bin/accept-flow --help for the stages. Each invocation records exact argv,
cwd, exit status and separate output streams. No unbake implementation is imported.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
import tomllib
from pathlib import Path


class ProofError(Exception):
    """A named missing input or an observed failed acceptance assertion."""


def require(condition: object, message: str) -> None:
    if not condition:
        raise ProofError(message)


def sha1(path: Path) -> str:
    return hashlib.sha1(path.read_bytes()).hexdigest()


def read_config(project: Path) -> dict:
    with (project / "config.toml").open("rb") as source:
        return tomllib.load(source)


class Proof:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.logs = self.root / "evidence"
        self.logs.mkdir(parents=True, exist_ok=True)
        self.python = self.root / "venv/bin/python"
        self.unbake = self.root / "venv/bin/unbake"
        self.env = dict(os.environ)
        self.env.pop("PYTHONPATH", None)
        self.env.update(
            TMPDIR=str(self.root / "tmp"),
            PIP_CACHE_DIR=str(self.root / "pip-cache"),
            PYTHONNOUSERSITE="1",
            PYTHONDONTWRITEBYTECODE="1",
            PATH=str(self.root / "venv/bin") + os.pathsep + self.env.get("PATH", ""),
        )
        self.env["UNBAKE_POLICY"] = str(self.root / "policy.toml")
        (self.root / "tmp").mkdir(exist_ok=True)

    def run(self, argv: list[str | Path], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
        command = [str(value) for value in argv]
        directory = cwd or self.root
        number = len(list(self.logs.glob("*.command.json"))) + 1
        label = f"{number:04d}"
        print(f"$ (cd {shlex.quote(str(directory))} && {shlex.join(command)})", flush=True)
        started = time.time()
        result = subprocess.run(
            command, cwd=directory, env=self.env, input="", capture_output=True, text=True, check=False
        )
        (self.logs / f"{label}.stdout").write_text(result.stdout)
        (self.logs / f"{label}.stderr").write_text(result.stderr)
        record = {
            "argv": command,
            "cwd": str(directory),
            "returncode": result.returncode,
            "started": started,
            "seconds": time.time() - started,
            "stdout": f"{label}.stdout",
            "stderr": f"{label}.stderr",
            "pythonpath": None,
        }
        (self.logs / f"{label}.command.json").write_text(json.dumps(record, indent=2) + "\n")
        print(result.stdout, end="", flush=True)
        print(result.stderr, end="", file=sys.stderr, flush=True)
        print(f"exit={result.returncode} evidence={label}", flush=True)
        return result

    def cli(self, *arguments: str | Path, cwd: Path | None = None, status: int | None = 0) -> str:
        result = self.run([self.unbake, *arguments], cwd)
        output = result.stdout + result.stderr
        if status is not None:
            require(result.returncode == status, f"cli.exit: expected {status}, got {result.returncode}")
        require("Traceback" not in output, "cli.traceback: public command raised an unhandled exception")
        receipts = re.findall(r"(?m)^Next: .+$", output)
        require(len(receipts) == 1, f"cli.next: expected one receipt, got {len(receipts)}")
        stream = result.stderr if "Next:" in result.stderr else result.stdout
        require(stream.rstrip().splitlines()[-1] == receipts[0], "cli.next: receipt must be final")
        return output

    def policy(self, source: Path) -> None:
        require(source.is_file(), f"accept.policy: missing file {source}")
        text = source.read_text()
        for key, folder in (("cache_root", "cache"), ("state_root", "state")):
            replacement = key + " = " + json.dumps(str(self.root / folder))
            text, count = re.subn(r"(?m)^" + key + r"\s*=.*$", lambda _, value=replacement: value, text)
            require(count == 1, f"policy.{key}: expected one explicit value")
        (self.root / "policy.toml").write_text(text)

    def install(self, spec: str) -> None:
        require(not self.python.exists(), "accept.root: existing virtual environment")
        result = self.run([sys.executable, "-m", "venv", self.root / "venv"])
        require(result.returncode == 0, "accept.venv: creation failed")
        result = self.run([self.python, "-m", "pip", "install", spec])
        require(result.returncode == 0, "accept.install: pip install failed")
        self.cli("--help")
        result = self.run([self.python, "-m", "unbake", "--help"])
        require(result.returncode == 0, "accept.module: help failed")
        require(result.stdout.count("Next: ") == 1, "accept.module: missing final receipt")
        self.run([self.python, "-m", "pip", "freeze", "--all"])
        code = (
            "import importlib.metadata as m, importlib.resources as r, json; "
            "p=m.distribution('unbake'); "
            "print(json.dumps({'requires':p.requires,'origin':p.read_text('direct_url.json')})); "
            "assert any('21c6a7f16bdd8e0b18544407067822bb1ef2b57d' in x for x in p.requires); "
            "assert r.files('unbake.project').joinpath('compilers.toml').is_file(); "
            "assert r.files('unbake.project').joinpath('policy.toml').is_file(); "
            "assert r.files('unbake.project_tools').joinpath('Makefile').is_file(); "
            "assert r.files('unbake.project_tools').joinpath('CONTRIBUTING.md').is_file(); "
            "assert r.files('unbake.project_tools').joinpath('CONTRIBUTING.pending.md').is_file()"
        )
        result = self.run([self.python, "-c", code])
        require(result.returncode == 0, "accept.package: pinned dependency or packaged asset missing")

    def prepare(
        self, project: Path, roms: list[str], names_from: str, compilers: list[str], supply: Path | None
    ) -> None:
        require(roms, "accept.roms: supply VERSION=FILE inputs")
        require(not project.exists() or not any(project.iterdir()), "accept.project: expected empty directory")
        self.cli("init", project)
        config = read_config(project)
        require(config["project"]["state"] == "awaiting-roms", "init.state: expected shell")
        require("compilers" not in config and "units" not in config, "init.compilers: shell has assignments")
        require(not (project / config["paths"]["build"]).exists(), "init.build: unexpected build")
        result = self.run(["git", "rev-parse", "--is-inside-work-tree"], project)
        require(result.returncode == 0 and result.stdout.strip() == "true", "init.git: missing repository")
        result = self.run(["git", "rev-parse", "--verify", "HEAD"], project)
        require(result.returncode != 0, "init.commit: unexpected automatic commit")
        output = self.cli("setup", cwd=project, status=1)
        require("setup.roms:" in output, "setup.roms: empty-input refusal missing")
        inputs = {}
        for index, assignment in enumerate(roms):
            require("=" in assignment, f"accept.roms: expected VERSION=FILE, got {assignment}")
            version, filename = assignment.split("=", 1)
            source = Path(filename).resolve()
            require(source.is_file(), f"accept.roms: missing file {source}")
            require(version not in inputs, f"accept.roms: duplicate expected version {version}")
            destination = project / config["paths"]["roms"] / f"image-{index}.z64"
            shutil.copyfile(source, destination)
            inputs[version] = {"source": str(source), "sha1": sha1(source)}
        evidence = self.logs / (project.name + ".inputs.json")
        evidence.write_text(json.dumps({"project": str(project), "roms": inputs}, indent=2) + "\n")
        arguments: list[str | Path] = ["setup", "--names-from", names_from]
        for choice in compilers:
            arguments.extend(["--compiler", choice])
        if supply:
            arguments.extend(["--supply", supply])
        output = self.cli(*arguments, cwd=project, status=1)
        require(
            "setup.compiler_confirmation:" in output or "setup.compiler_candidate:" in output,
            "setup.proposal: expected reviewable compiler proposal",
        )
        config = read_config(project)
        require(config["project"]["state"] == "awaiting-roms", "setup.confirmation: prematurely ready")
        require("compilers" not in config and "units" not in config, "setup.confirmation: assignments published")
        proposal = project / config["paths"]["build"] / "setup/proposal.json"
        require(proposal.is_file(), "setup.proposal: missing persisted evidence")
        saved = self.logs / (project.name + ".proposal.json")
        shutil.copyfile(proposal, saved)
        token = hashlib.sha256(proposal.read_bytes()).hexdigest()
        print(f"Review {saved}\nConfirmation token: {token}")

    def confirm(self, project: Path, token: str, supply: Path | None) -> None:
        arguments: list[str | Path] = ["setup", "--confirm", token]
        if supply:
            arguments.extend(["--supply", supply])
        self.cli(*arguments, cwd=project)
        require(read_config(project)["project"]["state"] == "ready", "setup.publication: project not ready")
        self.verify(project)

    def verify(self, project: Path) -> None:
        config = read_config(project)
        result = self.run(["make", "check"], project)
        require(result.returncode == 0, "make.check: ROM proof failed")
        inputs = json.loads((self.logs / (project.name + ".inputs.json")).read_text())["roms"]
        require(set(config["project"]["versions"]) == set(inputs), "setup.versions: wrong header labels")
        for version in config["project"]["versions"]:
            pinned = config["version"][version]
            expected = inputs[version]["sha1"]
            output = project / config["paths"]["build"] / version / (config["project"]["name"] + "." + version + ".z64")
            require(output.is_file(), f"setup.sha1.{version}: missing built ROM {output}")
            require(sha1(output) == expected == pinned["baserom_sha1"], f"setup.sha1.{version}: digest differs")
            require(sha1(Path(inputs[version]["source"])) == expected, f"accept.input.{version}: input changed")
            result = self.run(["git", "check-ignore", str(project / pinned["baserom"])], project)
            require(result.returncode == 0, f"setup.ignore.{version}: normalized ROM not ignored")
            result = self.run([self.unbake, "rodata", "owners", "--version", version], project)
            require(result.returncode == 0, f"rodata.owners.{version}: inspection failed")
            require("Next:" not in result.stdout, "rodata.json: guidance leaked to stdout")
            json.loads(result.stdout)
            require(result.stderr.rstrip().splitlines()[-1].startswith("Next: "), "rodata.next: missing stderr receipt")
        contributing = (project / "CONTRIBUTING.md").read_text()
        require(not re.search(r"@[A-Z_]+@", contributing), "docs.template: unexpanded placeholder")
        require("toolkit" not in contributing.lower(), "docs.wording: retired game description")
        for heading in ("Install", "New project", "Next command"):
            require("## " + heading in contributing, f"docs.heading: missing {heading}")
        for pinned in config["version"].values():
            require(pinned["baserom"] in contributing, "docs.roms: configured input path missing")
        result = self.run(["git", "ls-files"], project)
        require(result.returncode == 0, "setup.git: tracked-file query failed")
        for name in result.stdout.splitlines():
            require(not name.startswith(("roms/", "build/", "asm/")), f"setup.hygiene: tracked output {name}")
        before = self.canonical(project)
        self.cli("setup", cwd=project)
        require(self.canonical(project) == before, "setup.rerun: human work changed")

    @staticmethod
    def canonical(project: Path) -> dict[str, str]:
        config = read_config(project)
        roots = [config["paths"]["src"], *config["paths"]["include"]]
        return {
            str(path.relative_to(project)): hashlib.sha256(path.read_bytes()).hexdigest()
            for name in roots
            for path in (project / name).rglob("*")
            if path.is_file()
        }

    def cycle(self, project: Path, source: Path | None = None) -> None:
        output = self.cli("next", cwd=project)
        suggestion = next(line.removeprefix("Next: ") for line in output.splitlines() if line.startswith("Next: "))
        tokens = shlex.split(suggestion)
        require(tokens and tokens[0] == "unbake" and "draft" in tokens, "next.action: expected executable draft")
        if source is not None:
            tokens = ["unbake", "draft", source.stem]
        before = self.canonical(project)
        output = self.cli(*tokens[1:], cwd=project)
        action = next(line.removeprefix("Next: ") for line in output.splitlines() if line.startswith("Next: "))
        arguments = shlex.split(action)
        require("try" in arguments, "draft.action: expected printed try command")
        draft = Path(arguments[arguments.index("try") + 1])
        if not draft.is_absolute():
            draft = project / draft
        require(draft.is_file(), f"draft.source: missing file {draft}")
        require(self.canonical(project) == before, "draft.overlay: canonical files changed")
        if source is not None:
            shutil.copyfile(source, draft)
        self.cli("try", draft, cwd=project)
        require(self.canonical(project) == before, "trial.overlay: canonical files changed")
        exact = draft.read_bytes()
        draft.write_bytes(exact + b"\n")
        output = self.cli("submit", draft, cwd=project, status=1)
        require("source_sha256" in output, "submit.identity: changed untried input was not named")
        require(self.canonical(project) == before, "submit.refusal: canonical files changed")
        draft.write_bytes(exact)
        self.cli("try", draft, cwd=project)
        self.cli("submit", draft, cwd=project)
        self.verify(project)
        self.cli("next", cwd=project)

    def retirement(self) -> None:
        for arguments in (
            ("init", "unused", "--rom", "absent.z64"),
            ("init", "unused", "--rompath", "absent"),
            ("init", "unused", "--split", "files"),
            ("setup", "--new", "absent.z64"),
            ("rodata", "migrate", "alpha"),
            ("decomp", "draft", "alpha"),
            ("decomp", "try", "alpha.c"),
            ("match", "submit", "alpha.c"),
            ("match", "run"),
        ):
            output = self.cli(*arguments, status=1)
            require("invalid choice" in output or "unrecognized arguments" in output, "retire.parser: route accepted")
        code = (
            "import importlib.util as u; "
            "names=['unbake.layout.rodata_migrate','unbake.layout.rodata_bulk',"
            "'unbake.layout.rodata_switch','unbake.decomp.ledger']; "
            "assert all(u.find_spec(name) is None for name in names); "
            "import unbake.decomp.similar as s, unbake.decomp.declarations as d, unbake.match.free as f; "
            "assert all(not hasattr(module,'main') for module in (s,d,f))"
        )
        result = self.run([self.python, "-c", code])
        require(result.returncode == 0, "retire.imports: retired API remains")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True, help="Evidence and clean installed environment directory.")
    phases = parser.add_subparsers(dest="phase", required=True)
    install = phases.add_parser("install", help="Create a clean venv and pip install an explicit immutable source.")
    install.add_argument("--install-spec", required=True, help="Pinned Git URL or immutable source directory.")
    prepare = phases.add_parser("prepare", help="Init a shell, copy supplied ROMs and display the setup proposal.")
    prepare.add_argument("--policy", type=Path, required=True)
    prepare.add_argument("--rom", action="append", required=True, metavar="VERSION=FILE")
    prepare.add_argument("--names-from", required=True)
    prepare.add_argument("--compiler", action="append", default=[], metavar="REGION=ID")
    prepare.add_argument("--supply", type=Path)
    confirm = phases.add_parser("confirm", help="Accept an explicitly reviewed token and prove every ROM.")
    confirm.add_argument("--token", required=True)
    confirm.add_argument("--supply", type=Path)
    cycle = phases.add_parser("cycle", help="Execute next/draft/try/submit and refuse a changed untried source.")
    cycle.add_argument("--source", type=Path, help="Reviewed matching C to copy into the generated draft.")
    resubmit = phases.add_parser(
        "resubmit", help="Re-submit each read-only authored C input through public work commands."
    )
    resubmit.add_argument("--source-dir", type=Path, required=True)
    verify = phases.add_parser("verify", help="Check digests, generated docs, hygiene and repeat setup.")
    for command in (prepare, confirm, cycle, resubmit, verify):
        command.add_argument("--project", type=Path, required=True)
    phases.add_parser("retirement", help="Refuse every retired route and check removed imports.")
    args = parser.parse_args()
    root = args.root.expanduser().resolve()
    if args.phase == "install" and root.exists() and any(root.iterdir()):
        parser.error("accept.root: install requires an empty directory")
    proof = Proof(root)
    try:
        if args.phase == "install":
            proof.install(args.install_spec)
        elif args.phase == "retirement":
            proof.retirement()
        else:
            project = args.project.expanduser().resolve()
            require(proof.unbake.is_file(), "accept.install: run install first")
            if args.phase == "prepare":
                proof.policy(args.policy)
                proof.prepare(project, args.rom, args.names_from, args.compiler, args.supply)
            elif args.phase == "confirm":
                proof.confirm(project, args.token, args.supply)
            elif args.phase == "cycle":
                proof.cycle(project, args.source)
            elif args.phase == "verify":
                proof.verify(project)
            else:
                require(args.source_dir.is_dir(), "accept.source_dir: missing authored C directory")
                sources = sorted(args.source_dir.rglob("*.c"))
                require(sources, "accept.sources: no authored C inputs")
                for source in sources:
                    proof.cycle(project, source)
        print("PASS: " + args.phase)
        return 0
    except (ProofError, OSError, ValueError, KeyError, IndexError) as error:
        (proof.logs / "failure.json").write_text(
            json.dumps({"phase": args.phase, "reason": str(error)}, indent=2) + "\n"
        )
        print(f"HELD(accept): {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
