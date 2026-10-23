"""SSH with a PINNED host key, for the remote-Docker and Slurm backends (GOAL §17 Lab; PORTMAP §4, §10).

OpenResearch reaches its managed and rental boxes with `StrictHostKeyChecking=no` and `UserKnownHostsFile=/dev/null`
(OR src/jobs/ssh.rs `HostKeyPolicy::Ephemeral`): any key is accepted, so whoever answers on that address gets the
job, its source and the synced secrets. We do the opposite:
- the owner records the host's public key in the human-owned config (credentials.py); we write it to a private
  known_hosts file of our own and point ssh at that file ONLY (`-F none` ignores ~/.ssh/config, `GlobalKnownHostsFile
  none`, `HostKeyAlias` binds the pin to this config entry, `StrictHostKeyChecking=yes`, `UpdateHostKeys=no`);
- a mismatching or missing key fails the connection (ssh exit 255), which is an infrastructure failure, never a
  prompt and never "accept-new";
- the identity file comes from the owner config and is passed with `-i` and `IdentitiesOnly=yes`; the ssh agent is
  not forwarded (-a), nor X11 (-x), nor any port (ClearAllForwardings);
- the local side never runs a shell: argv lists only; the remote command is one string built from shlex.quote'd
  pieces.
Ported from OR: the per-operation `ssh` process and the stdin-carried payloads (src/jobs/ssh.rs `ssh_run`, run.sh
written via `cat >`); the connection options are ours (stricter than OR's every policy).
Built, NOT test-verified; never run against a real SSH host.
"""

from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path

from .jobs import CommandResult, RemoteError, Runner, subprocess_runner

_HOST = re.compile(r"(?:[A-Za-z0-9._-]+@)?[A-Za-z0-9._-]+")
_KEY = re.compile(r"(ssh-ed25519|ecdsa-sha2-nistp(?:256|384|521)|rsa-sha2-(?:256|512)|ssh-rsa) [A-Za-z0-9+/]+={0,3}")
SSH_CONNECTION_ERROR = 255


class PinnedSSH:
    """One `ssh` process per operation against exactly one pinned host."""

    def __init__(self, host: str, *, host_key: str, known_hosts_dir: Path, port: int = 22,
                 identity_file: str | None = None, runner: Runner | None = None, ssh: str = "ssh",
                 connect_timeout_s: int = 10):
        if not isinstance(host, str) or not _HOST.fullmatch(host) or host.startswith("-") or "@-" in host:
            raise ValueError(f"refusing SSH host {host!r} (must be [user@]hostname, never an option)")
        key = (host_key or "").strip()
        if not _KEY.fullmatch(key):
            raise ValueError("host_key must be ONE public key line '<type> <base64>' (the pin); accept-any is refused")
        if not isinstance(port, int) or not 0 < port < 65536:
            raise ValueError(f"bad port {port!r}")
        if identity_file is not None and (not identity_file or identity_file.startswith("-")):
            raise ValueError("bad identity file path")
        self.host, self.port, self.ssh = host, port, ssh
        self.identity_file = identity_file
        self.connect_timeout_s = int(connect_timeout_s)
        self._runner = runner or subprocess_runner
        self.alias = "chh-pin-" + hashlib.sha256(f"{host}:{port}".encode()).hexdigest()[:16]
        self.known_hosts = Path(known_hosts_dir) / f"{self.alias}.known_hosts"
        self._write_pin(key)

    def _write_pin(self, key: str) -> None:
        line = f"{self.alias} {key}\n"
        self.known_hosts.parent.mkdir(parents=True, exist_ok=True)
        if self.known_hosts.exists() and self.known_hosts.read_text(encoding="ascii") == line:
            return
        tmp = self.known_hosts.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="ascii", newline="\n") as f:
            f.write(line)
        os.replace(tmp, self.known_hosts)

    def argv(self, remote_command: str) -> list[str]:
        a = [self.ssh, "-F", "none", "-a", "-x", "-T", "-p", str(self.port),
             "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
             "-o", f"UserKnownHostsFile={self.known_hosts}", "-o", "GlobalKnownHostsFile=none",
             "-o", f"HostKeyAlias={self.alias}", "-o", "UpdateHostKeys=no", "-o", "CheckHostIP=no",
             "-o", "ForwardAgent=no", "-o", "ForwardX11=no", "-o", "ClearAllForwardings=yes",
             "-o", "PermitLocalCommand=no", "-o", f"ConnectTimeout={self.connect_timeout_s}"]
        if self.identity_file:
            a += ["-o", "IdentitiesOnly=yes", "-i", self.identity_file]
        return [*a, "--", self.host, remote_command]

    def run(self, remote_command: str, *, stdin: bytes | None = None, timeout_s: float = 120.0,
            max_output_bytes: int | None = None) -> CommandResult:
        from .credentials import tool_env
        kw = {} if max_output_bytes is None else {"max_output_bytes": int(max_output_bytes)}
        r = self._runner(self.argv(remote_command), stdin=stdin, timeout_s=timeout_s + self.connect_timeout_s,
                         env=tool_env(), **kw)
        if r.returncode == SSH_CONNECTION_ERROR:
            raise RemoteError(f"ssh {self.host}: connection failed or the host key does not match the pin "
                              f"({r.stderr.strip()[-500:]})", transient=True)
        return r

    def checked(self, what: str, remote_command: str, *, stdin: bytes | None = None,
                timeout_s: float = 120.0, max_output_bytes: int | None = None) -> CommandResult:
        r = self.run(remote_command, stdin=stdin, timeout_s=timeout_s, max_output_bytes=max_output_bytes)
        if r.timed_out or r.returncode != 0:
            raise RemoteError(f"ssh {self.host}: {what} failed (rc={r.returncode}, timed_out={r.timed_out}): "
                              f"{r.stderr.strip()[-1000:]}", transient=True)
        return r
