# Hermetic sandbox, CPU-time limits and non-interference replay (GOAL §16.5.1, §16.5.4, §16.8)

Status labels: **verified** = output seen when this was written; **inferred**; **not done**.

## Entropy pin (§16.5.1) — partial
- `Sandbox.pin_entropy(EntropyPin.for_run(claim_hash, arm, seed))` gives every later exec one deterministic entropy
  stream. A trusted `sitecustomize.py` (sandbox/support/pin, mounted read-only at /opt/chh) replaces os.urandom /
  os.getrandom (so secrets, uuid4, SystemRandom, numpy SeedSequence/default_rng/legacy RandomState, sklearn
  random_state=None), seeds every unseeded random.Random, seeds torch's default generator right after
  `import torch`, and the host sets PYTHONHASHSEED, TZ=UTC, C.UTF-8 locale and 1 BLAS/OpenMP thread.
- **Verified** (Docker candidate image): unpinned, os.urandom, random, numpy, torch, hash() and set order all differ
  between two runs; pinned, all are byte-identical (probe 2026-10-07; tests/test_hermetic_docker.py,
  tests/test_hermetic_sandbox.py).
- **Not pinned** (stated): the clock (no compiler in the images — gcc/cc/tcc/clang/libfaketime all absent, checked —
  so no LD_PRELOAD shim without a download; Docker has no realtime-clock namespace), the getrandom syscall and
  /dev/urandom reached below Python (ctypes, C extensions), ASLR (`setarch -R` is refused by the container's
  seccomp profile, checked), programs started with `python -I/-S`. So the pin makes HONEST programs deterministic;
  it is not a boundary against a program that wants randomness. Such a program falls to the McNemar fallback.
- Needs owner: an image with libfaketime or a compiler (download) would pin the clock.
- Measurement (Docker candidate image, two runs per program per condition, predictions-file bytes compared;
  script scratchpad/flowsb-measure, not committed). **Verified**, partial:
  - panel of unseeded wine programs: unpinned 3/4 identical (`RandomForestClassifier()` without random_state
    differed), pinned 4/4 identical. Seeded control identical in both.
  - suite baselines: 6/6 identical unpinned (they seed); pinned not run (the run was stopped for machine time).
  - distinct agent solutions in data/sandboxes (14): unpinned 10/14 identical; pinned 10/14 identical. Every
    non-identical case had a FAILED run, none had two successful runs that differed. Failures: program errors in
    both conditions (e.g. liblinear on multiclass), plus one program (attempt4, 377 s per run unpinned) that hit
    the 900 s timeout twice pinned. A benchmark in the same image (BLAS matmul, SVC and ExtraTrees CV, 20k urandom
    calls; two runs per condition) showed the pin NOT slower (blas 0.74-0.77 s pinned vs 1.35-1.55 s unpinned; SVC
    and ExtraTrees equal; urandom 0.15 s vs 0.06 s per 20k calls), so the timeouts are **inferred** to be host
    contention (other workstreams and a test run were using the machine then), not the pin. Not re-run.
  - So: today's agent programs mostly seed themselves; the pin closes the unseeded case observed (RF).

## CPU-time limits (§16.8) — built
`Sandbox.exec(..., cpu_s=S)`: a trusted supervisor runs as the container's PID 1 (so the program cannot signal it),
charges the cgroup v2 CPU counter of every process in the container, kills at the limit, enforces the wall backstop
(`timeout_s`), kills all other processes before writing a nonce-bound record (nonce passed on stdin before the
program exists; supervisor non-dumpable). A missing or forged record is the program's failure. When the wall
backstop fires below the CPU limit, a contention witness (fixed CPU loop, fresh container) reports wall/CPU;
more than 1.5 = contended (INFRA: re-run the whole evaluation). Limits: the witness runs only after the attempt; WSL2
cgroup accounting is what Docker Desktop provides (inferred equal to native for usage_usec).

## Non-interference replay (§16.5.4) — built (library); Kernel wiring not done
`verify/noninterference.py: replay_noninterference(run, files, copies, production, regression=...)`. Flipped and
constant worlds for every declared truth copy; decisions (not bytes) compared to the STORED production output;
nondeterministic classification programs fall back to McNemar (production counted as a real run) with the
stealth margin printed; nondeterministic regression fails closed. `sandbox_runner(...)` runs the frozen program
in fresh pinned sandboxes. Tests port the review's devices (agreement device f=1 and f=0.02, prevalence leak,
replay-aware, fingerprint, self-similar device A, amplifier, noise opt-out): all exact-class devices caught in every
task where they cheated; McNemar caught the noise opt-out device in 14/20 tasks at f=0.10 and 20/20 at f=0.30
(the toy's 98% at f=0.10 used a Bernoulli world). Only declared copies are perturbed; copies baked into the image
are not yet replaceable. Wiring into K3 (ExecRecord carries the pin; the replay uses `preds_ref`) belongs to kstats.
