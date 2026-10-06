"""Content-addressed blob store (local filesystem; S3-compatible backend later, PLAN B5)."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any


class BlobStore:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, digest: str) -> Path:
        return self.root / digest[:2] / digest

    def put(self, data: bytes) -> str:
        digest = hashlib.sha256(data).hexdigest()
        path = self._path(digest)
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_bytes(data)
            os.replace(tmp, path)  # atomic; blobs are write-once
        return digest

    def get(self, digest: str) -> bytes:
        data = self._path(digest).read_bytes()
        if hashlib.sha256(data).hexdigest() != digest:
            raise OSError(f"blob {digest} is corrupted")
        return data

    def put_json(self, obj: Any) -> str:
        return self.put(json.dumps(obj, sort_keys=True, default=str).encode())

    def get_json(self, digest: str) -> Any:
        return json.loads(self.get(digest))

    def has(self, digest: str) -> bool:
        return self._path(digest).exists()
