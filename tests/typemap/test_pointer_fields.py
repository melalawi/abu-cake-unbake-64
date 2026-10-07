"""Real RageWars pointer payloads and shared helpers do not equate independent object types."""

import json
import unittest
from pathlib import Path
from types import SimpleNamespace

from unbake.typemap import closure
from unbake.typemap.solver import infer

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


class PointerFieldTests(unittest.TestCase):
    def test_real_command_payload_keeps_stored_parameter_types_separate(self):
        # Both bodies put arg0 into word 1 of the current display command, then advance the buffer.
        solved = infer(SimpleNamespace(), json.loads((FIXTURES / "pointer_field_facts.json").read_text()), [])
        nodes = solved["nodes"]
        first = nodes["param:func_8026D914_de:r4"]["component"]
        second = nodes["param:func_802A9CD0_de:r4"]["component"]
        field = nodes["field:global:D_8010C574:4"]
        self.assertNotEqual(first, second)
        self.assertNotIn(field["component"], (first, second))
        self.assertEqual((field["state"], field["type"]), ("known", "void *"))

    def test_real_untyped_helper_keeps_distinct_object_fields_separate(self):
        # One caller passes four separate object pointers to a helper that manipulates flags at offset 18.
        solved = infer(SimpleNamespace(), json.loads((FIXTURES / "generic_helper_facts.json").read_text()), [])
        components = {solved["nodes"][f"field:global:D_800DF4C8:{offset}"]["component"] for offset in (4, 8, 12, 16)}
        self.assertEqual(len(components), 4)

    def test_recursive_pointer_slot_does_not_join_its_other_stored_objects(self):
        graph = closure.Constraints()
        for node in ("global:head", "param:a:r4", "param:b:r4"):
            graph.seed(node, "void *", {"kind": "machine"})
            graph.store(node, "field:global:head:0", {"function": node})
        graph.instantiate_fields()
        self.assertEqual(len({graph.root(n) for n in ("global:head", "param:a:r4", "param:b:r4")}), 3)
        field = "field:global:head:0"
        self.assertEqual(closure.resolve([field], graph.seeds, {})["type"], "void *")

    def test_scalar_stores_and_one_pointer_source_still_propagate(self):
        for type_, sources in (("float", ("a", "b")), ("void *", ("a",))):
            with self.subTest(type_=type_):
                graph = closure.Constraints()
                for node in sources:
                    graph.seed(node, type_, {"kind": "machine"})
                    graph.store(node, "field:global:source:0", {})
                graph.instantiate_fields()
                self.assertTrue(all(graph.root(n) == graph.root("field:global:source:0") for n in sources))

    def test_aliased_argument_and_result_are_checked_as_one_interface(self):
        graph = closure.Constraints()
        graph.connect("param:helper:r4", "result:helper:r2", {})
        graph.seed("left", "float", {"kind": "machine"})
        graph.seed("right", "short *", {"kind": "machine"})
        graph.link("left", "param:helper:r4", {})
        graph.link("right", "result:helper:r2", {})
        graph.instantiate()
        self.assertNotEqual(graph.root("left"), graph.root("right"))

    def test_conflicting_store_evidence_stays_on_the_field(self):
        graph = closure.Constraints()
        for node, type_ in (("pointer", "void *"), ("scalar", "float")):
            graph.seed(node, type_, {"kind": "machine"})
            graph.store(node, "field:global:source:0", {})
        graph.instantiate_fields()
        self.assertNotEqual(graph.root("pointer"), graph.root("scalar"))
        field = "field:global:source:0"
        self.assertEqual(closure.resolve([field], graph.seeds, {})["alternatives"], ["float", "void *"])
