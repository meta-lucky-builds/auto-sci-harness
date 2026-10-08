"""Runs INSIDE a sandbox container (stdlib only): find dataset-like files the program could read.

Two independent probes over the whole readable filesystem (minus /proc, /sys, /dev):
  1. by shape: data/array/table/image/archive file types, any non-code file under a tests/test/data/datasets/
     descr/images directory of an installed package;
  2. by content: distinctive rows of the five real suite datasets, in plain text and inside gzip/bz2/xz/zip
     members (catches a copy under any name or format that keeps the row text).
Prints one JSON object: {"shape": [paths], "content": {needle_name: [paths]}, "unreadable": n}.
Usage: python scan_bundled_data.py <json allowlist of paths>
"""

import bz2
import gzip
import io
import json
import lzma
import os
import sys
import zipfile

SKIP_ROOTS = ("/proc", "/sys", "/dev")
CODE = {".py", ".pyc", ".pyi", ".pyx", ".pxd", ".pxi", ".h", ".hpp", ".hxx", ".c", ".cc", ".cpp", ".cuh", ".cu",
        ".so", ".a", ".cmake", ".typed", ".pth", ".exe", ".dll"}
DATA = {".csv", ".tsv", ".arff", ".npz", ".npy", ".pkl", ".pickle", ".joblib", ".mat", ".h5", ".hdf5", ".hdf",
        ".parquet", ".feather", ".data", ".svmlight", ".libsvm", ".xls", ".xlsx", ".sav", ".dta", ".sas7bdat",
        ".nc", ".pt", ".pth.tar", ".ckpt", ".jpg", ".jpeg", ".png", ".bmp", ".gif", ".tif", ".tiff", ".wav",
        ".idx", ".ubyte"}
ARCHIVE = {".gz", ".bz2", ".xz", ".zip", ".tar", ".tgz", ".7z", ".z"}
DATA_DIRS = {"tests", "test", "testing_data", "testdata", "test_data", "data", "datasets", "descr", "images",
             "sample_data", "sample-data"}
# directories whose name matches DATA_DIRS but which hold only library code/headers, never sample data
CODE_DIRS = ("/torch/include/", "/torch/utils/data/", "/torch/distributed/elastic/utils/data/")
NEEDLES = {
    "iris": [b"5.1,3.5,1.4,0.2", b"5.099999999999999645e+00 3.5"],
    "wine": [b"14.23,1.71,2.43,15.6"],
    "breast_cancer": [b"17.99,10.38,122.8"],
    "digits": [b"0,0,5,13,9,1,0,0,0,0,13,15,10,15,5"],
    "diabetes": [b"59 2 32.1 101.0 157 93.2"],
}
MAX_READ = 64 << 20


def _ext(name: str) -> tuple[str, str]:
    low = name.lower()
    base, ext = os.path.splitext(low)
    inner = os.path.splitext(base)[1] if ext in ARCHIVE else ""
    return ext, inner


def _payloads(path: str, raw: bytes) -> list[bytes]:
    out = [raw]
    try:
        if raw[:2] == b"\x1f\x8b":
            out.append(gzip.decompress(raw))
        elif raw[:3] == b"BZh":
            out.append(bz2.decompress(raw))
        elif raw[:6] == b"\xfd7zXZ\x00":
            out.append(lzma.decompress(raw))
        elif raw[:4] == b"PK\x03\x04":
            with zipfile.ZipFile(io.BytesIO(raw)) as z:
                out += [z.read(n) for n in z.namelist() if z.getinfo(n).file_size <= MAX_READ]
    except Exception:  # noqa: S110 - a corrupt archive is just bytes (still searched raw above)
        pass
    return out


def main() -> None:
    allow = set(json.loads(sys.argv[1])) if len(sys.argv) > 1 else set()
    me = os.path.realpath(__file__)  # this script holds the needles itself
    shape, content, unreadable = [], {k: [] for k in NEEDLES}, 0
    for root, dirs, files in os.walk("/"):
        if root.startswith(SKIP_ROOTS):
            dirs[:] = []
            continue
        for f in files:
            p = os.path.join(root, f)
            if os.path.islink(p) or not os.path.isfile(p) or os.path.realpath(p) == me:
                continue
            ext, inner = _ext(f)
            parts = set(p.split("/")[:-1])
            in_pkg = "site-packages" in parts or "dist-packages" in parts
            looks = (ext in DATA or inner in DATA or ext in ARCHIVE
                     or (in_pkg and ext not in CODE and parts & DATA_DIRS and not any(c in p for c in CODE_DIRS)))
            if looks and p not in allow:
                shape.append(p)
            try:
                if os.path.getsize(p) > MAX_READ:
                    continue
                with open(p, "rb") as fh:
                    raw = fh.read()
            except OSError:
                unreadable += 1
                continue
            for blob in _payloads(p, raw):
                for name, needles in NEEDLES.items():
                    if any(n in blob for n in needles) and p not in content[name]:
                        content[name].append(p)
    print(json.dumps({"shape": sorted(shape), "content": content, "unreadable": unreadable}))


if __name__ == "__main__":
    main()
