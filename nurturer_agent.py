# ============================================================
# nurturer_agent.py — Agent 2: 14-Day Follow-Up Sequence
#                     Runs after Agent 1 if lead hasn't
#                     responded or booked a call yet
# ============================================================

import json
import logging
import threading
from datetime import datetime

import anthropic

from config import (
    ANTHROPIC_API_KEY, CLAUDE_MODEL, MAX_TOKENS,
    NURTURER_SCHEDULE_DAYS,
    LO_NAME, LO_COMPANY, LO_PHONE, LO_NMLS,
    CHANNELS,
    GHL_PIPELINE_ID, GHL_STAGE_NURTURING,
)
import ghl_client as ghl
import database as db

log = logging.getLogger(__name__)
log.setLevel(logging.INFO)

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)


# ── Sequence blueprint ────────────────────────────────────
# Each entry defines the message theme for that day.
# Claude generates the actual copy fresh each time.
SEQUENCE = [
    {"day": 1,  "channel": "sms",     "theme": "Warm check-in — did they get our message?"},
    {"day": 2,  "channel": "email",   "theme": "Educational: current mortgage rates overview"},
    {"day": 3,  "channel": "sms",     "theme": "Quick value add — one tip to improve their rate"},
    {"day": 5,  "channel": "email",   "theme": "Case study: client who saved $400/month refinancing"},
    {"day": 7,  "channel": "sms",     "theme": "Soft CTA — offer a free 10-min rate check call"},
    {"day": 9,  "channel": "email",   "theme": "FAQ: 'How long does pre-qualification take?'"},
    {"day": 11, "channel": "sms",     "theme": "Social proof — mention # of families helped this month"},
    {"day": 14, "channel": "email",   "theme": "Final breakup email — closing the loop, door still open"},
]


class NurturerAgent:
    """
    Agent 2 — The Relationship Builder.

    Runs a structured 14-day follow-up sequence across SMS and Email.
    Each message is generated fresh by Claude, personalised to
    the lead's type (purchase vs refinance) and conversation history.

    Stops automatically if the lead:
      - Replies and books a call (handed off to LO)
      - Asks to unsubscribe / stop
      - Is marked as disqualified in GHL
    """

    def __init__(self):
        self.active_sequences = {}   # lead_id → sequence state

    def schedule(self, lead: dict, classification: dict):
        """
        Starts the 14-day sequence for a lead.
        Uses threading.Timer to schedule each message at the right day.

        NOTE: threading.Timer does NOT survive a server restart.
        For production, swap for Celery tasks (see tasks.py).
        The _send_step method always checks the 'status' flag and GHL DND
        before sending, so it is safe to call even if the sequence was
        stopped externally while the timer was running.
        """
        lead_id = lead["id"]
        log.info(f"Scheduling 14-day nurture sequence for {lead.get('name')}")

        self.active_sequences[lead_id] = {
            "lead":           lead,
            "classification": classification,
            "status":         "running",
            "step":           0,
            "started_at":     datetime.utcnow().isoformat(),
            "messages_sent":  [],
        }

        # Move GHL pipeline stage to "Nurturing"
        if GHL_PIPELINE_ID and GHL_STAGE_NURTURING:
            ghl.move_to_stage(lead_id, GHL_PIPELINE_ID, GHL_STAGE_NURTURING)

        for step in SEQUENCE:
            delay_seconds = step["day"] * 86400  # convert days to seconds
            threading.Timer(
                delay_seconds,
                self._send_step,
                args=[lead_id, step]
            ).start()
            log.info(f"Step {step['day']} scheduled in {step['day']} day(s) via {step['channel']}")

    def stop(self, lead_id: str, reason: str = "unspecified"):
        """
        Stops the sequence for a lead (e.g. they booked, unsubscribed).
        Note: threading.Timer can't be cancelled mid-flight — in production
        use Celery task revocation or a DB flag check before each send.
        """
        if lead_id in self.active_sequences:
            self.active_sequences[lead_id]["status"] = f"stopped:{reason}"
            log.info(f"Nurture sequence stopped for {lead_id} — reason: {reason}")

    # ── Private: execute a single sequence step ───────────
    def _send_step(self, lead_id: str, step: dict):
        """Called by the timer when a scheduled step is due."""
        state = self.active_sequences.get(lead_id)
        if not state:
            return

        # Local stop flag (set by manager when lead qualifies or unsubscribes)
        if state["status"] != "running":
            log.info(f"Skipping step {step['day']} for {lead_id} — sequence stopped.")
            return

        # Always re-check GHL DND before sending — contact may have opted out
        if ghl.is_dnd(lead_id):
            log.info(f"Skipping step {step['day']} for {lead_id} — GHL DND is set.")
            self.stop(lead_id, reason="dnd_set_in_ghl")
            return

        lead           = state["lead"]
        classification = state["classification"]
        channel        = step["channel"]
        theme          = step["theme"]
        day            = step["day"]

        log.info(f"Sending nurture step (day {day}) to {lead.get('name')} via {channel}")

        # Generate message with Claude
        message = self._generate_message(lead, classification, step, state)

        # Send via appropriate channel
        from opener_agent import OpenerAgent
        sender = OpenerAgent()

        if channel == "sms" and CHANNELS.get("sms"):
            result = sender.send_sms(lead["phone"], message["body"])
        elif channel == "email" and CHANNELS.get("email"):
            result = sender.send_email(
                lead["email"], lead["name"],
                message["subject"], message["body"]
            )
        else:
            result = {"success": False, "error": "Channel disabled"}

        # Log the step to DB
        db.log_sequence_step(
            lead_id=lead_id, day=day, channel=channel,
            theme=theme, status="sent" if result.get("success") else "failed",
            message=message.get("body", ""), result=result
        )
        db.update_lead_status(lead_id, "nurturing", nurturer_step=day)
        db.add_log(
            "SUCCESS" if result.get("success") else "ERROR",
            f"Nurture step day {day} via {channel} — {'sent' if result.get('success') else 'failed'}",
            source="NURTURER", lead_id=lead_id
        )

        state["messages_sent"].append({
            "day":     day,
            "channel": channel,
            "sent_at": datetime.utcnow().isoformat(),
            "result":  result,
        })
        state["step"] = day

        log.info(f"Nurture step {day} result: {result}")

        # Final step — mark sequence complete
        if day == 14:
            state["status"] = "completed"
            db.update_lead_status(lead_id, "completed", nurturer_status="completed")
            db.add_log("SUCCESS", "14-day nurture sequence completed", source="NURTURER", lead_id=lead_id)
            log.info(f"14-day sequence completed for {lead.get('name')}")

    # ── Private: generate message with Claude ─────────────
    def _generate_message(self, lead: dict, classification: dict,
                          step: dict, state: dict) -> dict:
        """
        Asks Claude to write a follow-up message for this specific
        step, personalised to the lead and their loan type.
        """
        lead_type  = classification.get("lead_type", "mortgage")
        first_name = lead.get("name", "there").split()[0]
        day        = step["day"]
        channel    = step["channel"]
        theme      = step["theme"]
        is_final   = day == 14

        messages_sent_count = len(state["messages_sent"])

        prompt = f"""
You are writing a follow-up message for {LO_NAME} at {LO_COMPANY}.

Lead: {first_name} (interested in a {lead_type} loan)
Day: {day} of a 14-day nurture sequence
Channel: {channel}
Message #{messages_sent_count + 1} in sequence
Theme for this message: {theme}
Is this the final message in the sequence: {is_final}

Guidelines:
- SMS: max 160 characters, one clear thought, end with a question or CTA
- Email: subject max 8 words, body 3 short paragraphs, warm closing
- Sound like a real human — not a bot or salesperson
- Provide value first, ask second
- If final email: acknowledge you won't reach out again, leave the door open warmly
- Include NMLS #{LO_NMLS} in emails only (not SMS)
- LO direct line: {LO_PHONE}

Respond in JSON:
{{
  "subject": "email subject or empty string for SMS",
  "body": "the full message text"
}}
"""
        try:
            resp = client.messages.create(
                model=CLAUDE_MODEL,
                max_tokens=MAX_TOKENS,
                messages=[{"role": "user", "content": prompt}]
            )
            return json.loads(resp.content[0].text)
        except Exception as e:
            log.error(f"Nurturer message generation failed: {e}")
            # Fallback
            if channel == "email":
                return {
                    "subject": f"Still here to help, {first_name}",
                    "body": (
                        f"Hi {first_name},\n\n"
                        f"Just checking in — I'm still here whenever you're ready to "
                        f"explore your {lead_type} options.\n\n"
                        f"No pressure at all. Feel free to reply or call me directly "
                        f"at {LO_PHONE}.\n\n"
                        f"Best,\n{LO_NAME}\nNMLS #{LO_NMLS}"
                    )
                }
            else:
                return {
                    "subject": "",
                    "body": f"Hi {first_name}, {LO_NAME} here — still happy to help with your {lead_type} loan. Any questions? 😊"
                }

    def get_status(self, lead_id: str) -> dict:
        """Returns the current nurture sequence status for a lead."""
        return self.active_sequences.get(lead_id, {"status": "not_found"})
