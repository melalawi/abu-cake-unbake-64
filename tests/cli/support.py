import hashlib
import importlib
import io
import shutil
import tempfile
import unittest
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock, patch

from tests.project.test_bootstrap import policy
from unbake.cli import main as cli
from unbake.project import config


class MainCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.root = self.directory / "project"
        shutil.copytree(Path(__file__).parents[1] / "fixture", self.root)
        self.project = config.load(self.root)
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
                        "match": "unbake.match.queue",
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
            code = cli.main(args)
        return (code, stdout.getvalue(), stderr.getvalue())

    def args(self, *operands: str) -> list[str]:
        return ["--project", str(self.root), *operands]

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
            {"us": Compare("us", 0, 1, {}, [])},
            [],
            "",
            [],
        )
        captured = {}

        def try_draft(project: Any, policy: Any, source: Any, scratch: Any, *, versions: Any) -> Any:
            captured["scratch"] = scratch
            captured["versions"] = versions
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
            "trial": self.module("trial", try_draft=Mock(side_effect=try_draft)),
            "score": self.module("score", fuzzy=fuzzy),
            "drafts": self.module("drafts", Store=Mock(return_value=store)),
        }
        return (modules, result, captured, target, fuzzy, store)
