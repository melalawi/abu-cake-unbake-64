"""A parse resumed after a shared prefix puts every node where a whole parse of the same text puts it."""

import unittest

from unbake import cache, cdecl, prefixes

PREFIX = "".join(f"int a{i};\n" for i in range(700))


def places(tree) -> list[tuple[str, str, int]]:  # type: ignore[no-untyped-def]
    return [(node.name, node.coord.file, node.coord.line) for node in tree.ext]


class ResumedParseTests(unittest.TestCase):
    def setUp(self) -> None:
        cache.configure(memory_bytes=16 * 1024 * 1024)
        prefixes.forget()
        self.addCleanup(prefixes.forget)

    def test_resumed_nodes_keep_their_whole_text_coordinates(self) -> None:
        for label, first, second in [
            ("no line markers", PREFIX + "int x;\n", PREFIX + "typedef int T;\nT y;\n"),
            (
                "inside a marked file",
                '# 1 "h.h"\n' + PREFIX + "int x;\n",
                '# 1 "h.h"\n' + PREFIX + 'typedef int T;\n# 7 "s.c"\nT y;\n',
            ),
        ]:
            with self.subTest(label):
                prefixes.forget()
                cdecl.resumable_parse(first, {})
                resumed = cdecl.resumable_parse(second, {})
                self.assertEqual(places(resumed), places(cdecl.parser({}).parse(second)))
                self.assertTrue(cache.retained("prefixes"), "the second parse must resume from the kept prefix")

    def test_the_marker_names_the_last_file_and_the_resumed_line(self) -> None:
        text = '# 3 "a.h"\nint a;\nint b;\n'
        self.assertEqual(cdecl.resume_marker(text, text.index("int b")), '# 4 "a.h"\n')
        self.assertEqual(cdecl.resume_marker("int a;\nint b;\n", 7), "# 2\n")
