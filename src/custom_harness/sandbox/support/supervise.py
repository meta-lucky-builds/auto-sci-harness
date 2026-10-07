"""custom-harness CPU-time supervisor (GOAL §16.8 "CPU-time limits with a contention witness"). TRUSTED code, stdlib
only. In Docker it is mounted read-only at /opt/chh and runs as the container's PID 1 (`python -I -S`), the agent
program as its child. Usage:

    python -I -S supervise.py --cpu-s 60 --wall-s 180 -- python solution.py ...
    python -I -S supervise.py --witness 2.0          # contention witness: a fixed CPU loop, reports wall/CPU

The program is charged the CPU seconds of EVERY process in the container: the cgroup v2 counter
/sys/fs/cgroup/cpu.stat `usage_usec` (only when this process is PID 1, i.e. alone in its container's cgroup and
PID namespace). Elsewhere (LocalSandbox, tests only) it charges the direct child's own CPU time (Windows:
GetProcessTimes; Linux: /proc/<pid>/stat), so grandchildren are not counted there.

Why the program cannot forge its record: PID 1 of a PID namespace receives no signal from inside the namespace that
it has no handler for (SIGKILL included), so the program cannot kill the supervisor; before printing its record the
supervisor kills every other process in the container, so nothing the program wrote can follow the record on
stderr; the host reads only the LAST record line. A missing or unreadable record is the program's failure.

The record is one line on stderr: `CHH-SUPERVISOR {json}` with keys rc, cpu_s, cpu_exceeded, wall_exceeded, wall_s,
mode. The supervisor exits 0; the host takes the program's code from the record.
"""

import json
import os
import signal
import subprocess
import sys
import time

MARK = "CHH-SUPERVISOR "
CGROUP_CPU = "/sys/fs/cgroup/cpu.stat"


def _cgroup_usage() -> float | None:
    try:
        with open(CGROUP_CPU, encoding="ascii") as f:
            for line in f:
                k, _, v = line.partition(" ")
                if k == "usage_usec":
                    return int(v) / 1e6
    except (OSError, ValueError):
        return None
    return None


def _process_cpu(proc: "subprocess.Popen[bytes]") -> float | None:
    if os.name == "nt":  # pragma: no cover - exercised on Windows test hosts only
        import ctypes
        from ctypes import wintypes

        c, e, k, u = (wintypes.FILETIME() for _ in range(4))
        handle = getattr(proc, "_handle", None)
        if handle is None or not ctypes.windll.kernel32.GetProcessTimes(  # type: ignore[attr-defined]
                int(handle), ctypes.byref(c), ctypes.byref(e), ctypes.byref(k), ctypes.byref(u)):
            return None
        ticks = sum((t.dwHighDateTime << 32) | t.dwLowDateTime for t in (k, u))
        return ticks / 1e7
    if proc.returncode is not None:  # reaped: its CPU (and that of the children it waited for) is in RUSAGE_CHILDREN
        import resource

        ru = resource.getrusage(resource.RUSAGE_CHILDREN)
        return ru.ru_utime + ru.ru_stime
    try:
        with open(f"/proc/{proc.pid}/stat", encoding="ascii") as f:
            fields = f.read().rsplit(")", 1)[1].split()
        return (int(fields[11]) + int(fields[12])) / os.sysconf("SC_CLK_TCK")
    except (OSError, ValueError, IndexError):
        return None


def _kill_all() -> None:
    """As PID 1: kill every other process of the container (never this one) and reap them (PID 1 adopts orphans)."""
    try:
        os.kill(-1, signal.SIGKILL)
    except OSError:
        pass
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            if os.waitpid(-1, os.WNOHANG) == (0, 0):
                time.sleep(0.01)
        except ChildProcessError:
            break


def supervise(cpu_s: float, wall_s: float, cmd: list[str], poll_s: float = 0.25) -> dict[str, object]:
    in_container = os.getpid() == 1 and _cgroup_usage() is not None
    base = _cgroup_usage() if in_container else 0.0
    t0 = time.monotonic()
    proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL)
    last = 0.0
    cpu_exceeded = wall_exceeded = False

    def used() -> float:
        u = (_cgroup_usage() or 0.0) - (base or 0.0) if in_container else _process_cpu(proc)
        return last if u is None else max(last, u)

    rc: int | None = None
    while True:
        try:
            rc = proc.wait(timeout=poll_s)
        except subprocess.TimeoutExpired:
            pass
        last = used()  # after the wait too: the final reading must include the child's last interval
        if rc is not None:
            break
        if last > cpu_s:
            cpu_exceeded = True
            break
        if time.monotonic() - t0 > wall_s:
            wall_exceeded = True
            break
    wall = time.monotonic() - t0
    if rc is None:  # we stop it: take its own status first (a later waitpid(-1) could reap it and lose the code)
        try:
            proc.kill()
            rc = proc.wait(timeout=30)
        except (OSError, subprocess.TimeoutExpired):
            rc = None
    if in_container:
        _kill_all()  # the program's orphans too: nothing it started may print after the record
        last = used()
    if last > cpu_s:
        cpu_exceeded = True
    return {"rc": rc, "cpu_s": round(last, 3), "cpu_exceeded": cpu_exceeded, "wall_exceeded": wall_exceeded,
            "wall_s": round(wall, 3), "mode": "cgroup" if in_container else "process"}


def witness(seconds: float) -> dict[str, object]:
    """A fixed CPU loop of about `seconds` of CPU on an idle host. Reports wall/CPU: about 1 on an idle host, larger
    when the host is contended (the loop got less CPU per wall second)."""
    w0, c0 = time.perf_counter(), time.process_time()
    x = 0
    while time.process_time() - c0 < seconds:
        for i in range(10000):
            x ^= i * 2654435761
    wall, cpu = time.perf_counter() - w0, time.process_time() - c0
    return {"witness_wall_s": round(wall, 3), "witness_cpu_s": round(cpu, 3), "ratio": round(wall / max(cpu, 1e-9), 3)}


def _undumpable() -> None:
    """Linux: make this process non-dumpable, so a same-uid program cannot read its memory (/proc/1/mem, ptrace)
    and learn the nonce."""
    if sys.platform != "linux":
        return
    try:
        import ctypes

        ctypes.CDLL(None).prctl(4, 0, 0, 0, 0)  # PR_SET_DUMPABLE = 4
    except (OSError, AttributeError):
        pass


def main(argv: list[str]) -> int:
    # The host writes a per-run nonce as the first line of stdin, read here BEFORE the program exists (the program
    # gets /dev/null as stdin): a record without the right nonce is not the supervisor's.
    _undumpable()
    nonce = sys.stdin.readline().strip() if sys.stdin is not None else ""
    if argv[:1] == ["--witness"]:
        rec = witness(float(argv[1]))
    else:
        try:
            sep = argv.index("--")
            opts, cmd = argv[:sep], argv[sep + 1:]
            cpu_s = float(opts[opts.index("--cpu-s") + 1])
            wall_s = float(opts[opts.index("--wall-s") + 1])
        except (ValueError, IndexError):
            sys.stderr.write("supervise: usage: --cpu-s S --wall-s S -- cmd...\n")
            return 2
        if not cmd:
            sys.stderr.write("supervise: no command\n")
            return 2
        try:
            rec = supervise(cpu_s, wall_s, cmd)
        except OSError as e:  # the program could not be started (e.g. missing interpreter): its own failure
            rec = {"rc": 127, "cpu_s": 0.0, "cpu_exceeded": False, "wall_exceeded": False, "wall_s": 0.0,
                   "mode": "none", "start_error": type(e).__name__}
    rec["nonce"] = nonce
    sys.stdout.flush()
    sys.stderr.write("\n" + MARK + json.dumps(rec, sort_keys=True) + "\n")
    sys.stderr.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
