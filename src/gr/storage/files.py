"""Content-addressed file store for uploaded documents: `<data_dir>/files/<sha256>`."""

import asyncio
import hashlib
import os
import re
import tempfile
from pathlib import Path

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class FileStore:
    def __init__(self, root: Path) -> None:
        self.root = root

    def path_for(self, sha256: str) -> Path:
        # The digest is the only path component, so a hostile name cannot escape the root.
        if not _SHA256_RE.match(sha256):
            raise ValueError("not a sha256 hex digest")
        return self.root / sha256

    async def put(self, content: bytes) -> str:
        """Store bytes atomically (tmp file + fsync + rename); returns their sha256."""
        return await asyncio.to_thread(self._put_sync, content)

    def _put_sync(self, content: bytes) -> str:
        digest = hashlib.sha256(content).hexdigest()
        target = self.path_for(digest)
        if target.exists():
            return digest
        self.root.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=self.root, prefix=".upload-")
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, target)
        except BaseException:
            Path(tmp_name).unlink(missing_ok=True)
            raise
        return digest

    async def read(self, sha256: str) -> bytes:
        return await asyncio.to_thread(self.path_for(sha256).read_bytes)

    def exists(self, sha256: str) -> bool:
        return self.path_for(sha256).exists()
