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

    def run(
        self, argv: list[str | Path], cwd: Path | None = None, *, env: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        command = [str(value) for value in argv]
        directory = cwd or self.root
        number = len(list(self.logs.glob("*.command.json"))) + 1
        while True:
            label = f"{number:04d}"
            try:
                with (self.logs / f"{label}.command.json").open("x") as reserved:
                    reserved.write(json.dumps({"argv": command, "cwd": str(directory), "status": "running"}) + "\n")
                break
            except FileExistsError:
                number += 1
        print(f"$ (cd {shlex.quote(str(directory))} && {shlex.join(command)})", flush=True)
        started = time.time()
        result = subprocess.run(
            command, cwd=directory, env=env or self.env, input="", capture_output=True, text=True, check=False
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
            "path": (env or self.env)["PATH"],
            "policy": (env or self.env)["UNBAKE_POLICY"],
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
        destination = self.root / "policy.toml"
        if not destination.is_file() or destination.read_text() != text:
            destination.write_text(text)

    def install(self, spec: str, *, update: bool = False) -> None:
        if update:
            require(self.python.is_file(), "accept.install: run the clean install first")
        else:
            require(not self.python.exists(), "accept.root: existing virtual environment")
            result = self.run([sys.executable, "-m", "venv", self.root / "venv"])
            require(result.returncode == 0, "accept.venv: creation failed")
        options = ["--force-reinstall", "--no-deps"] if update else []
        result = self.run([self.python, "-m", "pip", "install", *options, spec])
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
            "; assert r.files('unbake.project_tools').joinpath('README.ready.md').is_file()"
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
            any(
                key + ":" in output
                for key in ("setup.compiler_confirmation", "setup.compiler_candidate", "setup.compiler_mixed")
            ),
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

    def propose(self, project: Path, compilers: list[str], supply: Path | None) -> None:
        require(compilers, "accept.compilers: supply explicit REGION=ID choices")
        arguments: list[str | Path] = ["setup"]
        for choice in compilers:
            arguments.extend(["--compiler", choice])
        if supply:
            arguments.extend(["--supply", supply])
        output = self.cli(*arguments, cwd=project, status=1)
        require("setup.compiler_confirmation:" in output, "setup.proposal: choices remain unresolved")
        config = read_config(project)
        require(config["project"]["state"] == "awaiting-roms", "setup.proposal: project prematurely ready")
        proposal = project / config["paths"]["build"] / "setup/proposal.json"
        saved = self.logs / (project.name + ".proposal.json")
        shutil.copyfile(proposal, saved)
        print(f"Review {saved}\nConfirmation token: {hashlib.sha256(proposal.read_bytes()).hexdigest()}")

    def confirm(self, project: Path, token: str, supply: Path | None) -> None:
        arguments: list[str | Path] = ["setup", "--confirm", token]
        if supply:
            arguments.extend(["--supply", supply])
        self.cli(*arguments, cwd=project)
        require(read_config(project)["project"]["state"] == "ready", "setup.publication: project not ready")
        accepted = (project / "docs/setup/compiler.json").read_bytes()
        require(hashlib.sha256(accepted).hexdigest() == token, "setup.confirmation: published evidence differs")
        require(
            accepted == (self.logs / (project.name + ".proposal.json")).read_bytes(),
            "setup.confirmation: accepted bytes differ from the displayed proposal",
        )
        require(not list((project / read_config(project)["paths"]["src"]).rglob("*.c")), "setup.assembly: C supplied")
        layout = json.loads((project / read_config(project)["paths"]["build"] / "setup/layout.json").read_bytes())
        coverage = {}
        for version, row in layout["versions"].items():
            cursor = 0
            counts: dict[str, int] = {}
            functions = {item["name"] for item in row["functions"]}
            shared = set()
            for provider in sorted(row["providers"], key=lambda item: item["start"]):
                require(provider["start"] == cursor < provider["end"], f"layout.coverage.{version}: overlap or gap")
                cursor = provider["end"]
                kind = provider["kind"]
                counts[kind] = counts.get(kind, 0) + 1
                if kind == "text":
                    require(provider["evidence"]["assembly"], f"layout.text.{version}: non-assembly initial provider")
                elif kind == "private":
                    require(
                        len(provider["owners"]) == 1 and provider["owners"][0] in functions,
                        f"layout.owner.{version}: private pool has no unique function",
                    )
                elif kind == "shared":
                    shared.add(provider["evidence"]["logical_provider"])
                if provider["evidence"].get("safe_sole_candidate"):
                    require(kind == "private", f"layout.owner.{version}: safely private pool was not carved")
            size = (project / read_config(project)["version"][version]["baserom"]).stat().st_size
            require(cursor == size, f"layout.coverage.{version}: incomplete ROM coverage")
            require(len(shared) <= 1, f"layout.shared.{version}: shared logical provider is not explicit")
            coverage[version] = {"bytes": cursor, "providers": counts, "shared_provider": sorted(shared)}
        (self.logs / (project.name + ".coverage.json")).write_text(json.dumps(coverage, indent=2) + "\n")
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
            require(result.stderr.count("Next: ") == 1, "rodata.next: expected exactly one stderr receipt")
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
        standalone = dict(self.env)
        standalone["PATH"] = os.pathsep.join(
            entry for entry in self.env["PATH"].split(os.pathsep) if not (Path(entry) / "unbake").exists()
        )
        standalone["PYTHON"] = str(self.python)
        require(shutil.which("unbake", path=standalone["PATH"]) is None, "make.standalone: unbake remains on PATH")
        result = self.run(["make", "check"], project, env=standalone)
        require(result.returncode == 0, "make.standalone: proof failed with unbake removed from PATH")

    def refusals(self, rom: str, other_rom: Path, policy: Path) -> None:
        require("=" in rom, "accept.rom: expected VERSION=FILE")
        version, filename = rom.split("=", 1)
        source = Path(filename).resolve()
        require(source.is_file() and other_rom.is_file(), "accept.rom: missing refusal ROM input")
        self.policy(policy)
        valid_policy = (self.root / "policy.toml").read_text()
        workspace = self.root / "refusals"
        require(not workspace.exists(), "accept.refusals: expected fresh proof directory")
        workspace.mkdir()

        def shell(name: str) -> Path:
            project = workspace / name
            self.cli("init", project)
            shutil.copyfile(source, project / "roms/input.z64")
            return project

        def pending(project: Path, key: str, *arguments: str) -> None:
            output = self.cli("setup", *arguments, cwd=project, status=1)
            require(key + ":" in output, f"accept.refusal: expected {key}")
            config = read_config(project)
            require(config["project"]["state"] == "awaiting-roms", "accept.refusal: project became ready")
            require("compilers" not in config and "units" not in config, "accept.refusal: compiler choices published")

        protected = workspace / "nonempty"
        protected.mkdir()
        (protected / "owner.txt").write_text("preserve this file\n")
        output = self.cli("init", protected, status=1)
        require("init.target:" in output, "init.target: nonempty refusal missing")
        require((protected / "owner.txt").read_text() == "preserve this file\n", "init.target: owner file changed")
        target = workspace / "symlink-target"
        target.mkdir()
        link = workspace / "symlink"
        link.symlink_to(target, target_is_directory=True)
        output = self.cli("init", link, status=1)
        require("init.target:" in output and not any(target.iterdir()), "init.target: symlink input not protected")
        pending(shell("no-naming-version"), "project.names_from")
        duplicate = shell("duplicate")
        shutil.copyfile(source, duplicate / "roms/duplicate.z64")
        pending(duplicate, "setup.roms.duplicate_sha1", "--names-from", version)
        mixed = shell("mixed")
        shutil.copyfile(other_rom, mixed / "roms/other.z64")
        pending(mixed, "setup.same_game.game_code", "--names-from", version)
        invalid = shell("bad-magic")
        (invalid / "roms/input.z64").write_bytes(b"bad input")
        output = self.cli("setup", "--names-from", version, cwd=invalid, status=1)
        require("rom.magic:" in output or "rom.size:" in output, "rom.input: malformed dump not named")
        crc = shell("bad-crc")
        damaged = bytearray(source.read_bytes())
        require(len(damaged) > 0x2000, "accept.rom: input too small for CRC protection proof")
        damaged[0x2000] ^= 1
        (crc / "roms/input.z64").write_bytes(damaged)
        output = self.cli("setup", "--names-from", version, cwd=crc, status=1)
        require("crc" in output.lower() and "HELD(" in output, "rom.crc: corrupt input not refused")
        try:
            for field in ("splat", "mips_as", "mips_ld", "mips_objcopy", "cpp", "cache_root"):
                modified, count = re.subn(r"(?m)^" + field + r"\s*=.*\n?", "", valid_policy)
                require(count == 1, f"policy.{field}: expected explicit input for omission test")
                (self.root / "policy.toml").write_text(modified)
                pending(shell("missing-" + field), "policy." + field, "--names-from", version)
            (self.root / "policy.toml").unlink()
            pending(shell("missing-policy"), "policy.cache_root", "--names-from", version)
            require((self.root / "policy.toml").is_file(), "policy.template: missing policy not created")
        finally:
            (self.root / "policy.toml").write_text(valid_policy)

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

    def map_solve(self, project: Path) -> None:
        self.cli("map", cwd=project)
        config = read_config(project)
        facts = json.loads((project / config["paths"]["build"] / "map/facts.json").read_bytes())
        require(set(facts["rom_sha1"]) == set(config["version"]), "map.versions: missing configured ROM")
        require(facts["functions"], "map.functions: missing whole-program items")
        self.cli("solve", cwd=project)
        database = project / config["paths"]["build"] / "types/database.json"
        require(database.is_file(), "solve.database: missing shared type database")
        for name in ("typemap.h", "prototypes.h"):
            require((project / "include/shared" / name).is_file(), f"solve.header: missing {name}")

    def measure(self, project: Path, label: str, fuzzy_bar: float | None) -> None:
        config = read_config(project)
        facts = json.loads((project / config["paths"]["build"] / "map/facts.json").read_bytes())
        with (self.root / "policy.toml").open("rb") as stream:
            policy = tomllib.load(stream)
        state = Path(policy["state_root"]) / config["project"]["id"] / config["workspace"]["id"]
        ledger = state / "drafts/trials.jsonl"
        trials = {}
        if ledger.is_file():
            for line in ledger.read_text().splitlines():
                row = json.loads(line)
                trials[row["function"]] = row
        attempts = []
        for path in self.logs.glob("*.command.json"):
            row = json.loads(path.read_text())
            if row.get("cwd") == str(project) and "try" in row["argv"] and "returncode" in row:
                text = (self.logs / row["stdout"]).read_text()
                attempts.append(row["returncode"] == 0 and "retained " in text and "HELD(" not in text)
        sources = {path.stem for path in (project / config["paths"]["src"]).rglob("*.c")}
        items = facts["functions"]
        matched = {name for name, item in items.items() if sources & {name, *item["aliases"]}}
        fuzzy = None if fuzzy_bar is None else sum(min(row["score"].values()) >= fuzzy_bar for row in trials.values())
        result = {
            "label": label,
            "project": str(project),
            "items": len(items),
            "compile_attempts": len(attempts),
            "compile_ok": sum(attempts),
            "compile_ok_percent": 100 * sum(attempts) / len(attempts) if attempts else None,
            "trial_items": len(trials),
            "owner_fuzzy_bar": fuzzy_bar,
            "owner_fuzzy_pass_percent": 100 * fuzzy / len(trials) if fuzzy is not None and trials else None,
            "matched_items": len(matched),
            "matched_percent": 100 * len(matched) / len(items),
            "versions": {
                version: {
                    "items": sum(version in item["versions"] for item in items.values()),
                    "matched": sum(version in items[name]["versions"] for name in matched),
                }
                for version in config["version"]
            },
        }
        (self.logs / (project.name + "." + label + ".metrics.json")).write_text(json.dumps(result, indent=2) + "\n")
        print(json.dumps(result, indent=2))
        if fuzzy_bar is None:
            print("GAP: owner.fuzzy_bar is missing. No threshold was inferred.")

    @staticmethod
    def clean_source(path: Path) -> None:
        text = path.read_text()
        for key, pattern in (
            ("raw_gfx", r"\bwords\s*\.\s*w[01]\s*="),
            ("local_macro", r"(?m)^\s*#\s*define\s+(?:_SHIFTL|g[sd]SP\w*|g[sd]DP\w*)\b"),
            ("local_type", r"\btypedef\b|\bfunc_\w+_S\d+\b"),
        ):
            require(not re.search(pattern, text), f"accept.{key}: {path}")

    def cycle(
        self, project: Path, source: Path | None = None, function: str | None = None, *, subset: bool = False
    ) -> None:
        config = read_config(project)
        type_root = project / config["paths"]["build"] / "types"
        revision = json.loads((type_root / "database.json").read_bytes())["revision"]
        if subset:
            facts = json.loads((project / config["paths"]["build"] / "map/facts.json").read_bytes())
            candidates = {
                name: row
                for name, row in facts["functions"].items()
                if config["project"]["names_from"] not in row["versions"]
            }
            require(candidates, "accept.subset: no item absent from the naming version")
            if function is None:
                function = min(
                    candidates,
                    key=lambda name: (
                        min(row["end"] - row["start"] for row in candidates[name]["versions"].values()),
                        name,
                    ),
                )
            require(function in candidates, "accept.subset: supplied item is present in the naming version")
            print(f"Subset item: {function}. Holding versions: {', '.join(candidates[function]['versions'])}")
        output = self.cli("next", cwd=project)
        suggestion = next(line.removeprefix("Next: ") for line in output.splitlines() if line.startswith("Next: "))
        tokens = shlex.split(suggestion)
        require(tokens and tokens[0] == "unbake" and "draft" in tokens, "next.action: expected executable draft")
        if source is not None:
            tokens = ["unbake", "draft", source.stem]
        if function is not None:
            tokens = ["unbake", "draft", function]
        before = self.canonical(project)
        output = self.cli(*tokens[1:], cwd=project)
        action = next(line.removeprefix("Next: ") for line in output.splitlines() if line.startswith("Next: "))
        arguments = shlex.split(action)
        require("try" in arguments, "draft.action: expected printed try command")
        draft = Path(arguments[arguments.index("try") + 1])
        if not draft.is_absolute():
            draft = project / draft
        require(draft.is_file(), f"draft.source: missing file {draft}")
        self.clean_source(draft)
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
        published = project / read_config(project)["paths"]["src"] / draft.name
        require(published.is_file(), "submit.source: missing published C")
        self.clean_source(published)
        feedback = json.loads((type_root / "proven.json").read_bytes())["records"][draft.stem]
        require(
            feedback["source_sha256"] == hashlib.sha256(published.read_bytes()).hexdigest(),
            "solve.feedback: published source differs from proven seed",
        )
        require(feedback["proof"]["matched"] is True, "solve.feedback: missing exact proof")
        database = json.loads((type_root / "database.json").read_bytes())
        require(database["revision"] > revision, "solve.feedback: submit did not re-solve")
        marks = json.loads((type_root / "redraft.json").read_bytes())["functions"]
        print(f"Submit feedback: revision {revision} -> {database['revision']}. Redraft marks: {len(marks)}")
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
            "import unbake.cli.rodata as r, unbake.decomp.similar as s, "
            "unbake.decomp.declarations as d, unbake.match.free as f; "
            "assert all(not hasattr(module,'main') for module in (r,s,d,f))"
        )
        result = self.run([self.python, "-c", code])
        require(result.returncode == 0, "retire.imports: retired API remains")

    def resubmit(self, project: Path, source_dir: Path) -> None:
        require(source_dir.is_dir(), "accept.source_dir: missing authored C directory")
        sources = sorted(source_dir.rglob("*.c"))
        require(sources, "accept.sources: no authored C inputs")
        require(len({source.stem for source in sources}) == len(sources), "accept.sources: duplicate function names")
        config = read_config(project)
        drafts = project / config["paths"]["drafts"]
        summary_path = self.logs / (project.name + ".resubmit.json")
        rows = json.loads(summary_path.read_text()) if summary_path.exists() else {}
        for source in sources:
            original = source.read_bytes()
            digest = hashlib.sha256(original).hexdigest()
            previous = rows.get(source.stem, {})
            published = project / config["paths"]["src"] / (source.stem + ".c")
            if (
                previous.get("status") == "passed"
                and previous.get("input_sha256") == digest
                and published.is_file()
                and hashlib.sha256(published.read_bytes()).hexdigest() == previous.get("published_sha256")
            ):
                continue
            copied = drafts / ("resubmit-" + source.stem) / source.name
            copied.parent.mkdir(parents=True, exist_ok=True)
            copied.write_bytes(original)
            record = {"input": str(source), "input_sha256": digest, "copy": str(copied)}
            before = self.canonical(project)
            try:
                self.cli("try", copied, cwd=project)
                require(self.canonical(project) == before, "trial.overlay: canonical files changed")
                self.cli("submit", copied, cwd=project)
                require(published.is_file(), "submit.source: canonical C missing after publication")
                record.update(status="passed", published_sha256=hashlib.sha256(published.read_bytes()).hexdigest())
            except ProofError as error:
                require(self.canonical(project) == before, "submit.refusal: canonical files changed")
                record.update(status="held", reason=str(error))
            require(source.read_bytes() == original, "accept.source: authored input changed")
            rows[source.stem] = record
            summary_path.write_text(json.dumps(rows, indent=2) + "\n")
        failures = [name for name, row in rows.items() if row["status"] != "passed"]
        self.verify(project)
        require(not failures, f"accept.resubmit: {len(failures)} held inputs, see {summary_path}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True, help="Evidence and clean installed environment directory.")
    phases = parser.add_subparsers(dest="phase", required=True)
    install = phases.add_parser("install", help="Create a clean venv and pip install an explicit immutable source.")
    install.add_argument("--install-spec", required=True, help="Pinned Git URL or immutable source directory.")
    update = phases.add_parser("update-install", help="Record a pinned follow-up install after integration fixes.")
    update.add_argument("--install-spec", required=True)
    prepare = phases.add_parser("prepare", help="Init a shell, copy supplied ROMs and display the setup proposal.")
    prepare.add_argument("--policy", type=Path, required=True)
    prepare.add_argument("--rom", action="append", required=True, metavar="VERSION=FILE")
    prepare.add_argument("--names-from", required=True)
    prepare.add_argument("--compiler", action="append", default=[], metavar="REGION=ID")
    prepare.add_argument("--supply", type=Path)
    confirm = phases.add_parser("confirm", help="Accept an explicitly reviewed token and prove every ROM.")
    confirm.add_argument("--token", required=True)
    confirm.add_argument("--supply", type=Path)
    propose = phases.add_parser("propose", help="Revise a pending proposal using explicit reviewed compiler choices.")
    propose.add_argument("--compiler", action="append", required=True, metavar="REGION=ID")
    propose.add_argument("--supply", type=Path)
    cycle = phases.add_parser("cycle", help="Execute next/draft/try/submit and refuse a changed untried source.")
    cycle.add_argument("--source", type=Path, help="Reviewed matching C to copy into the generated draft.")
    cycle.add_argument("--function", help="Explicit evidenced item, including a subset-version item.")
    cycle.add_argument("--subset", action="store_true", help="Prove an item absent from the naming version.")
    mapping = phases.add_parser("map-solve", help="Map all ROMs and solve one shared type database.")
    measure = phases.add_parser("measure", help="Record observed compile, fuzzy and matched round measures.")
    measure.add_argument("--label", required=True)
    measure.add_argument("--fuzzy-bar", type=float, help="Explicit owner threshold on the weakest version score.")
    resubmit = phases.add_parser(
        "resubmit", help="Re-submit each read-only authored C input through public work commands."
    )
    resubmit.add_argument("--source-dir", type=Path, required=True)
    verify = phases.add_parser("verify", help="Check digests, generated docs, hygiene and repeat setup.")
    for command in (prepare, propose, confirm, cycle, resubmit, verify, mapping, measure):
        command.add_argument("--project", type=Path, required=True)
    phases.add_parser("retirement", help="Refuse every retired route and check removed imports.")
    refusals = phases.add_parser("refusals", help="Prove named input refusals and pending-state protection.")
    refusals.add_argument("--rom", required=True, metavar="VERSION=FILE")
    refusals.add_argument("--other-rom", type=Path, required=True)
    refusals.add_argument("--policy", type=Path, required=True)
    args = parser.parse_args()
    root = args.root.expanduser().resolve()
    if args.phase == "install" and root.exists() and any(root.iterdir()):
        parser.error("accept.root: install requires an empty directory")
    proof = Proof(root)
    try:
        if args.phase in ("install", "update-install"):
            proof.install(args.install_spec, update=args.phase == "update-install")
        elif args.phase == "retirement":
            proof.retirement()
        elif args.phase == "refusals":
            proof.refusals(args.rom, args.other_rom, args.policy)
        else:
            project = args.project.expanduser().resolve()
            require(proof.unbake.is_file(), "accept.install: run install first")
            if args.phase == "prepare":
                proof.policy(args.policy)
                proof.prepare(project, args.rom, args.names_from, args.compiler, args.supply)
            elif args.phase == "confirm":
                proof.confirm(project, args.token, args.supply)
            elif args.phase == "propose":
                proof.propose(project, args.compiler, args.supply)
            elif args.phase == "cycle":
                proof.cycle(project, args.source, args.function, subset=args.subset)
            elif args.phase == "map-solve":
                proof.map_solve(project)
            elif args.phase == "measure":
                proof.measure(project, args.label, args.fuzzy_bar)
            elif args.phase == "verify":
                proof.verify(project)
            else:
                proof.resubmit(project, args.source_dir)
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
