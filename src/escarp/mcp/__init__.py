"""MCP shim: exposes the broker's acquire/status/release to LLM agents.

The model never sees lockfiles, lease tokens, or heartbeat math. It sees
three verbs. The shim handles the plumbing.
"""
