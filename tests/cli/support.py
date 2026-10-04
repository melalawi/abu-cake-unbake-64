import hashlib
import importlib
import io
import shutil
import tempfile
import unittest
from contextlib import ExitStack, contextmanager, redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock, patch

from tests.project.test_bootstrap import policy
from unbake.cli import guidance
from unbake.cli import main as cli
from unbake import config


class MainCase(unittest.TestCase):
    real_guidance = False

    def setUp(self) -> None:
        from tests.process_fakes import compiler_registry

        compiler_registry(self)
        import subprocess

        from tests.process_fakes import boundary, git_init
        from unbake.project import hygiene, init

        for mock in (
            boundary(init, git_init),
            boundary(hygiene, lambda command, **kwargs: subprocess.CompletedProcess(command, 0, b"", b"")),
        ):
            mock.start()
            self.addCleanup(mock.stop)
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name).resolve()
        self.root = self.directory / "project"
        shutil.copytree(Path(__file__).parents[1] / "fixture", self.root)
        self.project = config.load(self.root)
        from unbake.layout import map as ownership

        def synthetic_map(selected):
            members = ownership.catalog(selected)
            return ownership.Map(
                2, tuple(ownership.Group(m.name, m.segment, "default", (m.name,)) for m in members.values())
            )

        mapping = patch.object(ownership, "load", side_effect=synthetic_map)
        mapping.start()
        self.addCleanup(mapping.stop)

        self.project.roms.mkdir(exist_ok=True)
        self.policy = policy(self.directory)
        self.scratch = self.directory / "scratch"
        self.source = self.root / "src/alpha.c"
        self.source.parent.mkdir(exist_ok=True)
        self.source.write_text("int alpha(void) { return 1; }\n")

    def module(self, name: str, **members: Any) -> dict[str, Any]:
        return members

    def run_main(
        self, args: list[str], modules: dict[str, dict[str, Any]] | None = None, *, load_project: bool = True
    ) -> tuple[int, str, str]:
        stdout, stderr = (io.StringIO(), io.StringIO())
        with ExitStack() as stack:
            stack.enter_context(redirect_stdout(stdout))
            stack.enter_context(redirect_stderr(stderr))
            for name, members in (modules or {}).items():
                module = importlib.import_module(
                    {
                        "setup": "unbake.project.setup",
                        "init": "unbake.project.init",
                        "split": "unbake.layout.split",
                        "report": "unbake.report.progress",
                    }.get(name, f"unbake.decomp.{name}")
                )
                for member, replacement in members.items():
                    owner = module
                    if name == "split":
                        owner = importlib.import_module(
                            "unbake.layout.split_apply" if member in ("diff", "apply") else "unbake.layout.split_edits"
                        )
                    stack.enter_context(patch.object(owner, member, replacement))
            if load_project:
                stack.enter_context(patch.object(config, "load", return_value=self.project))
                stack.enter_context(patch.object(config, "load_policy", return_value=self.policy))
                if not self.real_guidance:
                    resolve = guidance.resolve

                    def isolated(root: Path | None, *, missing: str | None = None, retry: str = "unbake setup") -> str:
                        if missing is not None:
                            return resolve(root, missing=missing, retry=retry)
                        return guidance.command(root, "next")

                    stack.enter_context(patch.object(guidance, "resolve", side_effect=isolated))
            code = cli.main(args)
        output, error = stdout.getvalue(), stderr.getvalue()
        next_lines = [line for line in (output + error).splitlines() if line.startswith("Next: ")]
        self.assertEqual(len(next_lines), 1, output + error)
        self.assertTrue((error if "Next: " in error else output).splitlines()[-1].startswith("Next: "))
        return (code, output, error)

    def args(self, *operands: str) -> list[str]:
        return [
            "--project",
            str(self.root),
            *operands,
            *(
                ["--scratch", str(self.scratch)]
                if operands and operands[0] == "try" and "--scratch" not in operands
                else []
            ),
        ]

    def trial_modules(self, *, change_generation: bool = False, omit_artifact: bool = False) -> Any:
        generation = self.root / "build/us.1"
        generation.mkdir(parents=True)
        target = generation / "fixture.elf"
        target.write_bytes(b"original elf")
        (self.root / "build/us").symlink_to(generation.name)
        from unbake.decomp.trial import Trial
        from unbake.decomp.trial_compare import Compare

        result = Trial(
            "alpha",
            hashlib.sha256(self.source.read_bytes()).hexdigest(),
            {"us": Compare("us", 0, 1, {}, [], 88.5, ())},
            [],
            "",
        )
        captured = {}

        @contextmanager
        def inputs(project: Any, function: str, versions: list[str]) -> Any:
            from unbake.project import build

            with ExitStack() as holds:
                with build.lock(project):
                    pinned = {}
                    for v in versions:
                        directory = self.root / "build" / (v + ".1")
                        directory.mkdir(exist_ok=True)
                        pinned[v] = (holds.enter_context(build.pin(directory)), target)
                yield pinned

        def try_draft(
            project: Any, policy: Any, source: Any, scratch: Any, *, versions: Any, pinned: Any, function: Any = None
        ) -> Any:
            captured["scratch"] = scratch
            captured["versions"] = versions
            captured["function"] = function or source.stem
            result.generations = {v: g for v, (g, _) in pinned.items()}
            if not omit_artifact:
                version_dir = scratch / "alpha.unique/us"
                version_dir.mkdir(parents=True)
                (version_dir / "trial.elf").write_bytes(b"draft elf")
                (version_dir / "baserom.bin").write_bytes(bytes.fromhex("24020001 03e00008 00000000"))
                (version_dir / "draft.bin").write_bytes(bytes.fromhex("24020002 03e00008 00000000"))
            if change_generation:
                link = self.root / "build/us"
                link.unlink()
                link.symlink_to("us.2")
            return result

        fuzzy = Mock(return_value=88.5)
        store = SimpleNamespace(add=Mock(return_value=result.source_sha256))
        modules = {
            "trial": self.module("trial", try_draft=Mock(side_effect=try_draft), trial_inputs=inputs),
            "score": self.module("score", fuzzy=fuzzy),
            "drafts": self.module("drafts", Store=Mock(return_value=store)),
            "work": self.module("work", identity=Mock(return_value={"subject": "alpha"}), persist=Mock()),
        }
        return (modules, result, captured, target, fuzzy, store)
