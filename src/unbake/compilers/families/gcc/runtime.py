"""Prove native GCC runtime helper identity from complete code and literal values.

The signed DI-to-DF sequence first forms unsigned magnitude with a low-word
borrow, converts each word as signed and corrects negative words by 2**32,
scales the high word twice by 2**16, adds the low word and restores the sign.
Every instruction (including all branch and return delay slots) is required.
Only the three literal-address relocation pairs may vary; their actual ROM
bytes must represent the exact powers of two. No cartridge/address/name cue
establishes identity. Other implementations remain unrecognized.
"""

import struct
from collections.abc import Callable

from unbake.compilers.families.types import RuntimeHelper
from unbake.config import Held

_PATTERN = (
    8400929,
    10500129,
    79757318,
    10273,
    473123,
    405539,
    462891,
    12726307,
    604307457,
    399363,
    399299,
    1149440000,
    1182797985,
    73465860,
    0,
    1006698496,
    3558866944,
    1176506496,
    1006698496,
    3558866944,
    1176506498,
    1149706240,
    1182802209,
    1176506498,
    81854468,
    0,
    1006698496,
    3558866944,
    1176510720,
    278921218,
    1176768512,
    1176502279,
    65011720,
    0,
)
_RELOCATIONS = frozenset({15, 16, 18, 19, 26, 27})
_CONSTANTS = (4294967296.0, 65536.0, 4294967296.0)


def helpers(data: bytes, read_memory: Callable[[int, int], bytes]) -> tuple[tuple[int, RuntimeHelper], ...]:
    result = []
    anchor = struct.pack(">II", *_PATTERN[:2])
    start = data.find(anchor)
    while start >= 0:
        body = data[start : start + len(_PATTERN) * 4]
        if start % 4 == 0 and len(body) == len(_PATTERN) * 4:
            words = struct.unpack(">34I", body)
            normalized = tuple(word & 0xFFFF0000 if i in _RELOCATIONS else word for i, word in enumerate(words))
            if normalized == _PATTERN:
                try:
                    constants = []
                    for high in (15, 18, 26):
                        address = ((words[high] & 0xFFFF) << 16) + ((words[high + 1] & 0xFFFF) ^ 0x8000) - 0x8000
                        constants.append(struct.unpack(">d", read_memory(address & 0xFFFFFFFF, 8))[0])
                    if tuple(constants) == _CONSTANTS:
                        result.append(
                            (
                                start,
                                RuntimeHelper(
                                    "__floatdidf",
                                    len(body),
                                    (("a0/a1", "signed long long"),),
                                    ("f0/f1", "double"),
                                    "complete sign/borrow/COP1 conversion sequence and exact ROM literal powers",
                                ),
                            )
                        )
                except (Held, OSError, ValueError, struct.error):
                    pass
        start = data.find(anchor, start + 4)
    return tuple(result)
