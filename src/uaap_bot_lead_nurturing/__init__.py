"""Lead Nurturing: a UAAP bot, packaged on its own. Installing it registers it with the UAAP kernel."""

from uaap.bots.catalog import BotSpec

SPEC = BotSpec(
    id="lead-nurturing",
    name="Lead Nurturing",
    version="0.1.0",
    kind="deterministic",
    module="uaap_bot_lead_nurturing.agent",
    class_name="LeadNurturing",
    needs_model=False,
    needs_inbox=False,
    summary="Flags marketing leads needing follow-up; drafts one for approval. Platform workspace only.",
)
