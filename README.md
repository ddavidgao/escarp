# escarp

Identity-aware runtime for AI agents. Routes agents to the right mode of web access — autonomous (signed self-identity), delegated (scoped OAuth), or supervised (human-gated session inherit) — with strict isolation between modes and between agents and the user's personal browser state.

**Status:** v0 — Mode A (autonomous) only. Modes B and C coming in v0.1 and v0.2.

## Install

```bash
pip install escarp
```

## Quick start

```python
from escarp import run

result = run(task="fetch llms.txt from anthropic.com")
```

## License

MIT
