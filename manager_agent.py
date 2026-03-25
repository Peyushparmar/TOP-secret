# ============================================================
# manager_agent.py — Orchestrator: routes leads and manages
#                    handoffs between Agent 1 and Agent 2
# ============================================================

from __future__ import annotations

import json
import threading
import logging
from datetime import datetime

import anthropic

from config import (
    ANTHROPIC_API_KEY, CLAUDE_MODEL, MAX_TOKENS,
    OPENER_DELAY_SECONDS, LO_NAME, LO_COMPANY, LO_PHONE,
    GHL_PIPELINE_ID, GHL_STAGE_NEW, GHL_STAGE_CONTACTED, GHL_STAGE_QUALIFIED,
)
from opener_agent import OpenerAgent
from nurturer_agent import NurturerAgent
import ghl_client as ghl
import database as db

logging.basicConfig(level=logging.INFO, format="%(asctime)s [MANAGER] %(message)s")
log = logging.getLogger(__name__)

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)


class ManagerAgent:
    """
    The brain of the system. When a new lead arrives from GHL:
      1. Classifies the lead (purchase vs refinance, urgency, quality)
      2. Fires OpenerAgent within OPENER_DELAY_SECONDS
      3. Schedules NurturerAgent for the 14-day follow-up sequence
      4. Monitors responses and routes replies back to the right agent
    """

    def __init__(self):
        self.opener   = OpenerAgent()
        self.nurturer = NurturerAgent()
        self.active_leads = {}   # lead_id → lead state dict

    # ── Entry point called by webhook_server.py ───────────
    def handle_new_lead(self, lead: dict):
        """
        Called the moment a new lead webhook arrives from GHL.
        lead = {
            "id": "...",
            "name": "John Smith",
            "email": "john@example.com",
            "phone": "+15550001234",
            "fb_psid": "...",          # Facebook Messenger Page-Scoped ID
            "lead_type": "purchase",   # or "refinance"
            "source": "Facebook Lead Ads",
            "created_at": "2024-01-15T10:30:00Z",
            "raw": { ... }             # full GHL payload
        }
        """
        lead_id = lead.get("id")
        log.info(f"New lead received → {lead.get('name')} ({lead_id})")

        # Store lead state
        self.active_leads[lead_id] = {
            "lead":          lead,
            "status":        "new",
            "opener_fired":  False,
            "nurturer_running": False,
            "conversation":  [],       # message history for Claude context
            "created_at":    datetime.utcnow().isoformat(),
        }

        # Step 1 — Save to database
        db.upsert_lead(lead, status="new")
        db.add_log("INFO", f"New lead received: {lead.get('name')} ({lead_id})", source="MANAGER", lead_id=lead_id)

        # Move to "New Lead" stage in GHL pipeline
        if GHL_PIPELINE_ID and GHL_STAGE_NEW:
            ghl.move_to_stage(lead_id, GHL_PIPELINE_ID, GHL_STAGE_NEW)
        ghl.tag_contact(lead_id, ["agent-system-active"])
        ghl.add_note(lead_id, "[AgentOS] New lead received. Classification in progress.")

        # Step 2 — Classify the lead with Claude
        classification = self._classify_lead(lead)
        self.active_leads[lead_id]["classification"] = classification
        log.info(f"Lead classified → {classification}")

        # Save classification to DB and GHL
        db.update_lead_status(lead_id, "classified",
            urgency=classification.get("urgency", "medium"),
            classification=json.dumps(classification)
        )
        db.add_log("INFO", f"Lead classified → type: {classification.get('lead_type')}, urgency: {classification.get('urgency')}", source="MANAGER", lead_id=lead_id)
        ghl.add_note(
            lead_id,
            f"[AgentOS] Lead classified:\n"
            f"  Type: {classification.get('lead_type', 'unknown')}\n"
            f"  Urgency: {classification.get('urgency', 'unknown')}\n"
            f"  Strategy: {classification.get('strategy_notes', '')}"
        )

        # Step 3 — Fire Opener in background thread (speed is critical)
        threading.Timer(
            OPENER_DELAY_SECONDS,
            self._fire_opener,
            args=[lead_id]
        ).start()
        log.info(f"Opener scheduled in {OPENER_DELAY_SECONDS}s for lead {lead_id}")

    # ── Called by webhook when lead replies to any channel ─
    def handle_lead_reply(self, lead_id: str, channel: str, message: str):
        """
        Routes an inbound reply from the lead back through Claude
        to generate a smart response, then sends it via the right channel.
        """
        state = self.active_leads.get(lead_id)
        if not state:
            log.warning(f"Reply from unknown lead {lead_id}, ignoring.")
            return

        log.info(f"Reply from {lead_id} via {channel}: {message[:60]}...")

        # Save inbound message to DB
        db.save_message(lead_id, "user", message, channel)

        # Build conversation history for Claude (from DB for persistence)
        history = db.get_conversation(lead_id)
        state["conversation"] = [{"role": m["role"], "content": m["content"]} for m in history]

        # Generate response with Claude
        response_text = self._generate_reply(state, channel)

        # Save outbound response to DB
        db.save_message(lead_id, "assistant", response_text, channel)
        state["conversation"].append({"role": "assistant", "content": response_text})

        # Send reply back via same channel
        lead       = state["lead"]
        contact_id = lead["id"]
        if channel == "sms":
            self.opener.send_sms(contact_id, response_text)
        elif channel == "email":
            self.opener.send_email(contact_id, lead["name"], "Following up on your mortgage enquiry", response_text)
        elif channel == "messenger":
            self.opener.send_messenger(lead.get("fb_psid"), response_text)

        # If lead seems qualified, flag for LO handoff
        if self._is_qualified(state):
            self._flag_for_handoff(lead_id)

    # ── Private: classify lead with Claude ────────────────
    def _classify_lead(self, lead: dict) -> dict:
        """
        Asks Claude to classify the lead based on available data.
        Returns a dict with lead_type, urgency, and strategy notes.
        """
        prompt = f"""
You are a mortgage loan officer assistant for {LO_NAME} at {LO_COMPANY}.

A new enquiry just came in. Classify it and suggest an outreach strategy.

Lead info:
- Name: {lead.get('name')}
- Source: {lead.get('source')}
- Declared type: {lead.get('lead_type', 'unknown')}
- Any notes: {lead.get('notes', 'none')}

Respond in this exact JSON format:
{{
  "lead_type": "purchase" or "refinance" or "unknown",
  "urgency": "high" or "medium" or "low",
  "tone": "excited" or "professional" or "empathetic",
  "opening_hook": "One sentence opening tailored to this lead",
  "strategy_notes": "Brief notes for the loan officer"
}}
"""
        try:
            resp = client.messages.create(
                model=CLAUDE_MODEL,
                max_tokens=512,
                messages=[{"role": "user", "content": prompt}]
            )
            return json.loads(resp.content[0].text)
        except Exception as e:
            log.error(f"Classification error: {e}")
            return {"lead_type": "unknown", "urgency": "medium", "tone": "professional",
                    "opening_hook": f"Hi, I'm {LO_NAME} — I'd love to help you with your mortgage!",
                    "strategy_notes": "Could not classify — proceed with standard script."}

    # ── Private: fire Opener Agent ─────────────────────────
    def _fire_opener(self, lead_id: str):
        state = self.active_leads.get(lead_id)
        if not state:
            return

        # Check DND before firing — lead may have opted out in the delay window
        if ghl.is_dnd(lead_id):
            log.info(f"Skipping opener for {lead_id} — DND is set")
            return

        log.info(f"Firing OpenerAgent for lead {lead_id}")
        state["opener_fired"] = True
        state["status"] = "opener_running"

        results = self.opener.run(state["lead"], state["classification"])

        # Move to "Contacted" stage in GHL
        if GHL_PIPELINE_ID and GHL_STAGE_CONTACTED:
            ghl.move_to_stage(lead_id, GHL_PIPELINE_ID, GHL_STAGE_CONTACTED)

        channels_fired = [ch for ch, r in results.items() if r.get("success")]
        ghl.add_note(
            lead_id,
            f"[AgentOS] Opener fired. Channels: {', '.join(channels_fired) or 'none'}."
        )

        # After opener fires, schedule 14-day nurture sequence
        state["status"] = "nurturer_scheduled"
        state["nurturer_running"] = True
        self.nurturer.schedule(state["lead"], state["classification"])
        log.info(f"NurturerAgent scheduled for lead {lead_id}")

    # ── Private: generate reply with Claude ───────────────
    def _generate_reply(self, state: dict, channel: str) -> str:
        lead = state["lead"]
        classification = state.get("classification", {})

        system_prompt = f"""
You are {LO_NAME}, a licensed mortgage loan officer at {LO_COMPANY}.
You are responding to a {classification.get('lead_type', 'mortgage')} enquiry via {channel}.

Your goal: build rapport, answer questions, and gently move the lead
toward a 15-minute pre-qualification call.

Keep replies short and conversational — max 3 sentences for SMS,
max 5 sentences for email. Never be pushy. Be warm and helpful.
"""
        messages = state["conversation"][-10:]  # last 10 messages for context

        try:
            resp = client.messages.create(
                model=CLAUDE_MODEL,
                max_tokens=MAX_TOKENS,
                system=system_prompt,
                messages=messages
            )
            return resp.content[0].text
        except Exception as e:
            log.error(f"Reply generation error: {e}")
            return f"Thanks for your message! I'll get back to you shortly. — {LO_NAME}"

    # ── Private: check if lead is pre-qualified ───────────
    def _is_qualified(self, state: dict) -> bool:
        """
        Asks Claude to evaluate the conversation and decide whether
        the lead has shown enough qualification signals to hand off
        to the loan officer.

        Qualification signals Claude watches for:
          - Mentioned credit score range
          - Shared income / employment status
          - Disclosed down payment amount or savings
          - Asked about rates / monthly payment estimates
          - Expressed readiness to apply or meet
        """
        if len(state["conversation"]) < 2:
            return False   # Not enough conversation yet

        convo_text = "\n".join(
            f"{m['role'].upper()}: {m['content']}"
            for m in state["conversation"]
        )
        lead = state["lead"]
        lead_type = state.get("classification", {}).get("lead_type", "mortgage")

        prompt = f"""
You are evaluating a mortgage enquiry conversation to decide if the lead is
ready to be connected with a loan officer for a pre-qualification call.

Lead type: {lead_type}

Conversation so far:
{convo_text}

A lead is considered QUALIFIED if they have shown at least 2 of these signals:
1. Mentioned their credit score or credit situation
2. Shared income, employment, or financial situation
3. Discussed down payment or savings available
4. Asked specific questions about rates, payments, or loan amounts
5. Expressed readiness to apply, meet, or take next steps soon

Respond with only a JSON object:
{{"qualified": true or false, "reason": "one sentence explaining your decision"}}
"""
        try:
            resp = client.messages.create(
                model=CLAUDE_MODEL,
                max_tokens=128,
                messages=[{"role": "user", "content": prompt}]
            )
            result = json.loads(resp.content[0].text)
            if result.get("qualified"):
                log.info(f"Qualification check → QUALIFIED. Reason: {result.get('reason')}")
            return bool(result.get("qualified", False))
        except Exception as e:
            log.error(f"Qualification check error: {e}")
            # Fallback to keyword heuristic if Claude call fails
            conversation_text = " ".join(
                m["content"] for m in state["conversation"]
            ).lower()
            signals = ["credit score", "income", "down payment", "pre-qual",
                       "how much", "qualify", "afford", "ready to apply"]
            return sum(1 for s in signals if s in conversation_text) >= 2

    # ── Private: flag lead for LO handoff ─────────────────
    def _flag_for_handoff(self, lead_id: str):
        """
        Executes the handoff sequence when a lead qualifies:
          1. Marks status as handed_off (prevents duplicate handoffs)
          2. Stops the nurture sequence
          3. Tags the contact in GHL as 'qualified'
          4. Moves GHL pipeline opportunity to the qualified stage
          5. Adds a note to GHL with conversation summary
          6. Sends the LO an SMS alert with the lead's details
        """
        state = self.active_leads.get(lead_id)
        if not state or state["status"] == "handed_off":
            return

        state["status"] = "handed_off"
        lead = state["lead"]
        log.info(f"LEAD QUALIFIED — handing off {lead.get('name')} ({lead_id}) to {LO_NAME}")

        # Stop the nurture sequence
        self.nurturer.stop(lead_id, reason="qualified_for_handoff")

        # Tag in GHL
        ghl.tag_contact(lead_id, ["qualified", "agent-system-handoff"])

        # Move pipeline stage
        if GHL_PIPELINE_ID and GHL_STAGE_QUALIFIED:
            ghl.move_to_stage(lead_id, GHL_PIPELINE_ID, GHL_STAGE_QUALIFIED)

        # Add a summary note to GHL
        convo_summary = _summarise_conversation(state["conversation"])
        ghl.add_note(
            lead_id,
            f"[AgentOS Handoff] Lead qualified after {len(state['conversation'])} exchanges.\n"
            f"Conversation summary:\n{convo_summary}"
        )

        # Alert the LO — log prominently so it shows up in dashboard/logs
        # (For a real SMS to your personal phone, add Twilio/Bland keys later)
        log.info(
            f"\n{'='*50}\n"
            f"  HOT LEAD READY FOR CALL\n"
            f"  Name:  {lead.get('name')}\n"
            f"  Phone: {lead.get('phone')}\n"
            f"  Email: {lead.get('email')}\n"
            f"  Type:  {state.get('classification', {}).get('lead_type', 'unknown').title()}\n"
            f"{'='*50}"
        )
        # Also add a high-priority note in GHL so it shows in the contact
        ghl.add_note(
            lead_id,
            "🔥 READY FOR LO CALL — Lead has been pre-qualified by AgentOS. "
            f"Please call {lead.get('name')} at {lead.get('phone')} ASAP."
        )
        ghl.tag_contact(lead_id, ["hot-lead", "call-now"])


# ── Module-level helpers ──────────────────────────────────────

def _summarise_conversation(conversation: list[dict]) -> str:
    """
    Returns a concise plain-text summary of the conversation
    to attach as a GHL note. Falls back to a raw transcript
    excerpt if Claude is unreachable.
    """
    if not conversation:
        return "(no conversation recorded)"

    convo_text = "\n".join(
        f"{m['role'].upper()}: {m['content']}"
        for m in conversation
    )

    try:
        resp = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=256,
            messages=[{
                "role": "user",
                "content": (
                    "Summarise the following mortgage enquiry conversation in 3 bullet points. "
                    "Focus on: what the lead wants, key details they shared (credit, income, "
                    "timeline), and their apparent readiness level.\n\n"
                    f"{convo_text}"
                )
            }]
        )
        return resp.content[0].text
    except Exception:
        # Plain excerpt fallback — last 4 exchanges
        excerpt = conversation[-4:]
        return "\n".join(
            f"{m['role'].upper()}: {m['content'][:120]}"
            for m in excerpt
        )
