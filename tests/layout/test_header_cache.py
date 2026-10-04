"""Parsed header snapshots preserve recursive identities and observe content changes."""

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

from unbake.layout import header_cache, header_context
from unbake.layout.header_context import Headers
from unbake.layout.structs_parser import Parser
from unbake.config import Held


class HeaderCacheTests(unittest.TestCase):
    def test_large_shared_layout_source_is_serialized_once(self):
        source = "struct A {int value;};\n" + "/* padding */\n" * 10000
        layout = Parser(source).parse()[0]
        layouts = [replace(layout, name=f"A{index}") for index in range(200)]
        document = header_cache._encode(layouts)
        encoded = json.dumps(document)
        self.assertLess(len(encoded), len(source) * 2)
        restored = header_cache._decode(json.loads(encoded))
        self.assertEqual(restored, layouts)
        self.assertTrue(all(item.source is restored[0].source for item in restored))

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.cache = self.root / "cache"

    def test_private_roots_reuse_snapshot_with_recursive_types_and_layout_cache(self):
        text = (
            "#define COUNT 2\n"
            "typedef unsigned int u32; typedef u32 ref, tuple;\n"
            "typedef struct Node {u32 value; struct Node *next;} Node;\n"
            "typedef void (*Callback)(Node *, u32);\n"
            "typedef struct Owner {Node nodes[COUNT]; Callback callback;} Owner;\n"
        )
        original = self.root / "original"
        copied = self.root / "copied"
        inputs = {original / "include/types.h": text}
        first = header_cache.context(
            inputs, original, self.cache, lambda: header_context._context(inputs, root=original)
        )
        inputs = {copied / "include/types.h": text}
        miss = Mock(side_effect=AssertionError("unchanged copied context parsed again"))
        second = header_cache.context(inputs, copied, self.cache, miss)
        miss.assert_not_called()
        self.assertEqual(list(second[0]), list(inputs))
        self.assertEqual(first[2], second[2])
        parser = second[1]
        node = parser.types["struct Node"]
        self.assertIs(parser.types["Node"][0], node)
        self.assertIs(node.members[1].base, node)
        self.assertEqual(parser.layout(node), parser.cache[id(node)])
        restored = Headers.__new__(Headers)
        restored.root = copied
        restored._cache_root = self.cache
        with patch("unbake.cache._remembered", {}):
            restored._load(inputs)
        _, records = restored.parse("struct Later {Node nodes[COUNT]; Callback callback;};")
        fresh = Parser("struct Later {Node nodes[COUNT]; Callback callback;};")
        fresh.types.update(first[1].types)
        fresh.defines.update(first[1].defines)
        self.assertEqual(records, fresh.parse())

    def test_edited_header_and_changed_driver_invalidate_snapshot(self):
        root = self.root / "project"
        contents = {root / "types.h": "typedef struct A {int value;} A;"}
        parse = Mock(side_effect=lambda: header_context._context(contents, root=root))
        header_cache.context(contents, root, self.cache, parse)
        contents[root / "types.h"] = "typedef struct A {int other;} A;"
        header_cache.context(contents, root, self.cache, parse)
        self.assertEqual(parse.call_count, 2)
        original_key = header_cache.key
        with patch.object(header_cache, "key", side_effect=lambda *parts: original_key("changed driver", *parts)):
            header_cache.context(contents, root, self.cache, parse)
        self.assertEqual(parse.call_count, 3)

    def test_failed_parse_publishes_no_artifact(self):
        with self.assertRaisesRegex(Held, "invalid"):
            header_cache.context(
                {self.root / "types.h": "invalid"},
                self.root,
                self.cache,
                Mock(side_effect=Held("structs", "invalid")),
            )
        self.assertEqual(list(self.cache.rglob("*"))[-1].is_dir(), True)
        self.assertFalse(any(path.is_file() for path in self.cache.rglob("*")))
