# ============================================================
# tasks.py — Celery task definitions
#
# Production replacement for threading.Timer used in
# nurturer_agent.py. Celery + Redis survives server restarts
# and allows task cancellation (revocation).
#
# Setup:
#   1. Install Redis:  brew install redis && redis-server
#   2. pip install celery redis
#   3. Start worker:   celery -A tasks worker --loglevel=info
#   4. Optional beat:  celery -A tasks beat --loglevel=info
#
# Usage:
#   from tasks import schedule_nurture_step
#   schedule_nurture_step.apply_async(
#       args=[lead_id, step],
#       countdown=step["day"] * 86400   # delay in seconds
#   )
#
#   # Cancel a step before it fires:
#   task_result = schedule_nurture_step.apply_async(...)
#   task_result.revoke()   # prevents execution if not yet run
#
# To migrate from threading.Timer → Celery in nurturer_agent.py,
# replace each threading.Timer(...).start() call with:
#   result = schedule_nurture_step.apply_async(
#       args=[lead_id, step],
#       countdown=delay_seconds,
#   )
# and store result.id in state["timer_ids"][step["day"]]
# so you can revoke them in stop().
# ============================================================

import logging
from celery import Celery

log = logging.getLogger(__name__)

# ── Celery app ────────────────────────────────────────────────
# Redis is both broker (receives tasks) and backend (stores results)
app = Celery(
    "loan_agent_tasks",
    broker="redis://localhost:6379/0",
    backend="redis://localhost:6379/0",
)

app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
    task_track_started=True,
    task_acks_late=True,          # Re-queue if worker crashes mid-task
    worker_prefetch_multiplier=1, # One task at a time per worker slot
)


# ════════════════════════════════════════════════════════════
# Nurture Sequence Tasks
# ════════════════════════════════════════════════════════════

@app.task(bind=True, max_retries=3, default_retry_delay=300)
def schedule_nurture_step(self, lead_id: str, step: dict):
    """
    Celery task: sends a single nurture sequence step.
    Called with a delay (countdown) equal to the step's day × 86400s.

    Args:
        lead_id:  GHL contact ID
        step:     dict from SEQUENCE blueprint
                  {"day": 1, "channel": "sms", "theme": "..."}
    """
    try:
        # Import here to avoid circular imports at module load time
        from nurturer_agent import NurturerAgent
        import ghl_client as ghl

        # Check DND before doing any work
        if ghl.is_dnd(lead_id):
            log.info(f"Celery: skipping step {step['day']} for {lead_id} — DND set")
            return {"skipped": True, "reason": "dnd"}

        agent = NurturerAgent()
        # Re-hydrate the sequence state from GHL contact data
        contact = ghl.get_contact(lead_id)
        if not contact:
            log.warning(f"Celery: contact {lead_id} not found in GHL, skipping step {step['day']}")
            return {"skipped": True, "reason": "contact_not_found"}

        lead = {
            "id":        contact.get("id"),
            "name":      f"{contact.get('firstName','')} {contact.get('lastName','')}".strip(),
            "email":     contact.get("email"),
            "phone":     contact.get("phone"),
            "fb_psid":   contact.get("customField", {}).get("fb_psid"),
            "lead_type": _detect_lead_type_from_tags(contact.get("tags", [])),
            "source":    contact.get("source", "unknown"),
        }

        # Classify is lightweight — reads from GHL tags
        classification = {
            "lead_type": lead["lead_type"],
            "urgency":   "medium",
            "tone":      "professional",
        }

        # Build minimal state for _send_step
        state = {
            "lead":           lead,
            "classification": classification,
            "status":         "running",
            "step":           step["day"] - 1,
            "started_at":     "",
            "messages_sent":  [],
        }

        agent._send_step_with_state(lead_id, step, state)
        return {"success": True, "day": step["day"]}

    except Exception as exc:
        log.error(f"Celery task failed for lead {lead_id} step {step}: {exc}")
        raise self.retry(exc=exc)


@app.task
def send_lo_handoff_alert(lead_id: str):
    """
    Async task: notifies the loan officer when a lead qualifies.
    Separate task so it can be retried independently.
    """
    try:
        import ghl_client as ghl
        from opener_agent import OpenerAgent
        from config import LO_PHONE, LO_NAME

        contact = ghl.get_contact(lead_id)
        if not contact:
            return

        name  = f"{contact.get('firstName','')} {contact.get('lastName','')}".strip()
        phone = contact.get("phone", "unknown")
        tags  = contact.get("tags", [])
        ltype = _detect_lead_type_from_tags(tags).title()

        alert = (
            f"Hot lead ready for call!\n"
            f"Name:  {name}\n"
            f"Phone: {phone}\n"
            f"Type:  {ltype}\n"
            f"— AgentOS"
        )

        opener = OpenerAgent()
        result = opener.send_sms(LO_PHONE, alert)
        log.info(f"LO handoff alert sent: {result}")
        return result
    except Exception as e:
        log.error(f"LO handoff alert failed for {lead_id}: {e}")


# ── Helper ────────────────────────────────────────────────────

def _detect_lead_type_from_tags(tags: list) -> str:
    for t in tags:
        t = t.lower()
        if "refi" in t or "refinance" in t:
            return "refinance"
        if "purchase" in t or "buy" in t:
            return "purchase"
    return "unknown"
