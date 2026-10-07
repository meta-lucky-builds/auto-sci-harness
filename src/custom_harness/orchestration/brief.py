"""The typed helper brief (GOAL §15; OR orx-agent-delegation).

A brief is a closed schema: every field is required (an explicit empty `allowed_spends` means "no spends at all"),
unknown fields are refused, and the text must not lean on context the helper does not have. `allowed_spends` is a
whitelist: any compute or model-quota spend not listed in it is forbidden and the driver refuses it."""

from __future__ import annotations

import math
import re
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from . import params

_SLUG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
# git check-ref-format, conservatively: ASCII letters, digits and ._/- only; no leading '-', no '..', no '//',
# no trailing '/' or '.lock', no component starting with '.'.
_BRANCH = re.compile(r"^(?!-)(?!.*\.\.)(?!.*//)(?!.*/\.)(?!\.)[A-Za-z0-9._/-]{1,200}(?<![/.])(?<!\.lock)$")
_RESOURCE = re.compile(r"^[a-z][a-z0-9_-]*:[A-Za-z0-9_.:/-]{1,128}$")

SpendKind = Literal["compute", "model_quota"]
SpendUnit = Literal["cpu_seconds", "gpu_seconds", "wall_seconds", "calls", "tokens", "usd"]


def _text(v: str) -> str:
    if not isinstance(v, str) or not v.strip():
        raise ValueError("must be non-empty text")
    return v


class SpendGrant(BaseModel):
    """One authorised spend: up to `limit` units of `unit` on `resource` (e.g. 'local:cpu', 'model:<family>')."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    kind: SpendKind
    resource: str
    unit: SpendUnit
    limit: float

    @field_validator("resource")
    @classmethod
    def _res(cls, v: str) -> str:
        if not _RESOURCE.match(v):
            raise ValueError(f"resource {v!r} must look like '<namespace>:<name>'")
        return v

    @field_validator("limit")
    @classmethod
    def _lim(cls, v: float) -> float:
        if not math.isfinite(v) or v <= 0:
            raise ValueError("limit must be a finite number > 0")
        return float(v)

    def key(self) -> tuple[str, str, str]:
        return (self.kind, self.resource, self.unit)


def check_grants(grants: list[SpendGrant]) -> list[SpendGrant]:
    keys = [g.key() for g in grants]
    if len(set(keys)) != len(keys):
        raise ValueError("the same (kind, resource, unit) is granted twice")
    return grants


class HelperBrief(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    brief_id: str
    project: str
    branch: str
    worktree: str
    files: list[str] = Field(min_length=1)
    allowed_spends: list[SpendGrant]
    expected_output: str
    definition_of_done: str
    instructions: str

    @field_validator("project", "worktree", "expected_output", "definition_of_done", "instructions")
    @classmethod
    def _nonempty(cls, v: str) -> str:
        return _text(v)

    @field_validator("brief_id")
    @classmethod
    def _id(cls, v: str) -> str:
        if not _SLUG.match(v or ""):
            raise ValueError("brief_id: ASCII letters, digits, '_', '.', '-' (at most 64)")
        return v

    @field_validator("branch")
    @classmethod
    def _branch(cls, v: str) -> str:
        if not _BRANCH.match(v or ""):
            raise ValueError(f"branch {v!r} is not a safe git branch name")
        return v

    @field_validator("files")
    @classmethod
    def _files(cls, v: list[str]) -> list[str]:
        for f in v:
            _text(f)
            if (PurePosixPath(f).is_absolute() or PureWindowsPath(f).is_absolute() or PureWindowsPath(f).drive
                    or ".." in re.split(r"[\\/]", f)):
                raise ValueError(f"file {f!r} must be a path relative to the worktree, without '..'")
        return v

    @field_validator("allowed_spends")
    @classmethod
    def _spends(cls, v: list[SpendGrant]) -> list[SpendGrant]:
        return check_grants(v)

    @model_validator(mode="after")
    def _self_contained(self) -> HelperBrief:
        if len(self.instructions.strip()) < params.MIN_INSTRUCTIONS_CHARS:
            raise ValueError(f"instructions shorter than {params.MIN_INSTRUCTIONS_CHARS} characters cannot be "
                             f"self-contained")
        text = " ".join([self.instructions, self.expected_output, self.definition_of_done]).lower()
        hits = [p for p in params.CONTEXT_DEPENDENT_PHRASES if p in text]
        if hits:
            raise ValueError(f"brief refers to context the helper does not have: {hits}")
        return self


def brief_problems(raw: Any) -> list[str]:
    """Human-readable problems with a raw brief ([] if it is valid). Missing fields are named."""
    try:
        HelperBrief.model_validate(raw)
    except ValidationError as e:
        out = []
        for err in e.errors():
            loc = ".".join(str(x) for x in err["loc"]) or "brief"
            out.append(f"{loc}: {err['msg']}")
        return out
    return []


def parse_brief(raw: Any) -> HelperBrief:
    """Validate a raw brief; raises ValueError (pydantic's ValidationError is one) on any problem."""
    return HelperBrief.model_validate(raw)
