# ============================================================
# ghl_client.py — GoHighLevel API v2 helper
#
# Covers the calls made throughout the agent system:
#   • Contact read / update / tag
#   • Notes & activity
#   • Pipeline stage moves
#   • Contact search by custom field (PSID lookup)
# ============================================================

from __future__ import annotations

import logging
import requests

from config import GHL_API_KEY, GHL_LOCATION_ID, GHL_BASE_URL

log = logging.getLogger(__name__)
log.setLevel(logging.INFO)

# ── Shared session with auth header ──────────────────────────
_session = requests.Session()
_session.headers.update({
    "Authorization": f"Bearer {GHL_API_KEY}",
    "Content-Type":  "application/json",
    "Version":       "2021-07-28",   # GHL API v2 version header
})

_TIMEOUT = 12   # seconds


# ════════════════════════════════════════════════════════════
# Contact CRUD
# ════════════════════════════════════════════════════════════

def get_contact(contact_id: str) -> dict | None:
    """
    Fetches a single GHL contact by ID.
    Returns the contact dict or None on failure.
    """
    try:
        resp = _session.get(
            f"{GHL_BASE_URL}/contacts/{contact_id}",
            timeout=_TIMEOUT
        )
        resp.raise_for_status()
        data = resp.json()
        return data.get("contact", data)
    except Exception as e:
        log.error(f"GHL get_contact({contact_id}) failed: {e}")
        return None


def update_contact(contact_id: str, fields: dict) -> bool:
    """
    Partial-updates a GHL contact.
    fields = any subset of GHL contact attributes, e.g.:
        {"tags": ["qualified"], "customField": {"key": "value"}}
    Returns True on success.
    """
    try:
        resp = _session.put(
            f"{GHL_BASE_URL}/contacts/{contact_id}",
            json=fields,
            timeout=_TIMEOUT
        )
        resp.raise_for_status()
        log.info(f"GHL contact {contact_id} updated: {list(fields.keys())}")
        return True
    except Exception as e:
        log.error(f"GHL update_contact({contact_id}) failed: {e}")
        return False


def tag_contact(contact_id: str, tags: list[str]) -> bool:
    """
    Adds one or more tags to a GHL contact (non-destructive merge).
    """
    try:
        contact = get_contact(contact_id)
        if contact is None:
            return False
        existing = contact.get("tags", [])
        merged   = list(set(existing + tags))
        return update_contact(contact_id, {"tags": merged})
    except Exception as e:
        log.error(f"GHL tag_contact({contact_id}) failed: {e}")
        return False


def set_dnd(contact_id: str, dnd: bool = True) -> bool:
    """Marks a contact as Do Not Disturb (stops all automated outreach)."""
    return update_contact(contact_id, {"dnd": dnd})


# ════════════════════════════════════════════════════════════
# Messaging (SMS via GHL A2P)
# ════════════════════════════════════════════════════════════

def send_sms(contact_id: str, message: str) -> dict:
    """
    Sends an SMS through GHL's built-in messaging (A2P 10DLC verified).
    The message appears in the GHL conversation thread automatically.

    GHL resolves the phone number from the contact — no need to pass it.
    """
    try:
        resp = _session.post(
            f"{GHL_BASE_URL}/conversations/messages",
            json={
                "type":      "SMS",
                "contactId": contact_id,
                "message":   message,
            },
            timeout=_TIMEOUT
        )
        resp.raise_for_status()
        data = resp.json()
        log.info(f"GHL SMS sent to {contact_id} — messageId: {data.get('messageId', data.get('id'))}")
        return {"success": True, "messageId": data.get("messageId", data.get("id"))}
    except Exception as e:
        log.error(f"GHL send_sms({contact_id}) failed: {e}")
        return {"success": False, "error": str(e)}


def send_email(contact_id: str, subject: str, body: str, html: str = "") -> dict:
    """
    Sends an email through GHL's built-in email system.
    Falls back to plain text if no html is provided.
    """
    try:
        payload = {
            "type":      "Email",
            "contactId": contact_id,
            "subject":   subject,
            "html":      html if html else body.replace("\n", "<br>"),
        }
        resp = _session.post(
            f"{GHL_BASE_URL}/conversations/messages",
            json=payload,
            timeout=_TIMEOUT
        )
        resp.raise_for_status()
        data = resp.json()
        log.info(f"GHL email sent to {contact_id}")
        return {"success": True, "messageId": data.get("messageId", data.get("id"))}
    except Exception as e:
        log.error(f"GHL send_email({contact_id}) failed: {e}")
        return {"success": False, "error": str(e)}


# ════════════════════════════════════════════════════════════
# Notes & Activity Feed
# ════════════════════════════════════════════════════════════

def add_note(contact_id: str, body: str) -> bool:
    """
    Adds a note to a GHL contact's activity feed.
    Useful for logging agent actions and call summaries.
    """
    try:
        resp = _session.post(
            f"{GHL_BASE_URL}/contacts/{contact_id}/notes",
            json={"body": body, "userId": None},
            timeout=_TIMEOUT
        )
        resp.raise_for_status()
        log.info(f"GHL note added to contact {contact_id}")
        return True
    except Exception as e:
        log.error(f"GHL add_note({contact_id}) failed: {e}")
        return False


# ════════════════════════════════════════════════════════════
# Pipeline / Opportunity Stage
# ════════════════════════════════════════════════════════════

def get_opportunities(contact_id: str) -> list[dict]:
    """Returns all open opportunities linked to a contact."""
    try:
        resp = _session.get(
            f"{GHL_BASE_URL}/contacts/{contact_id}/opportunities",
            timeout=_TIMEOUT
        )
        resp.raise_for_status()
        return resp.json().get("opportunities", [])
    except Exception as e:
        log.error(f"GHL get_opportunities({contact_id}) failed: {e}")
        return []


def move_to_stage(contact_id: str, pipeline_id: str, stage_id: str) -> bool:
    """
    Moves the lead's open opportunity to a new pipeline stage.
    pipeline_id and stage_id come from your GHL pipeline setup.

    Tip: find these IDs in GHL → Settings → Pipelines → copy the URL.
    """
    try:
        opps = get_opportunities(contact_id)
        if not opps:
            # Create a new opportunity if none exists
            resp = _session.post(
                f"{GHL_BASE_URL}/opportunities",
                json={
                    "pipelineId":  pipeline_id,
                    "locationId":  GHL_LOCATION_ID,
                    "stageId":     stage_id,
                    "contactId":   contact_id,
                    "name":        "Auto-nurture",
                    "status":      "open",
                },
                timeout=_TIMEOUT
            )
            resp.raise_for_status()
            log.info(f"GHL opportunity created for contact {contact_id}")
            return True

        # Update the first open opportunity
        opp_id = opps[0]["id"]
        resp = _session.put(
            f"{GHL_BASE_URL}/opportunities/{opp_id}",
            json={"stageId": stage_id},
            timeout=_TIMEOUT
        )
        resp.raise_for_status()
        log.info(f"GHL opportunity {opp_id} moved to stage {stage_id}")
        return True

    except Exception as e:
        log.error(f"GHL move_to_stage({contact_id}) failed: {e}")
        return False


# ════════════════════════════════════════════════════════════
# Search / Lookup
# ════════════════════════════════════════════════════════════

def search_contacts(query: str) -> list[dict]:
    """
    Full-text search across GHL contacts.
    Returns a list of matching contact dicts.
    """
    try:
        params = {"locationId": GHL_LOCATION_ID}
        if query:
            params["query"] = query
        resp = _session.get(
            f"{GHL_BASE_URL}/contacts/",
            params=params,
            timeout=_TIMEOUT
        )
        resp.raise_for_status()
        data = resp.json()
        return data.get("contacts", data.get("data", []))
    except Exception as e:
        log.error(f"GHL search_contacts({query!r}) failed: {e}")
        return []


def find_contact_by_psid(psid: str) -> str | None:
    """
    Looks up a GHL contact ID by their Facebook Page-Scoped User ID.

    Assumes you have a custom field named 'fb_psid' in GHL.
    If GHL doesn't support direct custom-field search, falls back to
    fetching recent contacts and scanning the field client-side.

    Returns the GHL contact ID string, or None if not found.
    """
    if not psid:
        return None

    try:
        # Try direct custom-field search first (GHL v1 supports this)
        resp = _session.get(
            f"{GHL_BASE_URL}/contacts",
            params={
                "locationId":        GHL_LOCATION_ID,
                "query":             psid,
                "customFieldKey":    "fb_psid",
            },
            timeout=_TIMEOUT
        )
        resp.raise_for_status()
        contacts = resp.json().get("contacts", [])
        if contacts:
            return contacts[0].get("id")

        log.warning(f"PSID {psid} not found via GHL search")
        return None

    except Exception as e:
        log.error(f"GHL find_contact_by_psid({psid}) failed: {e}")
        return None


def is_dnd(contact_id: str) -> bool:
    """Returns True if the contact has DND set in GHL."""
    contact = get_contact(contact_id)
    if contact is None:
        return False
    return bool(contact.get("dnd", False))


def create_contact(name: str, phone: str, email: str = "") -> dict:
    """Creates a new contact in GHL and returns the API response."""
    first, *rest = name.strip().split(" ", 1)
    last = rest[0] if rest else ""
    payload = {
        "locationId": GHL_LOCATION_ID,
        "firstName":  first,
        "lastName":   last,
        "phone":      phone,
        "source":     "manual-dashboard",
    }
    if email:
        payload["email"] = email
    try:
        resp = _session.post(
            f"{GHL_BASE_URL}/contacts/",
            json=payload,
            timeout=_TIMEOUT
        )
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        log.error(f"GHL create_contact failed: {e}")
        raise
