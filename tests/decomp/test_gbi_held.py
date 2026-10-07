"""Held packet encodings and source guards, without external compilers."""

import hashlib
import operator
import os
import re
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import patch

from pycparser import c_ast, c_parser

from unbake.config import Held, Project
from unbake.decomp import checks, gbi
from unbake.decomp.gbi_expr import split
from unbake.decomp.gbi_source import expand, invocations, macros
from unbake.project import setup


class HeldPacketsTest(unittest.TestCase):
    def definitions(self, variant):
        header = gbi.HEADER.read_text()
        selected = []
        stack = [True]
        flags = {"F3DEX_GBI_2": variant == "f3dex2", "F3D_GBI": variant == "f3d"}
        for line in header[header.index("#if defined(F3DEX_GBI_2)", header.index("#define _GBI_CMD")) :].splitlines():
            if line.startswith("#if"):
                name = re.search(r"defined\((\w+)\)", line)
                stack.append(stack[-1] and bool(name and flags.get(name[1], False)))
            elif line.startswith("#else"):
                stack[-1] = stack[-2] and not stack[-1]
            elif line.startswith("#endif"):
                if len(stack) == 1:
                    break
                stack.pop()
            elif stack[-1]:
                selected.append(line)
        # The vertex rotation helper precedes the command builders.
        helper = macros(header)["_GBI_V3"]
        definitions = macros("\n".join(selected))
        definitions["_GBI_V3"] = helper
        return definitions

    def value(self, expression, symbols):
        node = c_parser.CParser().parse("void f(void) { x = " + expression + "; }").ext[0].body.block_items[0].rvalue

        def evaluate(node):
            if isinstance(node, c_ast.Constant):
                return int(node.value.rstrip("uUlL"), 0)
            if isinstance(node, c_ast.ID):
                return symbols[node.name]
            if isinstance(node, c_ast.Cast):
                return evaluate(node.expr)
            if isinstance(node, c_ast.TernaryOp):
                return evaluate(node.iftrue if evaluate(node.cond) else node.iffalse)
            if isinstance(node, c_ast.UnaryOp):
                return {"~": operator.invert, "-": operator.neg, "+": operator.pos}[node.op](evaluate(node.expr))
            if isinstance(node, c_ast.BinaryOp):
                operation = {
                    "|": operator.or_,
                    "&": operator.and_,
                    "^": operator.xor,
                    "<<": operator.lshift,
                    ">>": operator.rshift,
                    "*": operator.mul,
                    "+": operator.add,
                    "-": operator.sub,
                    "==": operator.eq,
                    "!=": operator.ne,
                    "/": operator.floordiv,
                }[node.op]
                return operation(evaluate(node.left), evaluate(node.right))
            if isinstance(node, c_ast.FuncCall) and node.name.name == "_SHIFTL":
                value, shift, width = map(evaluate, node.args.exprs)
                return (value & ((1 << width) - 1)) << shift
            raise AssertionError(type(node))

        return evaluate(node) & 0xFFFFFFFF

    def packets(self, call, variant):
        definitions = self.definitions(variant)
        symbols = {
            "G_TEXTURE": 0xD7,
            "G_LINE3D": 8,
            "G_TRI1": 5 if variant == "f3dex2" else 0xBF,
            "G_LOAD_UCODE": 0xDD,
            "G_RDPHALF_1": 0xE1,
            "G_TEXRECT": 0xE4,
        }

        def recover(text):
            if "_GBI_CMD" in text:
                args = invocations(text, "_GBI_CMD")[0][2]
                return [
                    (
                        self.value(expand(args[1], definitions), symbols),
                        self.value(expand(args[2], definitions), symbols),
                    )
                ]
            name = re.search(r"\b(?:g|gs)\w+\s*\(", text)[0].split("(")[0].strip()
            start, end, args = invocations(text, name)[0]
            macro = definitions[name]
            replacements = dict(zip(macro.parameters, args, strict=True))
            body = re.sub(r"\b\w+\b", lambda m: replacements.get(m[0], m[0]), macro.body).strip()
            if name.startswith("gs") and body.startswith("{"):
                a, b = split(body.strip("{} "), ",")
                return [(self.value(expand(a, definitions), symbols), self.value(expand(b, definitions), symbols))]
            if name in ("gSPLoadUcodeEx", "gsSPLoadUcodeEx"):
                separator = ";" if name.startswith("gSP") else ","
                return [pair for part in split(body.strip("{} "), separator) if part.strip() for pair in recover(part)]
            return recover(text[:start] + body + text[end:])

        return recover(call)

    def test_exact_builder_word_pairs(self):
        cases = [
            ("gSPTextureL(p, 32768, 32768, 0, 255, 0, 1)", "f3dex2", [(0xD7FF0002, 0x80008000)]),
            ("gLoadUcode(p, 0x12345678, 2048)", "f3dex2", [(0xDD0007FF, 0x12345678)]),
            ("gSPLoadUcode(p, 0x12345678, 0x87654321)", "f3dex2", [(0xE1000000, 0x87654321), (0xDD0007FF, 0x12345678)]),
            (
                "gSPLoadUcodeEx(p, 0x12345678, 0x87654321, 1024)",
                "f3dex2",
                [(0xE1000000, 0x87654321), (0xDD0003FF, 0x12345678)],
            ),
            ("gSP1Triangle(p, 1, 2, 3, 0)", "f3dex", [(0xBF000000, 0x00020406)]),
            ("gSP1Triangle(p, 129, 130, 131, 0)", "f3dex", [(0xBF000000, 0x00020406)]),
            ("gSP1Triangle(p, 1, 2, 3, 1)", "f3dex", [(0xBF000000, 0x00040602)]),
            ("gSP1Triangle(p, 1, 2, 3, 1)", "f3d", [(0xBF000000, 0x010A141E)]),
            ("gSPLineW3D(p, 1, 2, 3, 1)", "f3dex2", [(0x08040203, 0)]),
            ("gDPTexRect(p, 4, 8, 40, 80, 2)", "f3dex2", [(0xE4028050, 0x02004008)]),
        ]
        for v0, v1, word in [(0, 1, 0x08000200), (1, 2, 0x08020400), (2, 3, 0x08040600), (3, 0, 0x08060000)]:
            cases.append((f"gSPLine3D(p, {v0}, {v1}, 0)", "f3dex2", [(word, 0)]))
        for call, variant, expected in cases:
            with self.subTest(call=call):
                self.assertEqual(self.packets(call, variant), expected)
                static = call.replace("g", "gs", 1).replace("p, ", "")
                self.assertEqual(self.packets(static, variant), expected)

    def test_decoder_and_source_selector(self):
        cases = [
            ("0xE4028050", "0x02004008", "gDPTexRect"),
            ("0xDD0007FF", "text", "gLoadUcode"),
            ("0xD7FF0002", "0x80008000", "gSPTextureL"),
            ("0x08000200", "0", "gSPLine3D"),
        ]
        for w0, w1, name in cases:
            self.assertEqual(gbi.decode(w0, w1, "f3dex2")[0], name)
        source = (
            '#undef F3DEX_GBI_2\n#define F3DEX_GBI\n#include "gbi.h"\n'
            "void f(Gfx *p) {p->words.w0=0xBF000000;p->words.w1=0x20406;}"
        )
        lowered = gbi.lower(source, "f3dex2")
        self.assertFalse(lowered.raw)
        self.assertIn("gSP1Triangle(p, 1, 2, 3, 0)", lowered.source)
        read = "void f(Gfx *p) { if(p->words.w0 == 1 && p->words.w1 == 2) {} }"
        self.assertFalse(gbi.lower(read, "f3dex2").raw)
        self.assertFalse(checks.run(read))
        packed = "(s32)((((arg0 * 2) & 0xFE) << 16) | ((arg1 << 9) & 0xFE00) | ((arg2 * 2) & 0xFE))"
        name, vertices = gbi.decode("0xBF000000", packed, "f3dex")
        self.assertEqual(name, "gSP1Triangle")
        self.assertEqual(vertices, ["arg0", "arg1", "arg2", "0"])
        read_macro = "#define READ(p) { Gfx *g=p; g->words.w0 == 1; g->words.w1 == 2; }\nREAD(dl);"
        self.assertFalse(gbi.lower(read_macro, "f3dex2").raw)

    def test_hidden_packets_and_macro_fields(self):
        for text in [
            "p->unk0=0xBF000000;p->unk4=vertices;",
            "p->first=0x01000040 | ((n & 255)<<16);p->second=addr;",
            "magic=0xB8000000; p->unk0=a;p->unk4=b;p=&p->unk8;",
        ]:
            self.assertIn("raw-gfx", [f.rule for f in checks.run(text)])
        self.assertFalse(checks.run("p->first=17;p->second=22;"))
        source = "#define FIELD(p,t,o) (*(t)((char *)(p)+(o)))\n#define M(p,o) FIELD(p,int *,o)\n"
        source += "x=M(p,0x88);\ny=M(p,0);\n"
        findings = [f for f in checks.run(source) if f.rule == "raw-offset"]
        self.assertEqual([f.line for f in findings], [3])
        findings = checks.run("typedef s32 M2C_UNK;")
        self.assertEqual(findings[0].rule, "local-type-copy")

    def test_header_upgrade_only_accepts_the_previous_asset(self):
        with tempfile.TemporaryDirectory(dir=os.environ["TMPDIR"]) as temporary:
            root = Path(temporary)
            include = root / "include"
            include.mkdir()
            project = cast(Project, SimpleNamespace(root=root, include=[include], compilers={}))
            old = "/* previous canonical asset */"
            header = include / "gbi.h"
            header.write_text(old)
            digest = hashlib.sha256(old.encode()).hexdigest()
            with patch.object(gbi, "PREVIOUS_HEADER_SHA256", digest):
                setup._sdk_headers(project)
                self.assertEqual(header.read_text(), gbi.HEADER.read_text())
                header.write_text(old)
                gbi.install(project)
                self.assertEqual(header.read_text(), gbi.HEADER.read_text())
            header.write_text("/* authored */")
            with self.assertRaises(Held):
                setup._sdk_headers(project)
            with self.assertRaises(Held):
                gbi.install(project)
            self.assertEqual(header.read_text(), "/* authored */")
