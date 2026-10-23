"""The Lab's one credential (GOAL §17: scoped, spend-capped, no owner secrets): lab/credentials.py.

Fail-first design: before credentials.py, nothing stopped an owner's everyday ANTHROPIC_API_KEY, a subscription login
or an unbounded key from being handed to a lab box. Each test is one of those routes and must be refused; the record
test checks the value never reaches the store record.

Written 2026-10-23 by lab-agents; built, NOT run.
"""

from __future__ import annotations

import pytest

from custom_harness.lab import credentials as C
from custom_harness.lab.walls import lab_root

KEY = "sk-lab-0123456789abcdef0123456789"


def _spec(**kw):
    base = {"registry_key": "anthropic-api", "box_env": "ANTHROPIC_API_KEY", "spend_cap_usd": 20.0,
            "scope": "anthropic workspace lab, $20 monthly limit", "source_env": "CHH_LAB_CRED_ANTHROPIC"}
    base.update(kw)
    return C.CredentialSpec(**base)


def test_a_scoped_key_resolves_and_its_record_holds_no_value():
    cred = C.resolve(_spec(), environ={"CHH_LAB_CRED_ANTHROPIC": KEY})
    rec = cred.record()
    assert KEY not in repr(rec) and KEY not in repr(cred)
    assert rec["fingerprint"] == C.fingerprint(KEY) and rec["spend_cap_usd"] == 20.0
    assert cred.docker_env() == {"ANTHROPIC_API_KEY": KEY}


@pytest.mark.parametrize("source", ["ANTHROPIC_API_KEY", "OPENAI_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "MY_KEY"])
def test_default_cli_variables_are_never_a_source(source):
    """Attack: pass the owner's everyday key variable as the Lab credential."""
    with pytest.raises(C.CredentialRefused):
        C.resolve(_spec(source_env=source), environ={source: KEY})


def test_a_reused_owner_key_is_refused():
    """Attack: copy the owner's key into CHH_LAB_CRED_X; it equals ANTHROPIC_API_KEY on the host, so it is not
    scoped."""
    with pytest.raises(C.CredentialRefused, match="re-used"):
        C.resolve(_spec(), environ={"CHH_LAB_CRED_ANTHROPIC": KEY, "ANTHROPIC_API_KEY": KEY})


@pytest.mark.parametrize("key", ["anthropic-subscription", "openai-codex-cli", "google-subscription", "nope"])
def test_subscriptions_and_unknown_entries_are_refused(key):
    """Claude subscription is prohibited by the registry; the Codex subscription is allowed for the gateway but not
    in the Lab (not scoped, not capped); unknown entries fail the registry gate."""
    with pytest.raises(C.CredentialRefused):
        C.resolve(_spec(registry_key=key), environ={"CHH_LAB_CRED_ANTHROPIC": KEY})


@pytest.mark.parametrize("cap", [0.0, -1.0, C.LAB_MAX_SPEND_CAP_USD + 1])
def test_spend_cap_must_be_declared_and_bounded(cap):
    with pytest.raises(C.CredentialRefused):
        C.resolve(_spec(spend_cap_usd=cap), environ={"CHH_LAB_CRED_ANTHROPIC": KEY})


def test_credential_file_inside_the_lab_root_is_refused():
    """Attack: the key file sits where a box (or the agent of another session) can read it."""
    f = lab_root() / "keyfile"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(KEY, encoding="utf-8")
    with pytest.raises(C.CredentialRefused):
        C.resolve(_spec(source_env=None, source_file=str(f)), environ={})


def test_expired_record_is_refused():
    cred = C.resolve(_spec(ttl_s=60), environ={"CHH_LAB_CRED_ANTHROPIC": KEY}, now=1000.0)
    C.require_live(cred.record(), now=1059.0)
    with pytest.raises(C.CredentialRefused):
        C.require_live(cred.record(), now=1061.0)


def test_local_model_entry_takes_no_credential():
    with pytest.raises(C.CredentialRefused):
        C.resolve(_spec(registry_key="local-openai-compatible"), environ={"CHH_LAB_CRED_ANTHROPIC": KEY})
