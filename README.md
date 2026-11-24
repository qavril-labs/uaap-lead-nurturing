# uaap-lead-nurturing

**Lead Nurturing** (deterministic bot, no model): Flags marketing leads needing follow-up; drafts one for approval. Platform workspace only.

This bot runs on the UAAP kernel, [qavril-labs/uaap](https://github.com/qavril-labs/uaap). Installing this
package registers it with the kernel through the `uaap.bots` entry point, so the kernel's service, CLI and
MCP server pick it up.

```
pip install "uaap-lead-nurturing @ git+https://github.com/qavril-labs/uaap-lead-nurturing.git"
python -m uaap.bots describe lead-nurturing
```

In code:

```python
from uaap.bots import BotHost
host = BotHost(tools=register_my_tools)
bot = host.build("lead-nurturing")
```

Develop: `pip install -e ".[dev]"`, then `ruff check .` and `pytest`.
The test doubles come from the kernel (`uaap.testing`). The agent standard is in the kernel's docs;
`tests/test_conformance.py` checks this bot against it.
