"""Loop-form and ternary-arm rewrites from the order generator keep their meaning."""

import time
import unittest
from types import SimpleNamespace

from unbake.search import order

# RageWars func_8041F2A0 matched only once its config walk became config[i]; the walk form
# placed the strength-reduced pointer in another schedule slot.
SOURCE = """typedef struct { char pad[3]; unsigned char active; } C;
C D[4];
int seen;
void g(int i) { seen = seen * 10 + i; }
void f(void) {
    int i;
    C *c;
    for (i = 0, c = D; i < 4; i++, c++) {
        if (c->active == 1) {
            g(i);
        }
    }
}
"""


class LoopFormTests(unittest.TestCase):
    def rewrites(self, source: str) -> dict[str, str]:
        mutations = order.propose(
            source,
            SimpleNamespace(function="f"),
            SimpleNamespace(deadline=time.monotonic() + 30, focus_lines=None),
        )
        return {m.description: m.source for m in mutations if m.description.startswith("pointer walk")}

    def test_three_indexed_forms_preserve_behaviour(self) -> None:
        found = self.rewrites(SOURCE)
        self.assertEqual(
            sorted(found),
            ["pointer walk to address form", "pointer walk to array form", "pointer walk to index form"],
        )
        self.assertIn("(&D[i])->active", found["pointer walk to address form"])
        self.assertIn("D[i].active", found["pointer walk to array form"])
        self.assertIn("c[i].active", found["pointer walk to index form"])
        for text in found.values():
            self.assertIn("g(i);", text)
            self.assertIn("i < 4", text)
            self.assertNotIn("c++", text)

    def test_pointer_used_outside_its_fields_is_left_alone(self) -> None:
        for name, changed in (
            ("counter modified", SOURCE.replace("g(i);", "g(i); i++;")),
            ("arithmetic", SOURCE.replace("g(i);", "g(c - D);")),
            ("reassigned", SOURCE.replace("g(i);", "g(i); c = D;")),
            ("read after the loop", SOURCE.replace("    }\n}\n", "    }\n    seen = c->active;\n}\n")),
        ):
            with self.subTest(use=name):
                self.assertNotEqual(changed, SOURCE)
                self.assertEqual(self.rewrites(changed), {})


class TernaryArmTests(unittest.TestCase):
    def test_arm_order_inverts_the_condition(self) -> None:
        # RageWars func_8043FFAC: x > 0xFF ? 0xFF : x stores each arm into the outgoing argument
        # slot where x < 0x100 ? x : 0xFF computes a register first; each inversion gains words.
        source = "int g(int, int);\nint f(int x, int y) { return g(x < 256 ? x : 255, y); }\n"
        found = [
            m.source
            for m in order.propose(
                source,
                SimpleNamespace(function="f"),
                SimpleNamespace(deadline=time.monotonic() + 30, focus_lines=None),
            )
            if m.description == "conditional arm order"
        ]
        self.assertEqual(len(found), 1)
        self.assertIn("(!(x < 256)) ? (255) : (x)", found[0])


if __name__ == "__main__":
    unittest.main()
