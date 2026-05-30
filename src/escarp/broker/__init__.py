"""Escarp v2 broker: control-plane daemon for the worktree browser pool.

See V2_PLAN.md for the design. Public surface lives in `daemon` and `cli`.
"""

from escarp.broker.slots import SlotLease, claim_slot, ports_for_slot

__all__ = ["SlotLease", "claim_slot", "ports_for_slot"]
