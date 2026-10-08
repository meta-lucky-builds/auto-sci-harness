# SSH compute backend (trusted code only)

GOAL §5 L0 "Compute backends", §10 invariant 1. Code: `src/custom_harness/compute/ssh.py`,
`compute/backends.py` (`SSHBackend`). Tests: `tests/test_compute_ssh.py`.

## What is built

- `SSHTransport` (ABC): `make_new_dir` (parents as needed; the leaf must not exist, so a directory is never reused),
  `upload` (entries from `collect_upload`, relative paths kept), `exec` (argv, cwd = the remote dir, added env, a
  timeout; returns returncode/stdout/stderr/wall/timed_out), optional `fetch` (one file's bytes).
- `collect_upload(local_dir)`: host-side walk of the materialised revision. Directories and regular files only.
  Anything `is_link_like` (symlink, junction, reparse point, entry that cannot be lstat'ed) or special (FIFO, device,
  socket) is refused with `UploadRefused` before the remote is touched. Consequence: a snapshot revision that
  contains symlinks cannot run on SSH (it is refused, never followed).
- `SSHSandbox`: each `exec` creates a NEW remote directory `<remote_root>/<local run dir name>-<random>`, uploads,
  then runs. A retry of the same request gets a different remote directory and never inherits a previous attempt's
  files. CPU supervision (`cpu_s`) and entropy pinning are refused (`RemoteNotBuilt`), not silently ignored.
- `SSHBackend(store, runs_root, *, host, transport=None, remote_root="chh-runs")`: isolation class TRUSTED_ONLY,
  unchanged (container, no_secrets, read-only root, non-root all False; network "unknown"). `run()` is the shared
  `ComputeBackend.run`: admission first (agent code refused with or without an approval; trusted code needs a recorded
  human approval of exactly that request), then local materialisation into a fresh directory, then the remote run.
  With `transport=None` an admitted run raises `RemoteNotBuilt` exactly as before. `environment()` records
  `backend`, `host`, `remote_root` and the `remote_dir` used.
- `OpenSSHTransport`: the system `ssh` client, one process per operation, via `sandbox.base.run_bounded` (argv list,
  `Popen` without a shell, bounded output, local timeout). Options: `-a -x -T`, `BatchMode=yes`,
  `StrictHostKeyChecking=yes` (the host key must already be in known_hosts; never `accept-new`), `ForwardAgent=no`,
  `ForwardX11=no`, `ClearAllForwardings=yes`, `ConnectTimeout`. The host is validated (`[user@]host`, never starting
  with `-`) and placed after `--`. The remote command is one string made of `shlex.quote`d pieces:
  - mkdir: `umask 077 && mkdir -p -- <parent> && mkdir -- <dir>`
  - upload: a tar stream (only the collected entries) on stdin to `tar -x -f - --no-same-owner -C <dir>`
  - exec: `cd -- <dir> && exec env -- K=V... timeout -k 5 <secs> <argv...>`, plus a local backstop of
    timeout + connect timeout + 30 s
  - fetch: `base64 -- <dir>/<rel>` (binary-safe through the text capture; over the output cap is an error)

## Trust boundary

A plain SSH host runs programs as the remote user with that host's files, credentials and network. It is never an
isolation boundary, so it runs TRUSTED code only, each request approved by a human on the ledger. The caller declares
the code's origin (default AGENT, fail closed); a caller that mislabels agent code as TRUSTED is not detected here
(same boundary as `compute/backend.py`). Environment variables are passed as given to trusted code, and the remote
login environment is inherited (not cleared).

## NOT verified

- `OpenSSHTransport` is **not verified against a real SSH host**. Only the argv it builds is tested (fake runner, and
  a fake `Popen` for the no-shell check). No real `ssh`/`scp` was ever invoked.
- Remote requirements are assumed: POSIX shell, `mkdir`, `tar`, `env`, `base64`, GNU coreutils `timeout`.
- Exit codes are ambiguous: ssh itself returns 255 on connection failure, which `exec` reports as the program's
  return code; remote `timeout` exits 124/137, which `exec` reports as timed out even if the program chose that code.
- Killing the local `ssh` client on the local backstop does not guarantee the remote process dies (the remote
  `timeout` is the intended guard).
- Remote run directories are never cleaned up (nothing is deleted).
- Executable bits are not carried from a Windows host (files upload as 0644).

## Not built (needs the owner)

gVisor and cloud-GPU backends are NOT built and no fake versions exist: both need installs or accounts that only the
owner can set up.
