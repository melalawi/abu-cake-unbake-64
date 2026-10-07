"""The real guarded BT owner retains its contract through regeneration."""

import copy
import hashlib
import json
from pathlib import Path
from unittest.mock import patch

from tests.preprocessor import output
from tests.project_fixture import ProjectCase
from tests.typemap.test_solver import facts
from unbake import cdecl, config, migrate_state, pool, process
from unbake.config import Held
from unbake.fold import self_prototype
from unbake.typemap import database, evidence, regeneration
from unbake.work import attempts

FIXTURE = Path(__file__).parents[1] / "fixtures/migration_legacy"
NAME = "func_800767E4_us"
RECORD = json.loads((FIXTURE / "767E4-contract.json").read_text())[NAME]


class PublishedOwnDefinitionTests(ProjectCase):
    versions = ("us",)

    def setUp(self):
        super().setUp()
        version = self.project.version("us")
        for path in (version.split, version.symbols, self.project.root / "layout.toml"):
            path.write_text(path.read_text().replace("beta", NAME))
        self.project = config.load(self.project.root)
        self.source = self.project.src / (NAME + ".c")
        self.text = (FIXTURE / self.source.name).read_text()
        self.source.write_text(self.text)
        migrate_state.apply(self.project, migrate_state.plan(self.project))

    def value(self):
        value = {kind: {} for kind in ("structs", "functions", "globals", "arrays")}
        value["functions"][NAME] = copy.deepcopy(RECORD)
        return value

    def render(self, value):
        with (
            patch.object(process.subprocess, "run", side_effect=output),
            patch.object(
                pool,
                "run",
                side_effect=lambda host, fn, jobs, shared=None: [
                    fn(job) if shared is None else fn(shared, job) for job in jobs
                ],
            ),
        ):
            session = regeneration.Session(self.project, self.host)
            return database._render(self.project, value, self.host, session)

    def test_real_registered_guarded_owner_regenerates_the_same_prototype_as_exact_fold(self):
        value = self.value()
        original = copy.deepcopy(value)
        reads = []
        read_text = Path.read_text

        def read(path, *args, **kwargs):
            if path == self.source:
                reads.append(path)
            return read_text(path, *args, **kwargs)

        with (
            patch("unbake.runner.compile_unit", side_effect=AssertionError("native replay")) as native,
            patch.object(Path, "read_text", read),
            patch.object(database, "_source_drops", wraps=database._source_drops) as rows,
            patch.object(self_prototype, "definition_prototypes", wraps=self_prototype.definition_prototypes) as parsed,
        ):
            outputs = self.render(value)
        self.assertEqual((len(reads), parsed.call_count, rows.call_count, native.call_count), (1, 1, 1, 0))
        rendered = "\n".join(data.decode() for path, data in outputs.items() if path.suffix == ".h")
        self.assertIn(f"void {NAME}(void);", rendered)
        self.assertNotIn(f"int {NAME}(void);", rendered)
        own = self_prototype.definition_prototypes(attempts.unguarded(self.text), {})[NAME]
        self.assertIn(own, rendered)
        header = self.project.include[0] / "own.h"
        folded = self_prototype.exact(
            {header: RECORD["prototype"]},
            frozenset({header}),
            attempts.unguarded(self.text),
            self.text,
            NAME,
            RECORD,
            {},
            self.versions,
            attempts.Attempt(
                "",
                NAME,
                hashlib.sha256(self.text.encode()).hexdigest(),
                len(self.text),
                {"us": {"exact": True}},
                100,
                True,
                0,
                "ido-7.1",
            ),
        )
        self.assertEqual([edit.after for edit in folded], [own])
        self.assertEqual(value["functions"], original["functions"])
        self.assertEqual(self.source.read_text(), self.text)
        self.assertEqual(native.call_count, 0)
        self.assertIsNone(attempts.ledger(self.project).fuzzy(NAME))
        self.assertEqual(attempts.ledger(self.project).fuzzy_sources()[NAME]["score"], None)

    def test_actual_native_return_consumption_holds_with_the_proof(self):
        machine = facts(
            {
                NAME: (0x80002000, [0x24020001, 0x03E00008, 0]),
                "caller": (0x80001000, [0x0C000800, 0, 0x24430001, 0x03E00008, 0]),
            }
        )
        value = self.value()
        value["functions"][NAME]["abi"] = evidence.abi(machine["functions"])[NAME]
        self.assertEqual(value["functions"][NAME]["abi"]["used_returns"], ["r2"])
        with self.assertRaises(Held) as caught:
            self.render(value)
        self.assertEqual(caught.exception.key, "types.own_contract")
        self.assertIn("caller", caught.exception.reason)
        self.assertIn("r2", caught.exception.reason)
        self.assertEqual(self.source.read_text(), self.text)

    def test_unregistered_guard_is_not_promoted_to_source_authority(self):
        self.source.write_text(self.text + "\n/* changed since retention */\n")
        rendered = "\n".join(data.decode() for path, data in self.render(self.value()).items() if path.suffix == ".h")
        self.assertNotIn(f"extern void {NAME}(void);", rendered)
        self.assertNotEqual(
            hashlib.sha256(self.source.read_bytes()).hexdigest(),
            attempts.ledger(self.project).fuzzy_sources()[NAME]["source_sha256"],
        )

    def test_shared_reader_parses_each_own_definition_once(self):
        with patch.object(cdecl, "parse", wraps=cdecl.parse) as parsed:
            result = self_prototype.definition_prototypes(attempts.unguarded(self.text), {})
        self.assertEqual(result, {NAME: f"void {NAME}(void);"})
        self.assertEqual(parsed.call_count, 1)

    def test_caller_hints_discarded_and_unproven_results_do_not_override_the_owner(self):
        (self.project.src / "gamma.c").write_text(f"extern int {NAME}(void); void gamma(void) {{ {NAME}(); }}\n")
        value = self.value()
        value["functions"][NAME]["abi"].update(
            caller_arguments=["r4"],
            caller_return_uses={"unknown_proxy": ["r2"]},
            unproven_return_reads=["r2"],
            used_returns=[],
        )
        rendered = "\n".join(data.decode() for path, data in self.render(value).items() if path.suffix == ".h")
        self.assertIn(f"void {NAME}(void);", rendered)

    def test_published_c_definition_is_authority_without_a_registered_guard(self):
        self.source.write_text(attempts.unguarded(self.text))
        version = self.project.version("us")
        version.split.write_text(version.split.read_text().replace("asm, " + NAME, "c, " + NAME))
        rendered = "\n".join(data.decode() for path, data in self.render(self.value()).items() if path.suffix == ".h")
        self.assertIn(f"void {NAME}(void);", rendered)

    def test_render_reuse_reads_and_parses_no_owner_again_and_native_change_invalidates(self):
        session = regeneration.Session(self.project, None)
        value = self.value()
        with patch.object(
            self_prototype, "definition_prototypes", wraps=self_prototype.definition_prototypes
        ) as definitions:
            first = session.render(value, lambda: database._render(self.project, value, None, session))
            read_text = Path.read_text

            def read(path, *args, **kwargs):
                self.assertNotEqual(path, self.source, "owner payload reread")
                return read_text(path, *args, **kwargs)

            with patch.object(Path, "read_text", read):
                second = session.render(value, lambda: self.fail("unchanged owner rerendered"))
        self.assertEqual(first, second)
        self.assertEqual(definitions.call_count, 1)
        machine = facts(
            {
                NAME: (0x80002000, [0x24020001, 0x03E00008, 0]),
                "caller": (0x80001000, [0x0C000800, 0, 0x24430001, 0x03E00008, 0]),
            }
        )
        value["functions"][NAME]["abi"] = evidence.abi(machine["functions"])[NAME]
        with self.assertRaisesRegex(Held, "native return consumption"):
            session.render(value, lambda: database._render(self.project, value, None, session))
