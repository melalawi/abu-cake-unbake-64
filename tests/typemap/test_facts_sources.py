"""facts.source_facts: one extraction per distinct unit text, stamped per task, equal to single extraction."""

from types import SimpleNamespace
from unittest.mock import patch

from tests.kit import TempCase
from unbake.typemap import declarations, facts

HEADER = "typedef struct Pair { int a; int b; } Pair;\nextern Pair shared;\n"


class SourceFactsTests(TempCase):
    def setUp(self) -> None:
        super().setUp()
        self.source = self.root / "src" / "alpha.c"
        self.source.parent.mkdir()
        self.source.write_text("Pair *alpha(void) { return &shared; }\nint beta(void) { return 1; }\n")
        self.project = SimpleNamespace(root=self.root, include=(), version=lambda v: SimpleNamespace(macros=()))
        texts = {
            "us": HEADER + "Pair *alpha(void);\nint beta(void);\n",
            "eu": HEADER + "Pair *alpha(void);\nint beta(void);\n",
            "jp": HEADER + "Pair *alpha(void);\nint beta(void);\nconst int rodata_jp = 1;\n",
        }
        self.units = lambda project, policy, version, source: (
            "extern int __unbake_feedback_boundary;\n" + texts[version]
        )

    def test_stamped_facts_equal_each_task_extracted_alone(self) -> None:
        tasks = [(f, self.source, v) for v in ("eu", "jp", "us") for f in ("alpha", "beta")]
        with patch.object(declarations, "source_unit", self.units):
            store = facts.Store(None)
            stamped = facts.source_facts(self.project, None, store, tasks)
            for task, data in zip(tasks, stamped, strict=True):
                with self.subTest(task=task[0] + "/" + task[2]):
                    self.assertEqual(data, store.encoded(facts.extract(self.project, None, task)))
        self.assertNotIn(b"\\u0000", b"".join(stamped))

    def test_a_source_edited_after_its_key_is_refused(self) -> None:
        project = SimpleNamespace(
            root=self.root, include=(), build=self.root / "build", version=lambda v: SimpleNamespace(macros=())
        )
        task = ("alpha", self.source, "us")
        stale = facts.unit_key(project, None, self.source, "us", facts.Snapshot(project))
        self.source.write_text(self.source.read_text() + "int gamma;\n")
        with (
            patch.object(declarations, "source_unit", lambda *args, **named: self.units(*args)),
            patch.object(facts.Snapshot, "generated", lambda snapshot: frozenset()),
            self.assertRaises(facts.Held) as raised,
        ):
            facts._unit_job((project, None, {"us": {}}), [[(0, stale, task)]])
        self.assertIn("facts.inputs: src/alpha.c changed during the solve", str(raised.exception))

    def test_source_key_follows_schema_not_tool_code(self) -> None:
        project = SimpleNamespace(root=self.root, include=(), version=lambda v: SimpleNamespace(macros=("V",)))
        snapshot = facts.Snapshot(project)
        task = ("alpha", self.source, "us")
        base = facts.source_key(project, None, task, snapshot)
        with patch.object(facts, "FACTS_SCHEMA", facts.FACTS_SCHEMA + 1):
            self.assertNotEqual(facts.source_key(project, None, task, facts.Snapshot(project)), base)
        self.assertEqual(facts.source_key(project, None, task, facts.Snapshot(project)), base)


class SharedVersionsTests(TempCase):
    """The versions of one source that preprocess alike share one source-part extraction in their job."""

    def test_one_extraction_per_distinct_unit_text_across_versions(self) -> None:
        from unbake.typemap import layers

        source = self.root / "src" / "alpha.c"
        source.parent.mkdir()
        source.write_text("int alpha;\n")
        project = SimpleNamespace(
            root=self.root, include=(), build=self.root / "build", version=lambda v: SimpleNamespace(macros=())
        )
        texts = {v: declarations.BOUNDARY + "\nint alpha;\n" for v in ("de", "eu", "us")}
        texts["jp"] = declarations.BOUNDARY + "\nint alpha;\nint jp_only;\n"  # near miss: another unit text
        snapshot = facts.Snapshot(project)
        groups = [
            [(index, facts.unit_key(project, None, source, version, snapshot), ("alpha", source, version))]
            for index, version in enumerate(sorted(texts))
        ]
        parts: list[str] = []

        def source_part(text, *rest):  # type: ignore[no-untyped-def]
            parts.append(text)
            return None  # whole-unit: the job hands every version to one whole extraction

        whole: list[list] = []
        with (
            patch.object(declarations, "source_unit", lambda p, h, version, s, **k: texts[version]),
            patch.object(layers, "source_part", source_part),
            patch.object(facts.Snapshot, "generated", lambda snapshot: frozenset()),
            patch.object(facts, "_whole_tasks", lambda p, h, o, group, c: whole.append(group) or []),
        ):
            _, counts = facts._unit_job((project, None, {v: {} for v in texts}), groups)
        self.assertEqual(sorted(parts), sorted(set(texts.values())))
        self.assertEqual(counts["sources"], 2)
        self.assertEqual([len(group) for group in whole], [4])  # one whole call for all four versions
