"""Credential custody for compute backends and Lab connectors (GOAL §17 "no owner secrets fan out to agents or
runs", invariant 1 "no secrets").

Rules this module enforces (built, NOT test-verified):
- Credentials live in ONE human-owned config file, `<data_dir>/human/lab_compute.yaml` by default. `<data_dir>/human`
  is already a protected path (sandbox/base.py: no sandbox workspace may overlap it), so no agent mount can see it.
  The path is never taken from an environment variable (an agent-influenced process could set one).
- Each backend gets only ITS section (`OwnerConfig.section(kind, name)`), as a frozen copy. Secret values are wrapped
  in `Secret`, whose repr/str are redacted; the plaintext is read only by `Secret.reveal()` at the moment a host-side
  tool (ssh, kubectl, the Modal or HF launcher) needs it.
- Secrets never enter a RunRequest, a job manifest or a run's environment. Host-side tool processes get
  `tool_env(...)`: a minimal environment (PATH and the OS basics) plus only the variables that tool needs, so unrelated
  owner secrets in the harness's own environment (model API keys and the like) do not reach vendor CLIs.
- On POSIX the file must not be group/world-writable (another local user could otherwise swap a host key pin).

Format (YAML; unknown top-level keys and backend kinds are refused here, unknown keys inside a backend section are
refused by that backend's `from_config` via `check_keys`, so a typo cannot silently disable a pin):

    backends:
      remote-docker:
        gpu1: {host: "user@203.0.113.7", port: 22, host_key: "ssh-ed25519 AAAA...", identity_file: "C:/.../id_ed25519",
               image: "registry.example/chh-candidate@sha256:<64 hex>", gpus: "all", remote_root: "chh-runs"}
      slurm:
        cluster1: {host: "user@login.example", port: 22, host_key: "...", identity_file: "...", partition: "gpu"}
      k8s:
        lab: {kubeconfig: "C:/.../kubeconfig", context: "lab", namespace: "chh", template_file: "C:/.../job.yaml",
              template_sha256: "<64 hex>", images: ["registry/x@sha256:..."],
              isolation_evidence: {egress_test_sha256: "<64 hex>", recorded_by: "owner", date: "2026-11-01"}}
      modal:
        default: {python: "C:/.../modal-venv/python.exe", token_id_file: "...", token_secret_file: "...",
                  image: "registry/x@sha256:...", isolation_evidence: null}
      ray:
        cluster1: {address: "http://10.0.0.5:8265"}
      hf-jobs:
        default: {namespace: "owner", token_file: "...", python: "C:/.../hf-venv/python.exe"}
    literature:
      alphaxiv: {enabled: false, tos_status: "unknown"}
      # enabled only by the owner, with written permission: {enabled: true, tos_status: "permitted",
      # permission_note: "...", recorded_by: "...", api_base: "https://...", web_base: "https://..."}; the alphaXiv
      # endpoints are named here, never in code (literature/alphaxiv.py)
"""

from __future__ import annotations

import os
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any

import yaml

from ..config import data_dir

CONFIG_NAME = "lab_compute.yaml"
BACKEND_KINDS = frozenset({"remote-docker", "slurm", "k8s", "modal", "ray", "hf-jobs", "ssh"})
TOP_KEYS = frozenset({"backends", "literature"})
# keys whose values are secrets or point at secret material: never logged, never copied into a run
SECRET_KEYS = frozenset({"token", "token_file", "token_id_file", "token_secret_file", "identity_file", "kubeconfig",
                         "password"})
# OS basics a host-side CLI needs to start at all; nothing else from the harness's environment is passed on
_BASE_ENV = ("PATH", "PATHEXT", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "HOME", "USERPROFILE", "TEMP",
             "TMP", "TMPDIR", "LANG", "LC_ALL")


class CredentialError(PermissionError):
    """The owner config is missing, unsafe or malformed (fail closed: the backend is unusable)."""


class Secret:
    """A secret value whose repr/str never shows it. `reveal()` is the only way to the plaintext."""

    __slots__ = ("_value",)

    def __init__(self, value: str):
        if not isinstance(value, str) or not value:
            raise CredentialError("a secret must be a non-empty string")
        self._value = value

    def reveal(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return "Secret(<redacted>)"

    __str__ = __repr__

    def __reduce__(self) -> Any:  # never pickled (a pickle would carry the plaintext)
        raise TypeError("Secret is not serialisable")


def default_config_path() -> Path:
    return data_dir() / "human" / CONFIG_NAME


def read_secret_file(path: str | Path) -> Secret:
    """The first line of a human-owned secret file (e.g. a token), wrapped."""
    p = Path(path)
    _check_owner_only(p)
    try:
        text = p.read_text(encoding="utf-8").splitlines()[0].strip()
    except (OSError, IndexError) as e:
        raise CredentialError(f"cannot read the secret file {p.name}: {type(e).__name__}") from e
    return Secret(text)


def _check_owner_only(p: Path) -> None:
    if not p.is_file():
        raise CredentialError(f"{p} is not a file")
    if os.name != "nt":
        mode = p.stat().st_mode
        if mode & (stat.S_IWGRP | stat.S_IWOTH):
            raise CredentialError(f"{p} is group- or world-writable: another user could change it")


@dataclass(frozen=True)
class OwnerConfig:
    """The parsed human-owned config. `section` hands out one backend's settings only."""

    path: Path | None
    raw: Mapping[str, Any]

    @classmethod
    def empty(cls) -> OwnerConfig:
        """No config: every remote backend and every gated connector is unusable (fail closed)."""
        return cls(None, MappingProxyType({}))

    @classmethod
    def load(cls, path: Path | None = None) -> OwnerConfig:
        p = Path(path) if path is not None else default_config_path()
        if not p.exists():
            return cls.empty()
        _check_owner_only(p)
        try:
            doc = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError) as e:
            raise CredentialError(f"unreadable owner config {p.name}: {type(e).__name__}") from e
        validate(doc)
        return cls(p, MappingProxyType(doc))

    def section(self, kind: str, name: str) -> Mapping[str, Any]:
        if kind not in BACKEND_KINDS:
            raise CredentialError(f"unknown backend kind {kind!r}")
        sec = ((self.raw.get("backends") or {}).get(kind) or {}).get(name)
        if not isinstance(sec, dict):
            raise CredentialError(f"no owner configuration for {kind}/{name}: the backend is unusable until the "
                                  f"owner adds it to {self.path or default_config_path()}")
        return MappingProxyType(dict(sec))

    def literature(self, name: str) -> Mapping[str, Any]:
        sec = (self.raw.get("literature") or {}).get(name)
        return MappingProxyType(dict(sec)) if isinstance(sec, dict) else MappingProxyType({})


def validate(doc: Any) -> None:
    if not isinstance(doc, dict):
        raise CredentialError("owner config must be a mapping")
    unknown = set(doc) - TOP_KEYS
    if unknown:
        raise CredentialError(f"unknown top-level keys {sorted(unknown)}")
    backends = doc.get("backends") or {}
    if not isinstance(backends, dict):
        raise CredentialError("'backends' must be a mapping")
    for kind, named in backends.items():
        if kind not in BACKEND_KINDS:
            raise CredentialError(f"unknown backend kind {kind!r} (dropped backends such as tinker or managed "
                                  "boxes have no configuration)")
        if not isinstance(named, dict):
            raise CredentialError(f"backends.{kind} must map names to settings")
        for name, sec in named.items():
            if not isinstance(name, str) or not isinstance(sec, dict):
                raise CredentialError(f"backends.{kind}.{name} must be a mapping")
    lit = doc.get("literature") or {}
    if not isinstance(lit, dict):
        raise CredentialError("'literature' must be a mapping")


def tool_env(extra: Mapping[str, str] | None = None, *, base: Mapping[str, str] | None = None) -> dict[str, str]:
    """Environment for a HOST-SIDE tool process (ssh, kubectl, a launcher): OS basics plus `extra` only."""
    src = os.environ if base is None else base
    env = {k: src[k] for k in _BASE_ENV if k in src}
    for k, v in (extra or {}).items():
        if not isinstance(k, str) or not isinstance(v, str):
            raise CredentialError("tool environment entries must be strings")
        env[k] = v
    return env


def check_keys(section: Mapping[str, Any], allowed: frozenset[str], required: frozenset[str], where: str) -> None:
    """Refuse unknown keys (a typo must not silently disable a pin) and missing required ones."""
    unknown = set(section) - allowed
    if unknown:
        raise CredentialError(f"{where}: unknown keys {sorted(unknown)}")
    missing = required - {k for k, v in section.items() if v not in (None, "")}
    if missing:
        raise CredentialError(f"{where}: missing required keys {sorted(missing)}")


def describe(section: Mapping[str, Any]) -> dict[str, Any]:
    """A loggable view of a section: secret-bearing keys are replaced by '<redacted>'."""
    return {k: ("<redacted>" if k in SECRET_KEYS else v) for k, v in section.items()}
