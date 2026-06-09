"""Escarp broker: the control plane for the persistent browser-slot pool.

Public surface lives in `daemon` and `cli`.
"""

from escarp.broker.slots import SlotLease, claim_slot, ports_for_slot

__all__ = ["SlotLease", "claim_slot", "ports_for_slot"]
