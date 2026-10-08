"""Content-addressed blob store (local filesystem; S3-compatible backend later, PLAN B5)."""

from __future__ import annotations

import hashlib
import json
import os
import threading
import uuid
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
            # a temp name per writer: two writers of the same blob (concurrent runs storing identical bytes) used to
            # share `<digest>.tmp`, so one's os.replace could find it gone (tests/test_evalrace.py)
            tmp = path.with_name(f"{digest}.{os.getpid()}.{threading.get_ident()}.{uuid.uuid4().hex}.tmp")
            tmp.write_bytes(data)
            try:
                os.replace(tmp, path)  # atomic; blobs are write-once
            except OSError:
                # Windows refuses to replace a file another writer has open; the same digest means the same bytes,
                # so an existing, intact destination is the blob
                tmp.unlink(missing_ok=True)
                if not (path.exists() and hashlib.sha256(path.read_bytes()).hexdigest() == digest):
                    raise
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
