"""MCP shim: exposes the broker's acquire/status/release to LLM agents.

Per V2_PLAN.md Phase 4: the model never sees lockfiles, lease tokens, or
heartbeat math. It sees three verbs. The shim handles the plumbing.
"""
