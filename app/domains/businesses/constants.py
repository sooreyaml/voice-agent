from __future__ import annotations

# Lifecycle states in which an owner may still change their agent. A suspended
# or closed organization is frozen: publishing would re-activate the phone
# number and bypass dunning, so both draft-save and publish are refused.
EDITABLE_LIFECYCLES = frozenset({"provisioning", "active"})


class ErrorCode:
    AGENT_NOT_PROVISIONED = "agent_not_provisioned"
    AGENT_LOCKED = "agent_locked"
    AGENT_DRAFT_NOT_FOUND = "agent_draft_not_found"
