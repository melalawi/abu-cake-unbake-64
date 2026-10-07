"""SQLite connections with process-local temporary tables and sort storage.

temp_store_directory is deprecated and process-global, so changing it races other connections.
MEMORY prevents SQLite's independent default-directory search; journals stay beside the database.
"""

import sqlite3
from pathlib import Path


def connect(file: str | Path, *, uri: bool = False) -> sqlite3.Connection:
    connection = sqlite3.connect(file, uri=uri)
    try:
        connection.execute("PRAGMA temp_store=MEMORY")
    except BaseException:
        connection.close()
        raise
    return connection
