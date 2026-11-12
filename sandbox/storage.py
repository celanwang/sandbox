"""Atomic publication for local evidence files."""

import os
from collections.abc import Iterable
from pathlib import Path
from tempfile import NamedTemporaryFile


def write_atomic(path: Path, chunks: Iterable[str]) -> None:
    with NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as file:
        temporary = Path(file.name)
        try:
            file.writelines(chunks)
            file.flush()
            os.fsync(file.fileno())
            file.close()
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)
