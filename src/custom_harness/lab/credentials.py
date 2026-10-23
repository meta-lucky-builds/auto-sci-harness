"""The one credential a lab session may carry (GOAL §17: "scoped, spend-capped credentials inside the lab box; no
owner secrets fan out to agents or runs").

The owner creates a credential FOR THE LAB at the provider (e.g. a key in a dedicated Anthropic workspace or OpenAI
project with a spend limit) and passes it to `chh lab start` explicitly. Nothing here reads the owner's usual keys:

- the value comes from an environment variable whose name starts with `CHH_LAB_CRED_` (set by the owner for this one
  command), or from a file outside every agent-readable root and outside the lab root. `ANTHROPIC_API_KEY`,
  `OPENAI_API_KEY` and the other names the CLIs read by default are REFUSED as sources, so the owner's everyday key
  cannot be passed by accident;
- the value must not equal the value of any other secret-like variable in this process's environment (a re-used
  owner key is refused: it would not be scoped);
- the declared spend cap (USD, > 0, <= LAB_MAX_SPEND_CAP_USD) and scope are RECORDED. The cap is enforced by the
  provider on the scoped key, not by this code: stated, not hidden (docs/lab.md residual risks);
- the provider registry gate (`gateway.registry.require_usable`) must pass for the registry entry the credential is
  for, and subscription (BYOS) entries are refused in the Lab even when the registry allows them: a subscription login
  is the owner's own account, neither scoped nor spend-capped. Claude subscription use is `prohibited` in the
  registry anyway, so Claude Code in the Lab is API-key only (`anthropic-api`).

What is recorded in the store: the registry key, the in-box variable name, the source NAME (never the value), a
short fingerprint (first 16 hex of sha256 of the value), the spend cap, the scope note and the expiry. The value
reaches the box only as `docker run -e NAME` with the value in the docker CLI's environment (never in argv, never in
a file we write, never in the workspace).

Built, NOT test-verified.
"""

from __future__ import annotations

import hashlib
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .walls import LabWallError, forbidden_overlaps, lab_root, overlaps

SOURCE_PREFIX = "CHH_LAB_CRED_"
LAB_MAX_SPEND_CAP_USD = 200.0  # UNCALIBRATED (owner decision, GOAL §13.8): the largest cap one session may declare
LAB_MAX_TTL_S = 14 * 24 * 3600  # UNCALIBRATED (owner decision, GOAL §13.8): a session credential expires within this
_SECRET_NAME = re.compile(r"(?i)(key|token|secret|passw|credential|auth|cookie|session)")
# names agent CLIs read by default: never accepted as a SOURCE (they hold the owner's everyday keys)
DEFAULT_CLI_NAMES = frozenset({"ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN",
                               "OPENAI_API_KEY", "CODEX_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY",
                               "XAI_API_KEY", "DEEPSEEK_API_KEY", "MOONSHOT_API_KEY", "OPENROUTER_API_KEY",
                               "HF_TOKEN", "GH_TOKEN", "GITHUB_TOKEN"})
_NAME = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")


class CredentialRefused(PermissionError):
    pass


@dataclass(frozen=True)
class CredentialSpec:
    """What the owner declares at `chh lab start`."""

    registry_key: str  # provider registry entry this credential is for (gateway/providers.yaml)
    box_env: str  # the variable name the agent CLI reads INSIDE the box (e.g. ANTHROPIC_API_KEY)
    spend_cap_usd: float
    scope: str  # free text by the owner: where the key lives and what limits it (recorded)
    source_env: str | None = None  # CHH_LAB_CRED_<...> on the host
    source_file: str | None = None
    ttl_s: float = 24 * 3600


@dataclass(frozen=True)
class SessionCredential:
    """A resolved credential. `value` is held in memory only; `record()` never contains it."""

    spec: CredentialSpec
    provider: str
    fingerprint: str
    expires_at: float
    value: str = field(repr=False)

    def record(self) -> dict[str, Any]:
        s = self.spec
        return {"registry_key": s.registry_key, "provider": self.provider, "box_env": s.box_env,
                "source": f"env:{s.source_env}" if s.source_env else f"file:{s.source_file}",
                "fingerprint": self.fingerprint, "spend_cap_usd": s.spend_cap_usd,
                "spend_cap_enforced_by": "the provider, on the scoped key (not by the harness)",
                "scope": s.scope, "expires_at": self.expires_at}

    def docker_env(self) -> dict[str, str]:
        """{box_env: value} for the docker CLI's OWN environment (the argv carries only `-e box_env`)."""
        return {self.spec.box_env: self.value}


def fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def _read_source(spec: CredentialSpec, environ: dict[str, str]) -> str:
    if bool(spec.source_env) == bool(spec.source_file):
        raise CredentialRefused("give exactly one credential source: --credential-env CHH_LAB_CRED_... or "
                                "--credential-file PATH")
    if spec.source_env:
        name = spec.source_env
        if name in DEFAULT_CLI_NAMES or not name.startswith(SOURCE_PREFIX) or not _NAME.match(name):
            raise CredentialRefused(f"credential source {name!r} refused: it must be a variable named "
                                    f"{SOURCE_PREFIX}<NAME> set for the Lab, never a CLI's default key variable")
        value = environ.get(name, "")
    else:
        p = Path(str(spec.source_file))
        if overlaps(p, lab_root()):
            raise CredentialRefused(f"credential file {p} is inside the lab root: agents could read it")
        from ..commitment import default_agent_readable_roots
        for r in default_agent_readable_roots():
            if overlaps(p, r):
                raise CredentialRefused(f"credential file {p} is inside agent-readable root {r}")
        try:
            value = p.read_text(encoding="utf-8").strip()
        except OSError as e:
            raise CredentialRefused(f"credential file {p} unreadable: {type(e).__name__}") from e
    if len(value) < 16 or any(c.isspace() for c in value):
        raise CredentialRefused("the credential is empty, too short or contains whitespace")
    return value


def resolve(spec: CredentialSpec, *, environ: dict[str, str] | None = None,
            registry: dict[str, dict[str, Any]] | None = None, now: float | None = None) -> SessionCredential:
    """Check a declared credential against the Lab policy and return it. Fails closed (CredentialRefused)."""
    env = dict(os.environ if environ is None else environ)
    if not _NAME.match(spec.box_env):
        raise CredentialRefused(f"in-box variable name {spec.box_env!r} is not a plain upper-case name")
    if not (0 < spec.spend_cap_usd <= LAB_MAX_SPEND_CAP_USD):
        raise CredentialRefused(f"spend cap {spec.spend_cap_usd!r} USD refused: declare a cap in (0, "
                                f"{LAB_MAX_SPEND_CAP_USD}] that the provider enforces on this key")
    if not spec.scope.strip():
        raise CredentialRefused("declare the credential's scope (where the key lives and what limits it)")
    if not (0 < spec.ttl_s <= LAB_MAX_TTL_S):
        raise CredentialRefused(f"credential lifetime {spec.ttl_s!r}s refused (max {LAB_MAX_TTL_S}s)")
    from ..gateway.registry import ProviderBlocked, load_registry, require_usable
    reg = registry if registry is not None else load_registry()
    try:
        entry = require_usable(spec.registry_key, reg)
    except ProviderBlocked as e:
        raise CredentialRefused(str(e)) from e
    if entry.get("mode") == "byos":
        raise CredentialRefused(f"{spec.registry_key}: subscription (BYOS) credentials are refused in the Lab: a "
                                "subscription login is the owner's own account, neither scoped nor spend-capped "
                                "(GOAL §17). Use an API key for a dedicated, capped project instead.")
    if entry.get("mode") == "ow" and entry.get("auth_kind") == "none":
        raise CredentialRefused(f"{spec.registry_key}: this entry needs no credential; start without one")
    value = _read_source(spec, env)
    for k, v in env.items():
        if k == spec.source_env or not _SECRET_NAME.search(k):
            continue
        if v and v == value:
            raise CredentialRefused(f"the credential equals the value of host variable {k!r}: an owner key "
                                    "re-used for the Lab is not scoped; create a dedicated key")
    t = time.time() if now is None else now
    return SessionCredential(spec=spec, provider=str(entry.get("provider")), fingerprint=fingerprint(value),
                             expires_at=t + spec.ttl_s, value=value)


def require_live(record: dict[str, Any], now: float | None = None) -> None:
    """Refuse an expired credential record (attach/exec after expiry)."""
    t = time.time() if now is None else now
    exp = record.get("expires_at")
    if not isinstance(exp, int | float) or t >= float(exp):
        raise CredentialRefused("the session credential has expired: stop the session and start a new one")


def host_paths_checked() -> list[str]:
    """For the session record: the forbidden host paths the walls were checked against (names only)."""
    return sorted({why for _p, why in forbidden_overlaps()})


__all__ = ["CredentialRefused", "CredentialSpec", "LabWallError", "SessionCredential", "fingerprint", "require_live",
           "resolve"]
