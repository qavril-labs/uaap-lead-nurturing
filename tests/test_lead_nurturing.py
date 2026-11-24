"""Lead Nurturing: deterministic follow-up flags and approval-gated drafts; nothing is ever sent."""

from __future__ import annotations

from typing import Any

import pytest
from uaap.agents.base import AgentCatalog, AgentInput, AgentReply
from uaap.errors import UaapError
from uaap.obs.metrics import AgentMetrics
from uaap.runtime.approval.hooks import ApprovalEngine
from uaap.runtime.checkpointer.in_memory import InMemoryCheckpointStore
from uaap.runtime.runner import AgentRunner
from uaap.testing.fakes import (
    OTHER_PLATFORM_TOKEN,
    PLATFORM_APPROVER_TOKEN,
    PLATFORM_OPERATOR_TOKEN,
    FakeCoreSaas,
    build_stack,
    caller,
)

from uaap_bot_lead_nurturing.agent import LeadNurturing, classify, draft_text

TODAY = "2026-11-23"
L_NEW = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
L_OLD = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
L_QUIET = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
L_NOCONSENT = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
L_BOOKED = "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"


def _lead(lead_id: str, **over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": lead_id,
        "name": "Ada Lovelace",
        "email": "ada@example.com",
        "company": "Analytical Ltd",
        "country": None,
        "interest": "demo",
        "industry": None,
        "message": None,
        "source": "landing",
        "createdAt": "2026-11-22T10:00:00.000Z",
        "status": "new",
        "consentAt": "2026-11-22T10:00:00.000Z",
        "lastDraftAt": None,
    }
    return {**base, **over}


def _bot(fake: FakeCoreSaas) -> LeadNurturing:
    executor, _ = build_stack(fake)
    runner = AgentRunner(InMemoryCheckpointStore(), ApprovalEngine(), tool_executor=executor)
    bot = LeadNurturing(runner, metrics=AgentMetrics())
    AgentCatalog(executor.agents, executor.registry).add(bot)
    return bot


async def _ask(
    fake: FakeCoreSaas,
    message: str = "Which leads need a follow-up?",
    token: str = PLATFORM_OPERATOR_TOKEN,
) -> AgentReply:
    return await _bot(fake).respond(caller(token, agent_id=None), AgentInput(message=message, today=TODAY))


async def test_no_leads_says_so() -> None:
    reply = await _ask(FakeCoreSaas())
    assert reply.status == "answered"
    assert "No lead needs a follow-up" in reply.text


async def test_new_lead_after_two_days_is_due() -> None:
    fake = FakeCoreSaas()
    fake.leads = [_lead(L_NEW, createdAt="2026-11-20T10:00:00.000Z")]
    reply = await _ask(fake)
    assert reply.data["findings"] == [{"lead_id": L_NEW, "level": "due", "days": 3}]
    assert "UNGROUNDED_REPLY" not in reply.guardrail_events
    assert "ada@example.com" not in reply.text  # Contact details never go into replies.


async def test_new_lead_after_a_week_is_urgent() -> None:
    fake = FakeCoreSaas()
    fake.leads = [
        _lead(L_NEW, createdAt="2026-11-20T10:00:00.000Z"),
        _lead(L_OLD, name="Grace Hopper", createdAt="2026-11-10T10:00:00.000Z"),
    ]
    reply = await _ask(fake)
    assert [f["level"] for f in reply.data["findings"]] == ["urgent", "due"]
    assert reply.data["findings"][0]["lead_id"] == L_OLD


async def test_lead_gone_quiet_is_flagged() -> None:
    fake = FakeCoreSaas()
    fake.leads = [_lead(L_QUIET, status="contacted", lastDraftAt="2026-11-10T09:00:00.000Z")]
    reply = await _ask(fake)
    assert reply.data["findings"][0]["level"] == "quiet"
    assert reply.data["findings"][0]["days"] == 13


async def test_lead_without_consent_is_never_flagged() -> None:
    fake = FakeCoreSaas()
    fake.leads = [_lead(L_NOCONSENT, consentAt=None, createdAt="2026-10-01T10:00:00.000Z")]
    reply = await _ask(fake)
    assert reply.data["findings"] == []
    assert "no recorded consent" in reply.text


async def test_booked_and_closed_leads_are_skipped() -> None:
    fake = FakeCoreSaas()
    fake.leads = [_lead(L_BOOKED, status="booked", createdAt="2026-10-01T10:00:00.000Z")]
    reply = await _ask(fake)
    assert reply.data["findings"] == []


async def test_draft_is_proposed_for_approval() -> None:
    fake = FakeCoreSaas()
    fake.leads = [_lead(L_NEW, createdAt="2026-11-20T10:00:00.000Z")]
    bot = _bot(fake)
    proposal = await bot.respond(
        caller(PLATFORM_OPERATOR_TOKEN, agent_id=None),
        AgentInput(message=f"Draft a follow-up for lead {L_NEW}", today=TODAY),
    )
    assert proposal.status == "awaiting_approval"
    assert fake.drafts == []  # Nothing is saved before approval.
    assert proposal.approval is not None and proposal.approval_token and proposal.run_id
    saved = await bot.approve(
        caller(PLATFORM_APPROVER_TOKEN, agent_id=None),
        run_id=proposal.run_id,
        approval_id=proposal.approval.approval_id,
        approval_token=proposal.approval_token,
        confirmed_payload=proposal.approval.proposed_payload,
    )
    assert saved.status == "answered", saved.text
    assert "Nothing was sent" in saved.text
    assert len(fake.drafts) == 1 and fake.drafts[0]["leadId"] == L_NEW
    assert "Hello Ada" in fake.drafts[0]["body"]


async def test_initiator_cannot_approve_own_draft() -> None:
    fake = FakeCoreSaas()
    fake.leads = [_lead(L_NEW)]
    bot = _bot(fake)
    me = caller(PLATFORM_OPERATOR_TOKEN, agent_id=None)
    proposal = await bot.respond(me, AgentInput(message=f"Draft a follow-up for lead {L_NEW}", today=TODAY))
    assert proposal.approval is not None and proposal.approval_token and proposal.run_id
    with pytest.raises(UaapError) as exc:
        await bot.approve(
            me,
            run_id=proposal.run_id,
            approval_id=proposal.approval.approval_id,
            approval_token=proposal.approval_token,
            confirmed_payload=proposal.approval.proposed_payload,
        )
    assert exc.value.code == "FORBIDDEN"
    assert fake.drafts == []


async def test_draft_refused_without_consent() -> None:
    fake = FakeCoreSaas()
    fake.leads = [_lead(L_NOCONSENT, consentAt=None)]
    reply = await _ask(fake, f"Draft a follow-up for lead {L_NOCONSENT}")
    assert reply.status == "refused"
    assert fake.drafts == []


async def test_draft_for_unknown_lead_is_refused() -> None:
    fake = FakeCoreSaas()
    fake.leads = [_lead(L_NEW)]
    reply = await _ask(fake, f"Draft a follow-up for lead {L_OLD}")
    assert reply.status == "needs_clarification"
    assert fake.drafts == []


async def test_other_tenant_is_refused() -> None:
    fake = FakeCoreSaas()
    fake.leads = [_lead(L_NEW)]
    reply = await _ask(fake, token=OTHER_PLATFORM_TOKEN)
    assert reply.status == "failed"
    assert "platform workspace" in reply.text


async def test_nothing_is_sent() -> None:
    fake = FakeCoreSaas()
    fake.leads = [_lead(L_NEW, createdAt="2026-11-10T10:00:00.000Z")]
    await _ask(fake)
    assert {r.method for r in fake.requests if r.url.path.startswith("/v1/platform")} == {"GET"}


def test_classify_and_draft_text_are_deterministic() -> None:
    leads = [_lead(L_NEW, createdAt="2026-11-20T10:00:00.000Z")]
    assert classify(leads, TODAY) == classify(leads, TODAY)
    assert draft_text(leads[0]) == draft_text(leads[0])
