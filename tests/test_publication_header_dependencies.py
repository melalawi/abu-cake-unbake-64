"""Real b1520 imports recover only exact bytes owned by the current manifest."""

import json
import os
import shutil
from pathlib import Path
from unittest.mock import patch

from tests.project_fixture import ProjectCase
from unbake import atomic as atomic_files
from unbake import inputs, land, process
from unbake.cache import Cache
from unbake.cdecl import parse
from unbake.config import Held
from unbake.fold.apply import Folded
from unbake.layout import header_step, index
from unbake.layout.header_context import Headers
from unbake.project import header_dependencies
from unbake.typemap import storage

FIXTURE = Path(__file__).parent / "fixtures/publication_headers/staging"
MISSING = "common/types_d507c48987bb.h"
INSTALLED = "common/types_f8bfabebf96f.h"
SPAN = "span_1000/code_800F45C8.h"
SOURCE = f'#include "{SPAN}"\nint alpha(void) {{ return 1; }}\n'
RENDER = "e881cdab12eb320403b3a38cedaacc3ffb5a336440ebb69df02eac46c297499b"


class PublicationHeaderDependenciesTests(ProjectCase):
    versions = ("us",)

    def setUp(self):
        super().setUp()
        self.include = self.project.include[-1]
        for source in (FIXTURE / "include").rglob("*.h"):
            target = self.include / source.relative_to(FIXTURE / "include")
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
        self.lookup = json.loads((FIXTURE / "index.json").read_text())
        atomic_files.write(storage.output(index.path(self.project)), index.encoded(self.lookup))
        self.missing = self.include / MISSING
        self.payload = (FIXTURE / "missing-owned.h").read_text()
        self.assertEqual(
            inputs.bytes_digest(self.payload.encode(), algorithm="sha256"), self.lookup["headers"][MISSING]
        )
        self.artifact = Cache(self.project.cache).path("typemap-render", RENDER)
        self.cache_payload(self.payload)
        self.built = []

    def cache_payload(self, text):
        atomic_files.write(
            storage.output(self.artifact),
            storage.encoded(
                {"outputs": {f"include/{MISSING}": text, "include/common/unrelated.h": "#error stale header\n"}}
            ),
        )

    def native(self, view, file):
        dependencies = file.parent / "cpp.d"
        result = process.run_native(
            [
                "/usr/bin/cpp",
                "-P",
                "-undef",
                "-nostdinc",
                "-DNON_MATCHING=1",
                "-MMD",
                "-MF",
                str(dependencies),
                *(f"-I{root}" for root in view.include),
                str(file),
            ],
            self.project.root,
            "compile",
        )
        self.assertTrue(parse(result.stdout).ext)
        paths = dependencies.read_text().replace("\\\n", "").partition(":")[2].split()
        self.built.append(view)
        return {Path(os.path.abspath(path)) for path in paths if Path(os.path.abspath(path)) != file}

    def fuzzy_jobs(self, host, fn, jobs):
        result = []
        for _, view, _, _function, file, _version in jobs:
            try:
                dependencies = self.native(view, file)
            except Held as error:
                result.append(({"compiled": False, "reason": str(error)}, set()))
            else:
                result.append(({"compiled": True, "percent": 99.0, "exact": False}, dependencies))
        return result

    def proof(self, source=SOURCE, headers=None, fuzzy=True):
        def exact(project, host, view, function, file, versions):
            return self.native(view, file)

        with (
            patch("unbake.pool.run", side_effect=self.fuzzy_jobs),
            patch.object(land, "_prove_versions", side_effect=exact),
            patch.object(header_step, "validate") as validate,
        ):
            proof = land.prove(
                self.project,
                self.host,
                "alpha",
                source,
                headers or {},
                self.project.work / "_land/alpha",
                versions=("us",),
                fuzzy=fuzzy,
            )
        return proof, validate

    def test_real_missing_payload_uses_the_existing_home_in_exact_and_fuzzy_native_views(self):
        span = (self.include / SPAN).read_text().replace(MISSING, INSTALLED)
        for fuzzy in (False, True):
            with self.subTest(fuzzy=fuzzy):
                proof, validate = self.proof(fuzzy=fuzzy)
                self.assertEqual(proof.dependency_headers, {SPAN: span})
                self.assertIn(self.include / INSTALLED, proof.dependencies)
                self.assertNotIn(self.missing, proof.dependencies)
                stage = self.project.work / "_land/alpha/include"
                self.assertEqual((stage / SPAN).read_text(), span)
                self.assertEqual((stage / INSTALLED).read_bytes(), (FIXTURE / "include" / INSTALLED).read_bytes())
                self.assertFalse((stage / MISSING).exists())
                self.assertFalse(self.missing.exists())
                self.assertEqual(validate.call_args.args[2][self.include / SPAN], span.encode())
                self.assertFalse((stage / "common/unrelated.h").exists())

    def test_missing_payload_without_an_installed_copy_is_recovered_exactly(self):
        (self.include / INSTALLED).unlink()
        proof, validate = self.proof()
        self.assertEqual(proof.dependency_headers, {MISSING: self.payload})
        self.assertIn(self.missing, proof.dependencies)
        self.assertEqual((self.built[-1].work_include[0] / MISSING).read_text(), self.payload)
        self.assertEqual(validate.call_args.args[2][self.missing], self.payload.encode())
        self.assertFalse(self.missing.exists())

    def test_installed_home_keeps_its_bytes_even_when_cache_contains_other_bytes(self):
        body = self.payload + "extern int installed_only(void);\n"
        self.missing.write_text(body)
        proof, _ = self.proof()
        self.assertEqual(proof.dependency_headers, {})
        self.assertEqual(self.missing.read_text(), body)
        self.assertEqual((self.built[-1].work_include[0] / MISSING).read_text(), body)

    def test_private_header_edits_and_new_parent_relative_dependency_are_preserved(self):
        private = (self.include / SPAN).read_text().replace(f'"{MISSING}"', f'"../{MISSING}"')
        private += "extern int private_only(void);\n"
        proposed = {SPAN: private}
        proof, _ = self.proof(headers=proposed)
        self.assertEqual(proposed, {SPAN: private})
        self.assertEqual(proof.dependency_headers, {SPAN: private.replace("../" + MISSING, INSTALLED)})
        self.assertEqual(
            (self.built[-1].work_include[0] / SPAN).read_text(), private.replace("../" + MISSING, INSTALLED)
        )

    def test_current_manifest_excludes_an_old_home_even_if_cache_contains_it(self):
        self.lookup["headers"].pop(MISSING)
        for field in ("symbols", "clusters"):
            self.lookup[field] = {name: home for name, home in self.lookup[field].items() if home != MISSING}
        self.lookup["type_headers"] = {
            name: [home for home in homes if home != MISSING] for name, homes in self.lookup["type_headers"].items()
        }
        atomic_files.write(storage.output(index.path(self.project)), index.encoded(self.lookup))
        with self.assertRaisesRegex(Held, "land.fuzzy_compile"):
            self.proof()
        self.assertFalse(self.missing.exists())
        self.assertFalse((self.project.work / "_land/alpha/include" / MISSING).exists())

    def test_wrong_cached_digest_and_absent_cache_refuse_before_compilation(self):
        for text in (self.payload + "extern int stale(void);\n", None):
            with self.subTest(cached=text is not None):
                if text is None:
                    self.artifact.unlink()
                else:
                    self.cache_payload(text)
                with self.assertRaisesRegex(Held, "land.header_dependency.*exact bytes are unavailable"):
                    self.proof()
                self.assertEqual(self.built, [])
                self.assertFalse(self.missing.exists())

    def test_unreachable_manifest_entries_are_never_restored(self):
        proof, _ = self.proof(source="int alpha(void) { return 1; }\n")
        self.assertEqual(proof.dependency_headers, {})
        self.assertFalse(self.missing.exists())

    def test_recursive_owned_imports_close_without_restoring_unrelated_outputs(self):
        nested = "common/nested.h"
        body = self.payload.replace("#endif", '#include "nested.h"\n#endif')
        nested_body = '#ifndef NESTED_H\n#define NESTED_H\n#include "../span_1000/code_800F45C8.h"\n#endif\n'
        self.lookup["headers"].update(
            {
                MISSING: inputs.bytes_digest(body.encode(), algorithm="sha256"),
                nested: inputs.bytes_digest(nested_body.encode(), algorithm="sha256"),
            }
        )
        atomic_files.write(storage.output(index.path(self.project)), index.encoded(self.lookup))
        atomic_files.write(
            storage.output(self.artifact),
            storage.encoded({"outputs": {f"include/{MISSING}": body, f"include/{nested}": nested_body}}),
        )
        proof, _ = self.proof()
        self.assertEqual(proof.dependency_headers, {MISSING: body, nested: nested_body})
        self.assertIn(self.include / nested, proof.dependencies)
        self.assertFalse((self.include / nested).exists())

    def test_successful_publication_commits_the_repaired_import_without_duplicate_homes(self):
        file = self.project.work / "alpha/alpha.c"
        file.parent.mkdir(parents=True)
        file.write_text(SOURCE)
        committed = []
        span = (self.include / SPAN).read_text().replace(MISSING, INSTALLED)

        def commit(project, host, paths, message):
            self.assertFalse(self.missing.exists())
            self.assertEqual((self.include / SPAN).read_text(), span)
            self.assertIn(self.include / SPAN, paths)
            self.assertIn(self.include / INSTALLED, paths)
            self.assertNotIn(self.missing, paths)
            self.assertEqual(message, "Fuzzy alpha")
            committed.append(paths)

        with (
            patch("unbake.fold.apply.fold", return_value=Folded("alpha", SOURCE, {}, ())),
            patch("unbake.fold.apply.private_headers", return_value={}),
            patch("unbake.pool.run", side_effect=self.fuzzy_jobs),
            patch.object(header_step, "validate"),
            patch.object(land.buildfiles, "write", return_value=[]),
            patch("unbake.report.progress.write", return_value=[]),
            patch.object(land, "_commit", side_effect=commit),
            patch.object(land, "_git", return_value="c0ffee\n"),
        ):
            self.assertEqual(land.land(self.project, self.host, file, fuzzy=True), "c0ffee")
        self.assertEqual(len(committed), 1)
        self.assertFalse(self.missing.exists())
        Headers.read(self.project)
        self.assertFalse((self.project.work / "_land/alpha").exists())

    def test_recovery_does_not_change_caller_header_mapping(self):
        headers = {}
        closed = header_dependencies.complete(self.project, SOURCE, headers)
        self.assertEqual(headers, {})
        self.assertEqual(closed.source, SOURCE)
        self.assertEqual(closed.headers, {SPAN: (self.include / SPAN).read_text().replace(MISSING, INSTALLED)})

    def test_direct_source_import_is_rewritten_to_the_same_installed_payload(self):
        source = f'#include "{MISSING}"\nint alpha(void) {{ return 1; }}\n'
        proof, _ = self.proof(source=source)
        self.assertEqual(proof.proposed_source, source.replace(MISSING, INSTALLED))
        self.assertEqual(proof.dependency_headers, {})
        self.assertIn(self.include / INSTALLED, proof.dependencies)
        self.assertFalse(self.missing.exists())

    def test_typedef_or_conditional_changes_never_count_as_the_same_payload(self):
        # Even ABI-equivalent spellings are another lane's evidence, not a
        # reason to move this dependency away from its manifest home.
        installed = self.include / INSTALLED
        for change in ("typedef int s32;\n", "#define SOME_FLAG 1\n"):
            with self.subTest(change=change):
                installed.write_text((FIXTURE / "include" / INSTALLED).read_text() + change)
                proof, _ = self.proof()
                self.assertEqual(proof.dependency_headers, {MISSING: self.payload})
                self.assertIn(self.missing, proof.dependencies)
                self.assertNotIn(installed, proof.dependencies)

    def test_ambiguous_identical_homes_are_explicitly_held_for_provider_selection(self):
        duplicate = "common/another.h"
        body = (self.include / INSTALLED).read_text()
        (self.include / duplicate).write_text(body)
        self.lookup["headers"][duplicate] = inputs.bytes_digest(body.encode(), algorithm="sha256")
        atomic_files.write(storage.output(index.path(self.project)), index.encoded(self.lookup))
        with self.assertRaisesRegex(Held, "land.header_home.*multiple installed homes"):
            self.proof()
        self.assertEqual(self.built, [])
        self.assertFalse(self.missing.exists())
