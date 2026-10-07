"""custom-harness entropy pin (GOAL §16.5.1). TRUSTED code, mounted read-only into the sandbox at /opt/chh/pin and
put on the program's PYTHONPATH by the host, so the interpreter imports it at start-up (site.py imports
`sitecustomize`) before any agent code runs.

With CHH_ENTROPY_SEED set (hex), every Python-level entropy source a program normally reaches is replaced by one
deterministic stream, SHA-256(seed || counter):
  - os.urandom and os.getrandom (and so `secrets`, `uuid.uuid4`, random.SystemRandom, numpy's SeedSequence() and
    default_rng() with no seed, numpy's legacy global RandomState, scikit-learn's random_state=None);
  - random.Random(None).seed(None) of every new instance, including the `random` module's global instance;
  - torch's default generator (seeded right after `import torch`; torch otherwise seeds it from the OS in C++).
Hash randomisation is pinned separately by PYTHONHASHSEED (set by the host).

What this does NOT pin (stated, not hidden; see docs/hermetic_sandbox.md):
  - the clock (time.time, datetime.now, perf_counter): there is no compiler in the image for an LD_PRELOAD shim and
    Docker has no realtime-clock namespace;
  - entropy reached below Python: the getrandom syscall or /dev/urandom read through ctypes or a C extension that
    does not go through os.urandom; ASLR (object addresses: id()-ordered containers);
  - a program started with `python -I` or `-S` (no site import), or one that removes this module's effects.
So it makes HONEST programs deterministic; it is not a boundary against a program that wants randomness.
"""

import os as _os


def _install() -> None:
    seed_hex = _os.environ.get("CHH_ENTROPY_SEED")
    if not seed_hex:
        return
    import hashlib
    import operator

    key = bytes.fromhex(seed_hex)
    counter = [0]

    def urandom(size: int) -> bytes:
        size = operator.index(size)
        if size < 0:
            raise ValueError("negative argument not allowed")
        out = bytearray()
        while len(out) < size:
            out += hashlib.sha256(key + counter[0].to_bytes(8, "big")).digest()
            counter[0] += 1
        return bytes(out[:size])

    def getrandom(size: int, flags: int = 0) -> bytes:
        return urandom(size)

    _os.urandom = urandom
    if hasattr(_os, "getrandom"):
        _os.getrandom = getrandom
    import random

    random._urandom = urandom  # type: ignore[attr-defined]  # SystemRandom (and so `secrets`) reads this name
    original_seed = random.Random.seed

    def seed(self: random.Random, a: object = None, version: int = 2) -> None:
        if a is None:
            a = int.from_bytes(urandom(32), "big")
        original_seed(self, a, version)  # type: ignore[arg-type]

    random.Random.seed = seed  # type: ignore[method-assign]
    inst = random._inst  # type: ignore[attr-defined]  # the module-level functions are bound methods of this one
    inst.seed()  # it was seeded from the real urandom at import
    random.seed = inst.seed  # re-bound so that a later random.seed() goes through the pinned method

    # torch seeds its default generator from the OS (/dev/urandom in C++), below this module's reach: seed it right
    # after `import torch` with a value from the pinned stream (torch is not imported here: it is slow and optional).
    import importlib.abc
    import importlib.machinery
    import sys

    torch_seed = int.from_bytes(hashlib.sha256(key + b"torch").digest()[:8], "big") >> 1

    class _TorchSeeder(importlib.abc.MetaPathFinder):
        def find_spec(self, name: str, path: object = None, target: object = None) -> object:
            if name != "torch":
                return None
            sys.meta_path.remove(self)
            spec = importlib.machinery.PathFinder.find_spec(name, path)  # type: ignore[arg-type]
            if spec is None or spec.loader is None:
                return spec
            loader = spec.loader
            original_exec = loader.exec_module

            def exec_module(module: object) -> None:
                original_exec(module)  # type: ignore[arg-type]
                module.manual_seed(torch_seed)  # type: ignore[attr-defined]

            loader.exec_module = exec_module  # type: ignore[method-assign]
            return spec

    sys.meta_path.insert(0, _TorchSeeder())


_install()
del _install
