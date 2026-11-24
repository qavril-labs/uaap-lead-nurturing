"""Lead Nurturing (B8): flags marketing leads that need a follow-up and drafts one for approval.

Deterministic: fixed rules over the platform leads list, fixed draft wording, no model. Leads are
platform data, so only users of the platform workspace holding `platform.leads.read` can use it.

Rules (dates compared with the business date in the request):
- A lead without recorded consent is never contacted and never drafted for; it is listed apart.
- Leads that are booked or closed are skipped.
- No follow-up drafted yet: due after two days, urgent after seven.
- A follow-up was drafted, or the lead is already contacted: quiet after seven days since the last
  draft (or since the lead arrived, if there is none).

It reads leads (Tier 1) and saves a draft only after a different person approves (Tier 3). It never
sends anything: sending needs the notification provider (PLT-10), which is not built yet.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import date
from typing import Any

from uaap.agents.base import AgentCard, AgentInput, AgentReply, CaseStory, EdgeCase, Kpi, Trace, UseCase
from uaap.agents.deterministic import DeterministicBot
from uaap.agents.guardrails import MANDATORY_GUARDRAILS, GuardrailEvent
from uaap.runtime.runner import AgentRunner
from uaap.runtime.state import AgentState, AgentStatus
from uaap.tools.agents import AgentPrincipal
from uaap.tools.types import CallerContext

AGENT_ID = "lead-nurturing"
FIRST_FOLLOW_UP_AFTER_DAYS = 2
URGENT_AFTER_DAYS = 7
QUIET_AFTER_DAYS = 7

PRINCIPAL = AgentPrincipal(
    agent_id=AGENT_ID,
    display_name="Lead Nurturing",
    grants=frozenset({"platform.leads.read", "platform.leads.draft"}),
    tools=frozenset({"platform_list_leads", "platform_create_followup_draft"}),
)

_ERRORS: dict[str, str] = {
    "INTERNAL": "Something went wrong on our side. Nothing was saved or sent. Please try again.",
    "VALIDATION": "I could not use that request as written. Please check it and try again.",
    "UNAUTHENTICATED": "Your session has ended. Please sign in again.",
    "FORBIDDEN": "Leads are only available to people in the platform workspace who have been given access.",
    "NOT_FOUND": "I could not find that lead.",
    "CONFLICT": "That lead has not agreed to be contacted, so no draft was saved.",
    "TOOL_BLOCKED": "That action is not allowed for agents.",
    "RATE_LIMITED": "Too many requests right now. Please try again shortly.",
    "QUOTA_EXCEEDED": "The usage limit for this period has been reached. Please try again later.",
    "UPSTREAM_FAILED": "The leads service is unavailable. Nothing was saved or sent.",
    "UPSTREAM_TIMEOUT": "The leads service took too long to respond. Please try again.",
    "UPSTREAM_INVALID_RESPONSE": "The leads service returned data I could not verify, so I have not used it.",
    "UNGROUNDED_REPLY": "I could not verify my report against the leads, so I have not shown it.",
    "AGENT_DISABLED": "Lead follow-up has been switched off by an administrator.",
}

_INTEREST = {
    "early_access": "early access",
    "demo": "a demo",
    "partner": "a partnership",
    "trial": "a free trial",
}
_UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")


@dataclass(frozen=True)
class Flagged:
    level: str  # "urgent", "due" or "quiet"
    days: int
    lead: dict[str, Any]


def _day(value: str) -> date:
    return date.fromisoformat(value[:10])


def classify(leads: list[dict[str, Any]], today: str) -> tuple[list[Flagged], list[dict[str, Any]]]:
    """Returns (leads needing follow-up, leads without consent). Deterministic."""
    now = date.fromisoformat(today)
    flagged: list[Flagged] = []
    no_consent: list[dict[str, Any]] = []
    for lead in leads:
        if not lead.get("consentAt"):
            no_consent.append(lead)
            continue
        if lead.get("status") in ("booked", "closed"):
            continue
        last_draft = lead.get("lastDraftAt")
        since = (now - _day(last_draft or lead["createdAt"])).days
        if last_draft or lead.get("status") == "contacted":
            if since >= QUIET_AFTER_DAYS:
                flagged.append(Flagged("quiet", since, lead))
        elif since >= URGENT_AFTER_DAYS:
            flagged.append(Flagged("urgent", since, lead))
        elif since >= FIRST_FOLLOW_UP_AFTER_DAYS:
            flagged.append(Flagged("due", since, lead))
    order = {"urgent": 0, "due": 1, "quiet": 2}
    flagged.sort(key=lambda f: (order[f.level], -f.days, f.lead["id"]))
    return flagged, no_consent


def draft_text(lead: dict[str, Any]) -> tuple[str, str]:
    """Fixed wording filled only with the lead's own details. Nothing is invented."""
    topic = _INTEREST.get(lead.get("interest", ""), "your enquiry")
    first = str(lead["name"]).split()[0]
    where = f" at {lead['company']}" if lead.get("company") else ""
    subject = f"Following up on your request about {topic}"
    body = (
        f"Hello {first},\n\nThank you for getting in touch{where} about {topic}. "
        "Would you like to arrange a time to talk? Reply to this message and we will find a time "
        "that suits you.\n\nThe QAVRIL LABS team"
    )
    return subject, body


CARD = AgentCard(
    agent_id=AGENT_ID,
    name="Lead Nurturing",
    version="1.0.0",
    owner="Platform team",
    purpose=(
        "Show which marketing leads need a follow-up and save a follow-up draft for a person to approve. "
        "It never contacts anyone, never drafts for a lead without recorded consent, and sends nothing."
    ),
    use_cases=(
        UseCase(
            "Find leads that need a follow-up",
            "Which leads need a follow-up?",
            "Urgent, due and quiet leads, and the leads that have not agreed to be contacted.",
            "platform_list_leads",
            1,
        ),
        UseCase(
            "Draft a follow-up for one lead",
            "Draft a follow-up for lead <lead id>",
            "A proposed draft that is saved only after a different person approves it.",
            "platform_create_followup_draft",
            3,
        ),
    ),
    case_stories=(
        CaseStory(
            title="A demo request goes unanswered",
            actor="Platform operator",
            situation="A visitor asked for a demo several days ago and nobody has replied.",
            steps=(
                "The operator asks which leads need a follow-up.",
                "The lead is listed as urgent with how long it has waited.",
                "The operator asks for a draft; a colleague approves it and it is saved for review.",
            ),
            outcome="A reviewed draft exists for the lead; nothing has been sent.",
        ),
        CaseStory(
            title="A lead who did not agree to contact",
            actor="Platform operator",
            situation="An older lead has no recorded consent.",
            steps=(
                "The operator asks which leads need a follow-up.",
                "The lead appears only in the do-not-contact list.",
            ),
            outcome="No draft is offered or saved for that lead.",
        ),
    ),
    edge_cases=(
        EdgeCase("No leads", "Says there is nothing to follow up.", "test_no_leads_says_so"),
        EdgeCase("New lead after two days", "Listed as due.", "test_new_lead_after_two_days_is_due"),
        EdgeCase(
            "New lead after a week",
            "Listed as urgent, before due leads.",
            "test_new_lead_after_a_week_is_urgent",
        ),
        EdgeCase("Lead with a draft gone quiet", "Listed as quiet.", "test_lead_gone_quiet_is_flagged"),
        EdgeCase(
            "Lead without consent",
            "Listed apart; never flagged.",
            "test_lead_without_consent_is_never_flagged",
        ),
        EdgeCase("Booked or closed lead", "Skipped.", "test_booked_and_closed_leads_are_skipped"),
        EdgeCase(
            "Draft for a consenting lead",
            "Proposal awaiting approval.",
            "test_draft_is_proposed_for_approval",
        ),
        EdgeCase(
            "Draft for a lead without consent",
            "Refused; nothing proposed.",
            "test_draft_refused_without_consent",
        ),
        EdgeCase(
            "Draft for an unknown lead id",
            "Asks for a listed lead.",
            "test_draft_for_unknown_lead_is_refused",
        ),
        EdgeCase(
            "Caller outside the platform workspace", "Refused by the service.", "test_other_tenant_is_refused"
        ),
        EdgeCase("Anything that would send", "Nothing is sent.", "test_nothing_is_sent"),
    ),
    out_of_scope=(
        "Sending any message (needs the notification provider, PLT-10)",
        "Drafting for a lead without recorded consent",
        "Changing a lead's status or contact details",
        "Writing new wording; drafts use fixed text and the lead's own details",
    ),
    guardrails=MANDATORY_GUARDRAILS,
    error_handling=_ERRORS,
    kpis=(
        Kpi(
            "Leads waiting too long",
            "Consenting leads flagged urgent that have no draft.",
            "Zero urgent leads at the end of each working day",
        ),
    ),
    data_sources=(
        "The platform leads list (status, consent, dates, last draft)",
        "The business date in the request",
    ),
)


class LeadNurturing(DeterministicBot):
    card = CARD
    principal = PRINCIPAL

    def __init__(self, runner: AgentRunner, **kwargs: Any) -> None:
        super().__init__(runner, **kwargs)

    async def _fetch(
        self, caller: CallerContext, inp: AgentInput, trace: Trace
    ) -> tuple[list[dict[str, Any]] | None, AgentReply | None]:
        state, _ = await self.run_tool(
            caller, inp, trace, {"tool_name": "platform_list_leads", "arguments": {"limit": 100}}
        )
        if state.status != AgentStatus.COMPLETED:
            return None, self.failure_from_state(state)
        results = state.working_context.get("tool_results") or []
        leads = results[-1]["output"].get("leads") if results else None
        if not isinstance(leads, list):
            return None, self.error_reply("UPSTREAM_INVALID_RESPONSE", run_id=state.run_id)
        return leads, None

    async def _handle(self, caller: CallerContext, inp: AgentInput, trace: Trace) -> AgentReply:
        leads, failure = await self._fetch(caller, inp, trace)
        if failure is not None or leads is None:
            return failure or self.error_reply("INTERNAL")
        wanted = _UUID.search(inp.message)
        if wanted and "draft" in inp.message.lower():
            return await self._propose(caller, inp, trace, leads, wanted.group(0).lower())

        flagged, no_consent = classify(leads, inp.today)
        for f in flagged:
            trace.sources.add(str(f.days))
        if not flagged and not no_consent:
            return AgentReply("answered", "No lead needs a follow-up right now.", data={"findings": []})
        lines: list[str] = []
        if flagged:
            lines.append("These leads need a follow-up:")
            for f in flagged:
                lead = f.lead
                who = f"{lead['name']}, {lead.get('company') or 'no company'}"
                reason = {
                    "urgent": f"no follow-up yet, waiting {f.days} days",
                    "due": f"no follow-up yet, waiting {f.days} days",
                    "quiet": f"gone quiet, {f.days} days since the last contact",
                }[f.level]
                lines.append(f"- {f.level.upper()}: {who} (lead {lead['id']}): {reason}")
        else:
            lines.append("No lead needs a follow-up right now.")
        if no_consent:
            lines.append("Not to be contacted (no recorded consent):")
            lines += [f"- {lead['name']} (lead {lead['id']})" for lead in no_consent]
        findings = [{"lead_id": f.lead["id"], "level": f.level, "days": f.days} for f in flagged]
        return AgentReply("answered", "\n".join(lines), data={"findings": findings})

    async def _propose(
        self, caller: CallerContext, inp: AgentInput, trace: Trace, leads: list[dict[str, Any]], lead_id: str
    ) -> AgentReply:
        lead = next((x for x in leads if str(x["id"]).lower() == lead_id), None)
        if lead is None:
            trace.flag(GuardrailEvent.UNGROUNDED_IDENTIFIER)
            return AgentReply("needs_clarification", "Please give the id of a lead from the list.")
        if not lead.get("consentAt"):
            trace.flag(GuardrailEvent.OUT_OF_SCOPE)
            return AgentReply(
                "refused", "That lead has not agreed to be contacted, so I have not drafted anything."
            )
        subject, body = draft_text(lead)
        arguments = {"leadId": lead["id"], "subject": subject, "body": body}
        key = f"LND-{uuid.uuid5(uuid.NAMESPACE_URL, lead['id'] + inp.today).hex[:12].upper()}"
        action = {
            "tool_name": "platform_create_followup_draft",
            "arguments": arguments,
            "idempotency_key": key,
            "summary": f"Save a follow-up draft for {lead['name']} (lead {lead['id']}). Nothing is sent.",
        }
        state, token = await self.run_tool(caller, inp, trace, action)
        if state.status == AgentStatus.AWAITING_APPROVAL and state.pending_approval is not None:
            return AgentReply(
                "awaiting_approval",
                state.pending_approval.summary,
                run_id=state.run_id,
                approval=state.pending_approval,
                approval_token=token,
                data={"arguments": arguments},
            )
        return self.failure_from_state(state)

    def after_approval(self, state: AgentState) -> AgentReply:
        if state.status != AgentStatus.COMPLETED:
            return self.failure_from_state(state)
        results = state.working_context.get("tool_results") or []
        draft = results[-1]["output"].get("draft", {}) if results else {}
        return AgentReply(
            "answered",
            "The follow-up draft was saved for review. Nothing was sent.",
            run_id=state.run_id,
            data={"draft": draft},
        )
