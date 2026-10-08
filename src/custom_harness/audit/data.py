"""Fresh external data for a Claim Audit: OpenML datasets behind a hash-pinned local cache (GOAL §16.10).

- The cache holds `openml-<id>-<sha256>.arff` files. Every read re-hashes the bytes against the spec's pin; a
  missing or different file is refused (the audit then ends COULD NOT RUN), never silently replaced.
- Downloading is an OWNER decision: `OpenMLCache.ensure` fetches only when the caller passes a fetcher, and the
  CLI passes one only with `--allow-download`. The default fetcher (`http_fetcher`) uses the OpenML REST API; it is
  NOT exercised by the tests (they use local fixtures only) and has never been run by this workstream.
- `ingest` registers a dataset in the vault only for a claim that is ALREADY pre-registered (the claim is committed
  before the fresh data is touched). The train/test split is a function of the row position and the pinned hash
  only, never of the labels.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from ..kernel import DatasetRole, Kernel
from . import config
from .spec import OpenMLDataset

Fetcher = Callable[[int], tuple[bytes, dict[str, str]]]  # openml id -> (ARFF bytes, description fields)


class DataUnavailable(RuntimeError):
    """The pinned dataset cannot be provided (not cached, hash mismatch, unparsable). The audit ends COULD NOT RUN."""

    def __init__(self, msg: str, *, needs_owner: bool = False):
        super().__init__(msg)
        self.needs_owner = needs_owner


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class OpenMLCache:
    def __init__(self, root: Path):
        self.root = Path(root)

    def path(self, ds: OpenMLDataset) -> Path:
        return self.root / f"openml-{ds.openml_id}-{ds.sha256}.arff"

    def load(self, ds: OpenMLDataset) -> bytes:
        p = self.path(ds)
        if not p.is_file() or p.is_symlink():
            raise DataUnavailable(f"openml:{ds.openml_id} is not in the local cache ({p.name}); downloading it is an "
                                  f"owner decision (`chh audit fetch --allow-download`)", needs_owner=True)
        data = p.read_bytes()
        if sha256(data) != ds.sha256:
            raise DataUnavailable(f"openml:{ds.openml_id}: cached file does not match its pinned sha256 (refused)")
        return data

    def ensure(self, ds: OpenMLDataset, fetcher: Fetcher | None) -> bytes:
        """The pinned bytes from the cache, fetching them first only if a fetcher is given (owner decision)."""
        if self.path(ds).is_file() or fetcher is None:
            return self.load(ds)
        data, meta = fetcher(ds.openml_id)
        if sha256(data) != ds.sha256:
            raise DataUnavailable(f"openml:{ds.openml_id}: the fetched file's sha256 {sha256(data)[:16]} is not the "
                                  f"pinned {ds.sha256[:16]} (not cached)")
        self.root.mkdir(parents=True, exist_ok=True)
        self.path(ds).write_bytes(data)
        (self.root / f"openml-{ds.openml_id}-{ds.sha256}.json").write_text(
            json.dumps(meta, sort_keys=True, indent=1), encoding="utf-8")
        return data


def http_fetcher(timeout_s: float = 60.0) -> Fetcher:  # pragma: no cover - network path: owner decision, untested
    """OpenML REST: the JSON description names the ARFF file id and the licence. Used only with --allow-download."""

    def fetch(openml_id: int) -> tuple[bytes, dict[str, str]]:
        url = f"https://www.openml.org/api/v1/json/data/{int(openml_id)}"
        with urllib.request.urlopen(url, timeout=timeout_s) as r:
            desc = json.loads(r.read().decode("utf-8"))["data_set_description"]
        file_url = f"https://www.openml.org/data/v1/download/{int(desc['file_id'])}"
        with urllib.request.urlopen(file_url, timeout=timeout_s) as r:
            data = r.read()
        meta = {k: str(desc.get(k, "")) for k in ("name", "version", "licence", "default_target_attribute",
                                                  "file_id", "md5_checksum", "upload_date")}
        return data, meta

    return fetch


# ------------------------------------------------------------------ ARFF (dense) -> table
@dataclass(frozen=True)
class Table:
    header: tuple[str, ...]
    rows: tuple[tuple[str, ...], ...]


def parse_arff(data: bytes) -> tuple[list[tuple[str, str]], list[list[str]]]:
    """Dense ARFF only: (attributes [(name, type)], data rows). Sparse data and relational types are refused."""
    text = data.decode("utf-8")
    attrs: list[tuple[str, str]] = []
    rows: list[list[str]] = []
    in_data = False
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("%"):
            continue
        if not in_data:
            low = s.lower()
            if low.startswith("@attribute"):
                rest = s[len("@attribute"):].strip()
                if rest.startswith(("'", '"')):
                    q = rest[0]
                    end = rest.index(q, 1)
                    name, typ = rest[1:end], rest[end + 1:].strip()
                else:
                    name, _, typ = rest.partition(" ")
                    typ = typ.strip()
                if typ.lower().startswith("relational"):
                    raise DataUnavailable("relational ARFF attributes are not supported")
                attrs.append((name, typ))
            elif low.startswith("@data"):
                in_data = True
            continue
        if s.startswith("{"):
            raise DataUnavailable("sparse ARFF data is not supported")
        row = next(csv.reader([s], quotechar="'", skipinitialspace=True))
        rows.append([v.strip().strip('"') for v in row])
    if not attrs or any(len(r) != len(attrs) for r in rows):
        raise DataUnavailable("malformed ARFF: rows do not match the attribute list")
    return attrs, rows


def to_table(data: bytes, target: str) -> Table:
    """Features first (nominal ones as the index of their declared value; numeric as given), the label last as
    `label`. Rows with a missing value ('?') are dropped (a rule on the features and label presence only)."""
    attrs, rows = parse_arff(data)
    names = [n for n, _ in attrs]
    if target not in names:
        raise DataUnavailable(f"target attribute {target!r} is not in the dataset")
    ti = names.index(target)
    codes: dict[int, dict[str, str]] = {}
    for i, (_, typ) in enumerate(attrs):
        if typ.startswith("{") and i != ti:
            values = [v.strip().strip("'\"") for v in typ.strip("{}").split(",")]
            codes[i] = {v: str(k) for k, v in enumerate(values)}
        elif i != ti and typ.lower() not in ("numeric", "real", "integer"):
            raise DataUnavailable(f"attribute {names[i]!r} has unsupported type {typ!r}")
    out = []
    for r in rows:
        if any(v == "?" for v in r):
            continue
        feats = []
        for i, v in enumerate(r):
            if i == ti:
                continue
            if i in codes:
                if v not in codes[i]:
                    raise DataUnavailable(f"value {v!r} not declared for attribute {names[i]!r}")
                feats.append(codes[i][v])
            else:
                x = float(v)
                if not math.isfinite(x):
                    raise DataUnavailable(f"non-finite value in {names[i]!r}")
                feats.append(v)
        out.append((*feats, r[ti]))
    header = tuple(f"f{i}_{n}" for i, n in enumerate(names) if i != ti) + ("label",)
    if len(out) > config.MAX_ROWS:
        raise DataUnavailable(f"{len(out)} rows: over the laptop-scale cap of {config.MAX_ROWS}")
    return Table(header=header, rows=tuple(out))


def is_test_row(pin: str, index: int) -> bool:
    """The split: a hash of (pinned file hash, row position) against TEST_FRACTION. Never looks at a label."""
    d = hashlib.sha256(f"{pin}:{index}".encode()).digest()
    return int.from_bytes(d[:8], "big") < config.TEST_FRACTION * 2 ** 64


def _csv(rows: list[tuple[str, ...]]) -> bytes:
    buf = io.StringIO()
    csv.writer(buf, lineterminator="\n").writerows(rows)
    return buf.getvalue().encode()


@dataclass(frozen=True)
class Split:
    train: bytes  # public: features + label
    test_x: bytes  # public: features only
    test_y: bytes  # private: the label column
    n_train: int
    n_test: int


def split(table: Table, pin: str) -> Split:
    train: list[tuple[str, ...]] = []
    test: list[tuple[str, ...]] = []
    for i, r in enumerate(table.rows):
        (test if is_test_row(pin, i) else train).append(r)
    if len(test) < config.MIN_TEST_ROWS or not train:
        raise DataUnavailable(f"{len(test)} held-out rows (minimum {config.MIN_TEST_ROWS}) and {len(train)} "
                              f"training rows: too small to audit")
    return Split(train=_csv([table.header, *train]), test_x=_csv([table.header[:-1], *[r[:-1] for r in test]]),
                 test_y=_csv([("label",), *[(r[-1],) for r in test]]), n_train=len(train), n_test=len(test))


def ingest(kernel: Kernel, claim_hash: str, ds: OpenMLDataset, data: bytes) -> Split:
    """Register one fresh dataset (train split as dev data, held-out split as EXTERNAL) for a claim that is already
    pre-registered. Refused otherwise: fresh data are touched only after the claim is committed."""
    if not kernel.vault.registered(claim_hash):
        raise DataUnavailable("the audit claim is not pre-registered: fresh data are not touched before it is")
    if sha256(data) != ds.sha256:
        raise DataUnavailable(f"openml:{ds.openml_id}: bytes do not match the pin")
    sp = split(to_table(data, ds.target), ds.sha256)
    kernel.vault.register_dataset(ds.train_id, DatasetRole.DEV, public={"train.csv": sp.train}, private={})
    kernel.vault.register_dataset(ds.dataset_id, DatasetRole.EXTERNAL, public={"test_X.csv": sp.test_x},
                                  private={"test_y.csv": sp.test_y}, canary=f"CHH-AUDIT-CANARY-{ds.dataset_id}")
    return sp
