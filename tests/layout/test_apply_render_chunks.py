"""apply.render rewrites sources in pool chunks: same bytes, same order, same disagreements, same first refusal."""

import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from unbake.config import Held
from unbake.layout import apply
from unbake.typemap import database, regeneration

ROOT = Path("/project")


def fake_source(project, text, member, outputs, *, ownership, lookup, previous, disagreements):
    if "refuse" in text:
        raise Held("layout", f"layout.redeclaration.{member}: refused")
    if disagreements is not None and "differs" in text:
        disagreements[member] = ("int x;", "s32 x;")
    assert all(path.suffix != ".c" for path in outputs), "a source never reads another source"
    return f"{member}:{len(outputs)}:{text}"


class RenderChunkTests(unittest.TestCase):
    def render(self, texts: list[str], workers: int, collect: bool) -> tuple[dict, dict | None]:
        project = SimpleNamespace(build=ROOT / "build", src=ROOT / "src", include=(ROOT / "include",))
        sources = {ROOT / "src" / f"s{i:02}.c": text for i, text in enumerate(texts)}
        session = SimpleNamespace(sources=sources, ownership="map")
        index_path = ROOT / "build/layout/index.json"
        rendered = {ROOT / "include/a.h": b"int a;", index_path: json.dumps({"headers": {}}).encode()}
        policy = SimpleNamespace()
        disagreements: dict | None = {} if collect else None
        with (
            patch.object(apply.map, "load"),
            patch.object(database, "load", return_value={"declaration_evidence": {"e.h": "x"}}),
            patch.object(regeneration, "Session", return_value=session),
            patch.object(database, "_render", return_value=dict(rendered)),
            patch.object(apply.index, "load", return_value={"headers": {"a.h": "0"}}),
            patch.object(apply.index, "path", return_value=index_path),
            patch.object(apply.pool.Pool, "from_host", return_value=SimpleNamespace(size=workers)),
            patch.object(apply.pool, "run", side_effect=lambda host, fn, items: [fn(item) for item in items]),
            patch.object(apply, "source", side_effect=fake_source),
        ):
            outputs = apply.render(project, policy, disagreements)
        return outputs, disagreements

    def test_chunking_does_not_change_the_result(self) -> None:
        texts = ["plain", "differs", "plain", "differs", "plain", "plain", "plain"]
        for label, collect in [("with disagreements", True), ("near miss: refusing mode", False)]:
            with self.subTest(label):
                one = self.render(texts, 1, collect)
                many = self.render(texts, 3, collect)
                self.assertEqual(one, many)
                self.assertEqual(list(one[0])[2:], [ROOT / "src" / f"s{i:02}.c" for i in range(len(texts))])
                self.assertEqual(one[0][ROOT / "src/s00.c"], b"s00:2:plain")
                if collect:
                    self.assertEqual(sorted(p.name for p in one[1]), ["s01.c", "s03.c"])

    def test_first_refusal_in_source_order(self) -> None:
        for workers in (1, 4):
            with self.subTest(workers=workers), self.assertRaisesRegex(Held, "s02"):
                self.render(["plain", "plain", "refuse", "refuse"], workers, True)

    def test_loaded_solution_is_not_mutated(self) -> None:
        # Negative: rendering adds evidence to its own copy only.
        loaded = {"declaration_evidence": {"e.h": "x"}}

        def mutate(project, value, policy, session):
            value["declaration_evidence"]["new.h"] = "y"
            value["shared_aliases"] = {}
            return {ROOT / "build/layout/index.json": b"{}"}

        project = SimpleNamespace(build=ROOT / "build", src=ROOT / "src", include=(ROOT / "include",))
        with (
            patch.object(apply.map, "load"),
            patch.object(database, "load", return_value=loaded),
            patch.object(regeneration, "Session", return_value=SimpleNamespace(sources={}, ownership=None)),
            patch.object(database, "_render", side_effect=mutate),
            patch.object(apply.index, "load", return_value={"headers": {}}),
            patch.object(apply.index, "path", return_value=ROOT / "build/layout/index.json"),
            patch.object(apply.pool.Pool, "from_host", return_value=SimpleNamespace(size=2)),
        ):
            apply.render(project, SimpleNamespace())
        self.assertEqual(loaded, {"declaration_evidence": {"e.h": "x"}})


if __name__ == "__main__":
    unittest.main()
