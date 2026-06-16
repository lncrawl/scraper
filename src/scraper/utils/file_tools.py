import os
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Union


@contextmanager
def atomic_write(path: Union[str, Path], mode: str = "wb") -> Iterator:
    """Atomically write to `path` via a sibling temp file.

    On success the temp file is renamed onto the target. On exception the temp
    file is removed and the original (if any) is left untouched.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    try:
        with os.fdopen(fd, mode) as f:
            yield f
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
