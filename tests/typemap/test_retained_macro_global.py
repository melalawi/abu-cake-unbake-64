"""A real published macro use must survive freshness and native consumer checks."""

import hashlib
import json
import shutil
import subprocess
from pathlib import Path
from unittest.mock import patch

from tests.project_fixture import ProjectCase
from unbake import effort, pool
from unbake.config import Held
from unbake.fold import imports
from unbake.layout import apply, header_loss, header_step, index, map
from unbake.process import named
from unbake.typemap import database, declaration_evidence, regeneration
from unbake.typemap.split import guarded, required_providers

FIXTURE = Path(__file__).parent / "fixtures/ragewars_retained_global"
FUNCTION = "func_8040184C_de"
GLOBAL = "D_800DCB38"
HOME = "span_16E000/code_80400000.h"
OPTIONS = json.loads((FIXTURE / "options.json").read_text())


class RetainedMacroGlobalTests(ProjectCase):
    versions = tuple(OPTIONS["versions"])

    def setUp(self):
        super().setUp()
        self.include = self.project.include[0]
        for path in (FIXTURE / "before/include").rglob("*.h"):
            relative = path.relative_to(FIXTURE / "before/include")
            target = self.include / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            body = path.read_text()
            if target.name != "types.h":
                body = '#include "../types.h"\n' + body
            target.write_bytes(guarded(relative, body))
        self.source = self.project.src / f"{FUNCTION}.c"
        self.source.write_bytes((FIXTURE / "src" / self.source.name).read_bytes())
        self.old = self.include / "common/unused.h"
        self.ownership = map.Map(
            32,
            (
                map.Group("code_80400000", "span_16E000", "default", (FUNCTION,)),
                map.Group("beta", "main", "default", ("beta",)),
            ),
        )
        ownership = patch.object(map, "load", return_value=self.ownership)
        ownership.start()
        self.addCleanup(ownership.stop)
        for version in self.versions:
            definition = self.project.version(version)
            definition.symbols.write_text(definition.symbols.read_text().replace("alpha", FUNCTION))
            definition.split.write_text(
                definition.split.read_text().replace("asm, alpha", f"c, {FUNCTION}").replace("asm, beta", "c, beta")
            )
        contents = {
            path.relative_to(self.include).as_posix(): path.read_text()
            for path in self.include.rglob("*.h")
            if path.name != "types.h"
        }
        manifest = index.path(self.project)
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_bytes(
            index.encoded(index.overlay({"schema": 1, "headers": {}, "symbols": {}, "clusters": {}}, contents))
        )
        self.compiles = []

    def value(self):
        value = {kind: {} for kind in ("functions", "globals", "arrays", "structs")}
        value["globals"][GLOBAL] = {
            "state": "known",
            "declaration": f"extern float {GLOBAL};",
            "provenance": [{"component": f"global:{GLOBAL}"}],
        }
        value["declaration_evidence"] = {".evidence_unrelated.h": (FIXTURE / "unrelated.h").read_text()}
        return value

    def view(self, outputs):
        include = self.root / "native-view"
        shutil.copytree(self.include, include, dirs_exist_ok=True)
        for path, data in outputs.items():
            if path.suffix == ".h":
                target = include / path.relative_to(self.include)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data.read_bytes() if isinstance(data, Path) else data)
        return include

    def rewritten(self, outputs):
        return apply.source(
            self.project,
            self.source.read_text(),
            FUNCTION,
            outputs,
            lookup=json.loads(outputs[index.path(self.project)]),
        ).encode()

    def native(self, source, version, include):
        preprocessed = subprocess.run(
            [
                "/usr/bin/cpp",
                *OPTIONS["cppflags"],
                *(f"-D{macro}" for macro in OPTIONS["versions"][version]),
                f"-I{include}",
                str(source),
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual(preprocessed.returncode, 0, preprocessed.stderr)
        compiled = subprocess.run(
            [
                "/usr/bin/gcc",
                "-m32",
                "-x",
                "c",
                "-std=c89",
                "-fsyntax-only",
                "-Werror=implicit-function-declaration",
                "-",
            ],
            input=preprocessed.stdout,
            capture_output=True,
            text=True,
        )
        self.compiles.append((source.stem, version))
        return preprocessed.stdout, compiled

    def test_exact_payload_digests(self):
        records = json.loads((FIXTURE / "provenance.json").read_text())["files"]
        for relative, record in records.items():
            path = FIXTURE / ("before" if relative.startswith("include/") else "") / relative
            self.assertEqual(
                hashlib.sha256(path.read_bytes()).hexdigest(), record.get("slice_sha256", record.get("sha256"))
            )

    def test_historical_proposal_is_a_real_native_loss_and_guard_refuses_before_install(self):
        moved = self.include / "span_C76B0/data.h"
        proposal = {
            self.old: guarded(Path("common/unused.h"), (FIXTURE / "proposal/include/common/unused.h").read_text()),
            moved: guarded(Path("span_C76B0/data.h"), (FIXTURE / "proposal/include/span_C76B0/data.h").read_text()),
        }
        after = self.view(proposal)
        for version in self.versions:
            with self.subTest(version=version):
                body, native = self.native(self.source, version, self.include)
                self.assertEqual(native.returncode, 0, native.stderr)
                self.assertIn(f"u = u * (u * {GLOBAL})", body)
                self.assertIn(f"(*(&{GLOBAL} + 1))", body)
                body, native = self.native(self.source, version, after)
                self.assertNotEqual(native.returncode, 0)
                self.assertIn(GLOBAL, native.stderr)
                self.assertNotIn(f"extern float {GLOBAL};", body)
        before = self.old.read_bytes()
        with self.assertRaisesRegex(
            Held, f"headers.merge_only: .*{FUNCTION}.*would lose reachable declarations {GLOBAL}"
        ):
            apply.install(self.project, proposal)
        self.assertEqual(self.old.read_bytes(), before)
        self.assertFalse(moved.exists())
        self.assertEqual(self.compiles, [(FUNCTION, version) for version in self.versions for _ in range(2)])

    def test_freshness_retains_macro_global_once_and_reuses_render_without_inventing_a_declaration(self):
        value = self.value()
        session = regeneration.Session(self.project, None)
        self.assertIn(f"extern float {GLOBAL};", "".join(session.published.values()))
        self.assertIn(self.old, set().union(*session.published_homes.values()))
        before = effort.counted()
        with patch.object(database, "_render", wraps=database._render) as render:
            outputs = session.render(value, lambda: database._render(self.project, value, None, session))
            second = session.render(value, lambda: self.fail("unchanged macro dependency rerendered"))
        self.assertEqual(render.call_count, 1)
        self.assertEqual(outputs, second)
        for kind in ("render.compute", "render.reused"):
            self.assertEqual(effort.counted()[kind][0] - before.get(kind, (0, 0))[0], 1)
        self.assertEqual(value["declaration_headers"][GLOBAL], HOME)
        body = "\n".join(data.decode() for path, data in outputs.items() if path.suffix == ".h")
        self.assertEqual(body.count(f"extern float {GLOBAL};"), 1)
        header_loss.check(self.project, outputs)
        # The reduced fixture moves complete type providers too. Follow the
        # real layout import projection before compiling the final C view.
        rewritten = self.rewritten(outputs)
        header_loss.check(self.project, {**outputs, self.source: rewritten})
        source = self.root / self.source.name
        source.write_bytes(rewritten)
        after = self.view(outputs)
        for version in self.versions:
            _, native = self.native(source, version, after)
            self.assertEqual(native.returncode, 0, native.stderr)
        self.assertEqual(self.compiles, [(FUNCTION, version) for version in self.versions])

    def test_proven_contradiction_still_refuses_the_real_carried_contract(self):
        value = self.value()
        value["globals"][GLOBAL].update(declaration=f"extern int {GLOBAL};", provenance={"kind": "proven"})
        before = self.old.read_bytes()
        with self.assertRaisesRegex(Held, f"headers.declaration: {GLOBAL}: published.*float.*incompatible.*int"):
            database._render(self.project, value, None, regeneration.Session(self.project, None))
        self.assertEqual(self.old.read_bytes(), before)

    def test_provider_closure_counts_macro_bodies_and_skips_non_code_spellings(self):
        providers = {GLOBAL: {self.old}}
        self.assertEqual(required_providers(self.source.read_text(), providers, {}, {}), {self.old})
        self.assertEqual(required_providers(self.source.read_text(), providers, {}, {}, {GLOBAL}), set())
        source = f'#include "{GLOBAL}.h"\n/* {GLOBAL} */\nconst char *message = "{GLOBAL} /*";\n'
        self.assertNotIn(GLOBAL, apply.spelled(source))
        self.assertEqual(required_providers(source, providers, {}, {}), set())
        retained, _ = declaration_evidence.published_snapshot(self.project, sources={self.source: source})
        self.assertNotIn(GLOBAL, "".join(retained.values()))
        continuation = f"#define THREE \\\n    {GLOBAL}\nfloat consumer(void) {{ return THREE; }}\n"
        self.assertEqual(required_providers(continuation, providers, {}, {}), {self.old})

    def test_header_validation_proves_every_consumer_and_holding_version_with_native_payloads(self):
        value = self.value()
        outputs = database._render(self.project, value, None, regeneration.Session(self.project, None))
        other = self.project.src / "beta.c"
        other.write_text(f'#include "{HOME}"\n#include "common/unused.h"\nfloat beta(void) {{ return {GLOBAL}; }}\n')
        headers = {path: data for path, data in outputs.items() if path.suffix == ".h"}
        headers[self.source] = self.rewritten(outputs)

        def compile_job(job):
            view, _host, source, version, unit, _prove, _fuzzy = job
            _, native = self.native(source, version, view.include[0])
            if native.returncode:
                return {"key": f"compile.{unit}.{version}", "reason": native.stderr}
            return None

        def run(_host, function, jobs, shared=None):
            return [function(job) if shared is None else function(shared, job) for job in jobs]

        with patch.object(pool, "run", side_effect=run), patch.object(header_step, "_compile", side_effect=compile_job):
            self.assertEqual(header_step.validate(self.project, self.host, headers), ["beta", FUNCTION])
            self.assertEqual(
                set(self.compiles), {(unit, version) for unit in (FUNCTION, "beta") for version in self.versions}
            )
            self.assertEqual(len(self.compiles), 2 * len(self.versions))
            self.compiles.clear()
            bad = other.read_text().replace(
                f"return {GLOBAL};",
                f"\n#ifdef VERSION_DE\nreturn {GLOBAL}.missing;\n#else\nreturn {GLOBAL};\n#endif\n",
            )
            with self.assertRaisesRegex(Held, "1 unit compiles fail.*beta.de"):
                header_step.validate(self.project, self.host, {**headers, other: bad.encode()})
            self.assertEqual(
                set(self.compiles), {(unit, version) for unit in (FUNCTION, "beta") for version in self.versions}
            )
            self.assertEqual(len(self.compiles), 2 * len(self.versions))

    def relocation(self):
        # Replay the actual held render proposal, including its measured data
        # home. Macro-retention alone puts the reduced fresh render in the
        # consumer module and does not reproduce this publication boundary.
        value = self.value()
        value.update(revision=1, dependencies={FUNCTION: [], "beta": []}, shared_aliases={})
        session = regeneration.Session(self.project, None)
        outputs = {
            self.include / path.relative_to(FIXTURE / "proposal/include"): guarded(
                path.relative_to(FIXTURE / "proposal/include"), path.read_text()
            )
            for path in (FIXTURE / "proposal/include").rglob("*.h")
        }
        contents = {path.relative_to(self.include).as_posix(): path.read_text() for path in self.include.rglob("*.h")}
        contents.update({path.relative_to(self.include).as_posix(): data.decode() for path, data in outputs.items()})
        lookup = index.overlay({"schema": 1, "headers": {}, "symbols": {}, "clusters": {}}, contents)
        outputs[index.path(self.project)] = index.encoded(lookup)
        value["declaration_headers"] = lookup["symbols"]
        self.assertEqual(value["declaration_headers"][GLOBAL], "span_C76B0/data.h")
        return value, session, outputs

    def test_real_segment_relocation_reconnects_only_the_affected_imports_once(self):
        _value, session, outputs = self.relocation()
        untouched = self.project.src / "beta.c"
        session.sources[untouched] = "int beta(void) { return 0; }\n"
        with patch("unbake.fold.imports.resolve", wraps=imports.resolve) as resolve:
            changes = database._consumer_imports(self.project, outputs, session.sources)
        self.assertEqual(resolve.call_count, 1)
        self.assertEqual(set(changes), {self.source})
        projected = changes[self.source].decode()
        self.assertIn('#include "span_C76B0/data.h"', projected)
        self.assertEqual(apply._INCLUDE.sub("", projected), apply._INCLUDE.sub("", self.source.read_text()))
        header_loss.check(self.project, {**outputs, **changes})
        self.assertEqual(database._consumer_imports(self.project, outputs, {self.source: projected}), {})
        native_source = self.root / self.source.name
        native_source.write_bytes(changes[self.source])
        after = self.view(outputs)
        for version in self.versions:
            _, compiled = self.native(native_source, version, after)
            self.assertEqual(compiled.returncode, 0, compiled.stderr)
        self.assertEqual(self.compiles, [(FUNCTION, version) for version in self.versions])

    def test_publish_atomically_reconnects_the_real_data_segment_before_native_consumers(self):
        value, session, outputs = self.relocation()
        before = self.source.read_bytes()

        def compile_job(job):
            view, _host, source, version, unit, _prove, _fuzzy = job
            self.assertEqual(self.source.read_bytes(), before)
            self.assertIn('#include "span_C76B0/data.h"', source.read_text())
            _, compiled = self.native(source, version, view.include[0])
            return None if compiled.returncode == 0 else {"key": f"compile.{unit}.{version}", "reason": compiled.stderr}

        def run(_host, function, jobs, shared=None):
            return [function(job) if shared is None else function(shared, job) for job in jobs]

        with (
            patch.object(database.regeneration, "Session", return_value=session),
            patch.object(session, "render", return_value=dict(outputs)) as render,
            patch.object(database, "validate_headers") as shared_headers,
            patch.object(pool, "run", side_effect=run),
            patch.object(header_step, "_compile", side_effect=compile_job) as compile_consumer,
            patch.object(database.types_db, "install", wraps=database.types_db.install) as install,
        ):
            database.publish(self.project, value, {}, policy=self.host)
        self.assertEqual(render.call_count, 1)
        self.assertEqual(shared_headers.call_count, 1)
        self.assertEqual(compile_consumer.call_count, len(self.versions))
        self.assertEqual(install.call_count, 1)
        self.assertEqual(self.compiles, [(FUNCTION, version) for version in self.versions])
        self.assertIn('#include "span_C76B0/data.h"', self.source.read_text())
        self.assertEqual(apply._INCLUDE.sub("", self.source.read_text()), apply._INCLUDE.sub("", before.decode()))
        self.assertEqual(database.types_db.read(database.types_db.path(self.project))["revision"], 1)

    def test_failed_native_reconnection_cannot_install_headers_sources_or_database(self):
        value, session, outputs = self.relocation()
        before = {path: path.read_bytes() for path in (self.source, self.old)}
        with (
            patch.object(database.regeneration, "Session", return_value=session),
            patch.object(session, "render", return_value=dict(outputs)),
            patch.object(database, "validate_headers"),
            patch.object(
                pool,
                "run",
                side_effect=lambda _host, function, jobs, shared=None: [
                    function(job) if shared is None else function(shared, job) for job in jobs
                ],
            ),
            patch.object(
                header_step,
                "validate",
                side_effect=Held(named("fixture.refusal", "native consumer refused", owner="fixture", stage="headers")),
            ) as native,
            patch.object(database.types_db, "stage") as stage,
            self.assertRaisesRegex(Held, "native consumer refused"),
        ):
            database.publish(self.project, value, {}, policy=self.host)
        self.assertEqual(native.call_count, 1)
        stage.assert_not_called()
        for path, data in before.items():
            self.assertEqual(path.read_bytes(), data)
        self.assertFalse((self.include / "span_C76B0/data.h").exists())
        self.assertFalse(database.types_db.path(self.project).exists())

    def test_absent_declaration_still_holds_without_synthesizing_a_provider(self):
        outputs = {self.old: b""}
        self.assertEqual(database._consumer_imports(self.project, outputs, {self.source: self.source.read_text()}), {})
        with self.assertRaisesRegex(Held, f"would remove {GLOBAL} used by published C"):
            header_loss.check(self.project, outputs)
