"""Real BattleTanx headers and cache state cannot resurrect an obsolete home."""

import json
import shutil
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from tests.project_fixture import ProjectCase
from unbake.cache import Cache
from unbake.config import Held
from unbake.layout import apply, header_step, index
from unbake.layout.header_context import Headers
from unbake.typemap import database, regeneration, storage

FIXTURE = Path(__file__).parent / "fixtures/battletanx_orphan"
OLD = "common/types_d507c48987bb.h"
CURRENT = "common/types_f8bfabebf96f.h"


class OrphanHeaderTests(ProjectCase):
    versions = ("us",)

    def setUp(self):
        super().setUp()
        self.include = self.project.include[0]
        for source in (FIXTURE / "include").rglob("*.h"):
            target = self.include / source.relative_to(FIXTURE / "include")
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
        self.old, self.current = self.include / OLD, self.include / CURRENT
        self.hand = self.include / "common/types_hand.h"
        self.hand.write_text("#ifndef HAND_H\n#define HAND_H\nstruct HandRecord { int value; };\n#endif\n")
        self.source = self.project.src / "alpha.c"
        self.source.write_text(f'#include "{CURRENT}"\nint alpha(struct QueryResult *p) {{ return p->kind; }}\n')
        self.lookup = self.manifest(self.current)
        self.outputs = {self.current: self.current.read_bytes(), index.path(self.project): index.encoded(self.lookup)}

    def manifest(self, header):
        name = header.relative_to(self.include).as_posix()
        lookup = {
            "schema": 1,
            "headers": {name: storage.digest(header.read_bytes())},
            "symbols": {},
            "clusters": {},
            "type_headers": {"QueryResult": [name]},
        }
        storage.write(index.path(self.project), index.encoded(lookup))
        return lookup

    def cached_session(self):
        session = object.__new__(regeneration.Session)
        session.project, session.cache = self.project, Cache(self.project.cache)
        session.inputs = "db50f25979c8d8b6f7af81fb18bc938122b0556565d22b97d12d3c16b714fac2"
        session.source_words = {}
        session.reserved, session.consumer_names, session.consumer_tags = set(), {}, {}
        return session

    def test_real_orphan_is_deleted_even_when_all_outputs_are_unchanged(self):
        self.assertEqual(apply.install(self.project, self.outputs, dry_run=True), 1)
        self.assertTrue(self.old.exists())
        self.assertEqual(apply.install(self.project, self.outputs), 1)
        self.assertFalse(self.old.exists())
        self.assertEqual(self.current.read_bytes(), self.outputs[self.current])
        self.assertTrue(self.hand.exists())
        self.assertEqual(apply.install(self.project, self.outputs), 0)

    def test_declaration_catalogue_uses_current_manifest_and_hand_headers(self):
        headers = Headers.read(self.project)
        self.assertNotIn(self.old, headers.texts)
        self.assertIn(self.current, headers.texts)
        self.assertIn(self.hand, headers.texts)
        self.assertEqual(headers.homes["struct QueryResult"], self.current)
        self.assertEqual(next(r.size for r in headers.records if r.name == "QueryResult"), 12)

    def test_root_scalar_header_with_unbake_guard_remains_hand_owned(self):
        scalar = self.include / "types.h"
        body = "#ifndef UNBAKE_TYPES_H\n#define UNBAKE_TYPES_H\ntypedef int s32;\n#endif\n"
        scalar.write_text(body)
        self.assertFalse(storage.generated(self.project, scalar))
        apply.install(self.project, self.outputs)
        self.assertEqual(scalar.read_text(), body)
        self.assertIn(scalar, Headers.read(self.project).texts)

    def test_hand_owned_duplicate_still_refuses(self):
        self.old.unlink()
        self.hand.write_text("struct QueryResult { int other; };\n")
        with self.assertRaisesRegex(Held, "struct QueryResult: duplicate definition"):
            Headers.read(self.project)
        self.assertTrue(self.hand.exists())

    def test_orphan_is_never_ingested_as_authored_or_published_evidence(self):
        session = regeneration.Session(self.project, None)
        self.assertNotIn(self.old, session.authored)
        self.assertTrue(storage.generated(self.project, self.old))
        self.assertNotIn(self.old, set().union(*session.published_homes.values()))
        value = {kind: {} for kind in ("structs", "functions", "globals", "arrays")}
        outputs = database._render(self.project, value, None, session)
        self.assertNotIn(self.old, outputs)
        self.assertIn(
            "struct QueryResult", "\n".join(data.decode() for path, data in outputs.items() if path.suffix == ".h")
        )

    def test_deletion_only_run_installs_and_reports_the_orphan(self):
        with (
            patch.object(apply, "units"),
            patch.object(apply, "render", return_value=self.outputs),
            patch.object(header_step, "validate"),
        ):
            self.assertEqual(header_step.run(self.project, self.host), [self.old])
        self.assertFalse(self.old.exists())
        self.assertTrue(self.hand.exists())

    def test_deletion_is_journaled_and_restored_on_a_hold(self):
        original = self.old.read_bytes()
        install = apply.install

        def refuse(project, outputs):
            install(project, outputs)
            raise Held("headers", "headers.test: after orphan deletion")

        with (
            patch.object(apply, "units"),
            patch.object(apply, "render", return_value=self.outputs),
            patch.object(header_step, "validate"),
            patch.object(apply, "install", side_effect=refuse),
            self.assertRaisesRegex(Held, "after orphan deletion"),
        ):
            header_step.run(self.project, self.host)
        self.assertEqual(self.old.read_bytes(), original)

    def test_orphan_import_is_rewritten_before_deletion(self):
        self.source.write_text(self.source.read_text().replace(CURRENT, OLD))
        rewritten = apply.source(self.project, self.source.read_text(), "alpha", self.outputs, lookup=self.lookup)
        self.assertNotIn(OLD, rewritten)
        self.assertIn(CURRENT, rewritten)
        outputs = {**self.outputs, self.source: rewritten.encode()}
        apply.install(self.project, outputs)
        self.assertFalse(self.old.exists())
        self.assertIn(CURRENT, self.source.read_text())

    def test_orphan_deletion_cannot_erase_a_consumed_contract(self):
        self.current.write_text("/* empty replacement */\n")
        self.source.write_text(self.source.read_text().replace(CURRENT, OLD))
        with self.assertRaisesRegex(Held, "QueryResult"):
            apply.install(self.project, {})
        self.assertTrue(self.old.exists())

    def test_deleted_header_is_empty_in_the_compile_view(self):
        from unbake import pool
        from unbake.work import attempts

        self.source.write_text(self.source.read_text().replace(CURRENT, OLD))
        seen = []

        def compile_jobs(host, function, jobs):
            for view, _, _, _, _, _, _ in jobs:
                stage = view.work_include[0]
                self.assertEqual((stage / OLD).read_bytes(), b"")
                self.assertEqual((stage / CURRENT).read_bytes(), self.current.read_bytes())
                self.assertTrue(self.old.read_bytes())
                seen.append(True)
            return [None for _ in jobs]

        with (
            patch.object(attempts, "fuzzy_sources", return_value={"alpha"}),
            patch.object(pool, "run", side_effect=compile_jobs),
        ):
            self.assertEqual(
                header_step.validate(self.project, self.host, {}, obsolete=frozenset({self.old})), ["alpha"]
            )
        self.assertEqual(seen, [True])
        self.assertFalse((self.project.work / "_headers").exists())

    def test_validated_render_is_reused_after_install_changes_the_manifest(self):
        session = self.cached_session()
        value = deepcopy(json.loads((FIXTURE / "cached-render-state.json").read_bytes())["projection"])
        self.manifest(self.old)

        def compute():
            value.update(declaration_headers={}, shared_aliases={})
            return dict(self.outputs)

        outputs = session.render(value, compute)
        apply.install(self.project, outputs)
        self.assertFalse(self.old.exists())
        reused = session.render(value, lambda: self.fail("installed validated headers were rendered again"))
        self.assertEqual(reused, outputs)

    def test_render_key_pins_real_installed_names_and_bytes(self):
        session = self.cached_session()
        value = deepcopy(json.loads((FIXTURE / "cached-render-state.json").read_bytes())["projection"])
        self.manifest(self.old)
        old_key, _ = session._content_key(value)
        self.manifest(self.current)
        current_key, _ = session._content_key(value)
        self.assertNotEqual(old_key, current_key)
        self.current.write_bytes(self.current.read_bytes().replace(b"int kind", b"unsigned int kind"))
        changed_key, _ = session._content_key(value)
        self.assertNotEqual(current_key, changed_key)

    def test_real_cached_render_state_cannot_restore_old_filename(self):
        for legacy in (True, False):
            with self.subTest(legacy=legacy):
                session = self.cached_session()
                session.cache = Cache(self.root / ("legacy-cache" if legacy else "current-cache"))
                state = json.loads((FIXTURE / "cached-render-state.json").read_bytes())
                if not legacy:
                    state["schema"] = regeneration.RENDER_SCHEMA
                storage.write(session.cache.path("typemap-render-state", session.inputs), storage.encoded(state))
                storage.write(
                    session.cache.path("typemap-render", state["content_key"]),
                    (FIXTURE / "cached-render.json").read_bytes(),
                )
                value = deepcopy(state["projection"])
                calls = []

                def compute(calls=calls, value=value):
                    calls.append(True)
                    value.update(declaration_headers={}, shared_aliases={})
                    return dict(self.outputs)

                outputs = session.render(value, compute)
                self.assertEqual(calls, [True])
                self.assertNotIn(self.old, outputs)
                self.assertEqual(outputs[self.current], self.current.read_bytes())
                self.assertEqual(session.render(value, lambda: self.fail("current render was not reused")), outputs)
