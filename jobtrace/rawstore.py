"""Content-addressed raw HTML evidence and an append-only manifest."""
from __future__ import annotations
import json
import os
import re
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterator
from .clock import Clock, iso_z
from .errors import RawStoreError
from .fingerprint import canonical_json, sha256_bytes
_HASH = re.compile(r"^[0-9a-f]{64}$")
@dataclass(frozen=True)
class RawDocument:
    sha256: str
    url: str
    kind: str
    http_status: int
    content_type: str
    size_bytes: int
    fetched_at: str
    run_id: str
    relative_path: str
    def as_dict(self) -> dict[str, object]: return asdict(self)
class RawStore:
    def __init__(self, root: str | Path, clock: Clock):
        self.root = Path(root)
        self.clock = clock
        self.objects = self.root / "objects"
        self.manifest = self.root / "manifest.jsonl"
    def _ensure(self) -> None:
        self.objects.mkdir(parents=True, exist_ok=True)
        self.root.mkdir(parents=True, exist_ok=True)
    def object_path(self, digest: str) -> Path:
        if not _HASH.match(digest): raise RawStoreError(f"invalid SHA-256 digest: {digest!r}")
        return self.objects / digest[:2] / f"{digest}.html"
    def put(self, content: bytes, *, url: str, kind: str, run_id: str,
            http_status: int = 200, content_type: str = "text/html",
            fetched_at: str | None = None) -> RawDocument:
        if not isinstance(content, bytes): raise RawStoreError("raw evidence must be bytes")
        if not kind: raise RawStoreError("document kind must not be empty")
        if not run_id: raise RawStoreError("run_id must not be empty")
        self._ensure()
        digest = sha256_bytes(content)
        path = self.object_path(digest)
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            handle, temp_name = tempfile.mkstemp(prefix=".raw-", dir=path.parent)
            try:
                with os.fdopen(handle, "wb") as stream:
                    stream.write(content)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temp_name, path)
            finally:
                if os.path.exists(temp_name): os.unlink(temp_name)
        record = RawDocument(
            sha256=digest, url=url, kind=kind, http_status=int(http_status),
            content_type=content_type.split(";", 1)[0].strip(), size_bytes=len(content),
            fetched_at=fetched_at or iso_z(self.clock.now()), run_id=run_id,
            relative_path=path.relative_to(self.root).as_posix(),
        )
        with self.manifest.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(canonical_json(record.as_dict()) + "\n")
        return record
    def read(self, digest: str) -> bytes:
        path = self.object_path(digest)
        if not path.is_file(): raise RawStoreError(f"raw evidence not found: {digest}")
        return path.read_bytes()
    def verify(self, record: RawDocument) -> bool:
        content = self.read(record.sha256)
        return len(content) == record.size_bytes and sha256_bytes(content) == record.sha256
    def iter_manifest(self) -> Iterator[RawDocument]:
        if not self.manifest.exists(): return
        with self.manifest.open("r", encoding="utf-8") as stream:
            for line_number, line in enumerate(stream, start=1):
                if not line.strip(): continue
                try:
                    payload = json.loads(line)
                    yield RawDocument(**payload)
                except (TypeError, ValueError, json.JSONDecodeError) as exc:
                    raise RawStoreError(f"invalid manifest line {line_number}: {exc}") from exc
