"""Compact cartridge fixtures; full checksum vectors live in header tests."""

import zlib
from unittest.mock import patch


def checksum(data, cic):
    return zlib.crc32(data[0x1000:]), zlib.adler32(data[0x1000:])


def install(case):
    from unbake.project import header

    mock = patch.object(header, "checksum", side_effect=checksum)
    mock.start()
    case.addCleanup(mock.stop)
