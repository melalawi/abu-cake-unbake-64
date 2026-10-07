"""Real default-build consumer imports retain one owner through regenerated headers."""

import hashlib
import json
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tests.preprocessor import expand
from tests.project_fixture import ProjectCase
from unbake.cache import Cache
from unbake.config import Held
from unbake.fold import provider_reuse
from unbake.layout import apply, index
from unbake.process import named
from unbake.typemap import database, regeneration

FIXTURE = Path(__file__).parent / "fixtures/regeneration_shared_owner"
OLD = "common/types_d507c48987bb.h"
NEW = "common/types_c5836f3cc521.h"
SPAN = "span_1000/code_800F45C8.h"
SOURCE = "func_8008001C_us"


class RegenerationOwnerTests(ProjectCase):
    versions = ("us",)

    def setUp(self):
        super().setUp()
        self.headers = {
            self.project.include[0] / p.relative_to(FIXTURE / "include"): p.read_text()
            for p in (FIXTURE / "include").rglob("*.h")
        }
        self.old = self.project.include[0] / OLD
        self.new = self.project.include[0] / NEW
        self.span = self.project.include[0] / SPAN
        self.source = self.project.src / (SOURCE + ".c")
        self.source.write_bytes((FIXTURE / "src" / (SOURCE + ".c")).read_bytes())
        self.proposed = {self.new: self.headers[self.new].encode(), self.span: self.headers[self.span].encode()}
        installed = {path: text for path, text in self.headers.items() if path != self.new}
        installed[self.span] = installed[self.span].replace(NEW, OLD)
        self.installed = installed
        for path, text in installed.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        self.owned = {self.old: installed[self.old], self.span: installed[self.span]}
        self.lookup = {
            "schema": 1,
            "headers": {
                NEW: hashlib.sha256(self.proposed[self.new]).hexdigest(),
                SPAN: hashlib.sha256(self.proposed[self.span]).hexdigest(),
            },
            "symbols": {},
            "clusters": {},
            "type_headers": {"QueryResult": [NEW]},
        }
        self.proposed[index.path(self.project)] = index.encoded(self.lookup)

    def session(self):
        session = object.__new__(regeneration.Session)
        session.project, session.cache = self.project, Cache(self.project.cache)
        session.inputs = hashlib.sha256(b"real-consumer-small-render").hexdigest()
        session.source_words = {}
        session.reserved, session.consumer_names, session.consumer_tags = set(), {}, {}
        session.authored = {p: t for p, t in self.installed.items() if p not in self.owned}
        session.installed = dict(self.owned)
        session.provider_catalogs = {}
        session.sources = {self.source: self.source.read_text()}
        return session

    def value(self):
        return {
            "shared_aliases": {},
            "declaration_headers": {},
            "functions": {},
            "globals": {},
            "structs": {},
            "arrays": {},
            "dependencies": {},
            "revision": 1,
        }

    def default_consumer(self, outputs):
        for path, content in outputs.items():
            if path.suffix == ".h":
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content.read_bytes() if isinstance(content, Path) else content)
        return expand(
            self.source.read_text(),
            roots=(self.project.include[0],),
            macros={"VERSION_US": "1"},
            cwd=self.source.parent,
        )

    def test_render_cache_and_following_header_install_keep_the_actual_old_owner(self):
        session = self.session()
        value = self.value()
        with (
            patch.object(session, "_installed_headers", return_value={}) as installed,
            patch.object(session, "_content_key", return_value=(hashlib.sha256(b"render-key").hexdigest(), {})),
            patch.object(index, "load", return_value=self.lookup),
            patch.object(provider_reuse, "_catalog", wraps=provider_reuse._catalog) as catalogs,
        ):
            first = session.render(value, lambda: dict(self.proposed))
            second = session.render(value, lambda: self.fail("rendered the same solution twice"))
        self.assertEqual(first, second)
        self.assertEqual(installed.call_count, 2)
        self.assertEqual(catalogs.call_count, 8)
        self.assertIn(self.old, first)
        lookup = json.loads(first[index.path(self.project)])
        self.assertIn(OLD, lookup["headers"])
        self.assertEqual(lookup["headers"][OLD], hashlib.sha256(self.installed[self.old].encode()).hexdigest())
        self.assertNotIn("struct QueryResult {", first[self.new].decode())
        self.assertIn(f'#include "{OLD}"', first[self.new].decode())
        with (
            patch.object(index, "owned", return_value=frozenset({self.old, self.span})),
            patch("unbake.layout.header_loss.check"),
        ):
            apply.install(self.project, first)
        self.assertTrue(self.old.exists(), "headers step deleted the canonical owner")
        source_before = (FIXTURE / "src" / (SOURCE + ".c")).read_bytes()
        self.assertEqual(self.source.read_bytes(), source_before)
        expanded = self.default_consumer(first)
        self.assertEqual(len(re.findall(r"struct QueryResult\s*\{", expanded)), 1)
        self.assertEqual(len(re.findall(r"struct QueryBox\s*\{", expanded)), 1)

    def test_final_namespace_projection_reuses_the_same_plan_before_required_default_proof(self):
        session = self.session()
        value = self.value()
        # Return the real failed render; namespace output is intentionally after
        # render so the public publish boundary must reconcile its final view.
        session.render = lambda *args: dict(self.proposed)
        calls = []

        def native(project, policy, outputs, **kwargs):
            self.assertTrue(kwargs["prove_all"])
            expanded = self.default_consumer(outputs)
            self.assertEqual(len(re.findall(r"struct QueryResult\s*\{", expanded)), 1)
            self.assertEqual(len(re.findall(r"struct QueryBox\s*\{", expanded)), 1)
            calls.append((SOURCE, "us", 0))
            raise Held(
                named(
                    "compile.fixture",
                    "compile.fixture: stop after retained default proof",
                    owner="fixture",
                    stage="compile",
                )
            )

        with (
            patch.object(regeneration, "Session", return_value=session),
            patch.object(database.namespace, "FunctionDeclarations", return_value=SimpleNamespace()),
            patch.object(database.namespace, "publication_outputs", return_value={self.span: self.proposed[self.span]}),
            patch.object(database, "_consumer_imports", return_value={}) as reconnect,
            patch("unbake.layout.header_loss.check"),
            patch.object(database, "validate_headers") as headers,
            patch("unbake.layout.header_step.validate", side_effect=native) as proof,
            patch.object(database.types_db, "stage") as stage,
            self.assertRaisesRegex(Held, "compile.fixture"),
        ):
            database.publish(self.project, value, {}, policy=self.host)
        self.assertEqual(reconnect.call_count, 1)
        self.assertEqual(headers.call_count, 1)
        self.assertEqual(proof.call_count, 1)
        self.assertEqual(calls, [(SOURCE, "us", 0)])
        stage.assert_not_called()

    def test_reused_payloads_are_catalogued_once_and_no_headers_are_reread(self):
        cache = {}
        with (
            patch.object(provider_reuse, "_catalog", wraps=provider_reuse._catalog) as catalogs,
            patch.object(Path, "read_bytes", side_effect=AssertionError("payload reread")),
        ):
            first = provider_reuse.regenerated(self.project, self.proposed, self.installed, self.owned, cache=cache)
            second = provider_reuse.regenerated(self.project, self.proposed, self.installed, self.owned, cache=cache)
        self.assertEqual(first, second)
        self.assertEqual(catalogs.call_count, len(cache))
        self.assertEqual(catalogs.call_count, 8)

    def test_conflicting_regenerated_layout_names_both_real_providers(self):
        changed = dict(self.proposed)
        changed[self.new] = changed[self.new].replace(b"int unused[2]", b"int unused[3]")
        with self.assertRaises(Held) as caught:
            provider_reuse.regenerated(self.project, changed, self.installed, self.owned)
        self.assertEqual(caught.exception.key, "headers.declaration.duplicate-shared-provider")
        self.assertIn(OLD, caught.exception.reason)
        self.assertIn(NEW, caught.exception.reason)
