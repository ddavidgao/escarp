"""Escarp -- identity-aware runtime for parallel coding agents.

Public surface is the `escarp` and `escarp-mcp` CLIs and the broker's HTTP
API on http://127.0.0.1:7878. See README.md for the quick-start.

For programmatic access, import directly from the submodules:
    from escarp.broker import claim_slot, ports_for_slot
    from escarp.broker.lease import Broker
    from escarp.broker.discovery import discover_pool
"""

__version__ = "1.3.1"
__all__ = ["__version__"]
