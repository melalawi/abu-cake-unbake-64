"""Tiny locally allocated functions still reach structural search proposals."""

import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import patch

from tests.search.test_order_schedule import project_fixture
from unbake.decomp import explain, trial, trial_compile
from unbake.decomp.trial_compare import Compare
from unbake.families.gcc.allocation import allocation as gcc_allocation
from unbake.project import toolchain
from unbake.project.config import Held, Policy, Project
from unbake.search import order
from unbake.search.core import Context


class EmptyAllocationOrderTests(unittest.TestCase):
    def test_empty_global_order_allows_tiny_function_mutations(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = cast(Project, project_fixture(root / "project"))
            source = root / "f.c"
            source.write_text("int f(unsigned char *base, int index){return (base + index)[24];}")
            policy = cast(Policy, SimpleNamespace(state_root=root / "state"))
            proof = trial.Trial("f", "0" * 64, {"us": Compare("us", 4, 2, {}, [])}, [], "", [])
            for ranking in ("", ";; 0 regs to allocate:\n"):
                with self.subTest(ranking=ranking):

                    def diagnostics(command: list[str], work: Path, phase: str, ranking: str = ranking) -> str:
                        if "-o" in command:
                            (work / "source.lreg").write_text(
                                ";; Function f\nRegister 80 used 2 times across 4 insns; GR_REGS or none.\n"
                            )
                            (work / "source.greg").write_text(
                                ";; Function f\n" + ranking + ";; Register dispositions:\n80 in 2\n"
                            )
                        return source.read_text()

                    with (
                        patch.object(toolchain, "verify", return_value={}),
                        patch.object(trial_compile, "run_tool", side_effect=diagnostics),
                        patch.object(trial, "try_draft", return_value=proof),
                    ):
                        allocation = explain.allocation(project, policy, source, "us")
                    self.assertEqual([(p.rank, p.allocator) for p in allocation.pseudos], [(None, "local")])
                    self.assertIn("Global allocation order is empty; no pseudos to rank.", explain.render(allocation))
                    context = Context(project, policy, root, source, allocation, (), time.monotonic() + 30)
                    mutations = list(order.propose(source.read_text(), proof, context))
                    self.assertEqual(len(mutations), 1)
                    self.assertIn("index + base", mutations[0].source)
            for missing in ("lreg", "greg"):
                with self.subTest(missing=missing):

                    def absent(command: list[str], work: Path, phase: str, missing: str = missing) -> str:
                        diagnostics(command, work, phase)
                        if "-o" in command:
                            (work / ("source." + missing)).unlink()
                        return source.read_text()

                    with (
                        patch.object(toolchain, "verify", return_value={}),
                        patch.object(trial_compile, "run_tool", side_effect=absent),
                        patch.object(trial, "try_draft", return_value=proof),
                        self.assertRaisesRegex(Held, "dumps\\." + missing),
                    ):
                        explain.allocation(project, policy, source, "us")

            for global_text in (
                ";; Register dispositions:\n\n",
                ";; 80 conflicts: 2\n;; Register dispositions:\n80 in 2\n",
            ):
                with self.subTest(incomplete=global_text), self.assertRaisesRegex(Held, r"dumps\.greg\.order"):
                    gcc_allocation({"lreg": "Register 80 used 2 times across 4 insns", "greg": global_text})
            for expression in ("base + index++", "base + next()"):
                with self.subTest(side_effect=expression):
                    unsafe = "int next(void); " + source.read_text().replace("base + index", expression)
                    self.assertEqual(list(order.propose(unsafe, proof, context)), [])
