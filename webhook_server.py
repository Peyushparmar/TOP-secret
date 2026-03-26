# ============================================================
# webhook_server.py — Flask server that receives events from
#                     GoHighLevel and Facebook Messenger
# ============================================================
# Expose this server to the internet with:
#   ngrok http 5000
# Then paste the public URL into GHL → Settings → Webhooks

from __future__ import annotations

import os
import hmac
import hashlib
import logging
import json

from flask import Flask, request, jsonify, abort, session, redirect, url_for, render_template_string, send_file

from config import (
    FLASK_PORT, FLASK_DEBUG,
    GHL_WEBHOOK_SECRET,
    FB_VERIFY_TOKEN, FB_PAGE_ACCESS_TOKEN,
)
from manager_agent import ManagerAgent
import ghl_client as ghl
from auth import check_password, login_required, is_logged_in, DASHBOARD_USERNAME
import database as db

logging.basicConfig(level=logging.INFO, format="%(asctime)s [SERVER] %(message)s")
log = logging.getLogger(__name__)

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "dev-secret-change-in-production")
manager = ManagerAgent()

# Initialise database on startup
db.init_db()


# ════════════════════════════════════════════════════════════
# Auth Routes
# ════════════════════════════════════════════════════════════

LOGIN_PAGE = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>AgentOS — Login</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet"/>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:'Inter',sans-serif;background:#0c0d14;color:#e2e8f0;min-height:100vh;display:flex;align-items:center;justify-content:center;}
.card{background:#161929;border:1px solid rgba(255,255,255,0.06);border-radius:16px;padding:40px;width:100%;max-width:380px;}
.logo{display:flex;align-items:center;gap:10px;margin-bottom:32px;}
.mark{width:36px;height:36px;border-radius:10px;background:linear-gradient(135deg,#7c6ce0,#4f46e5);display:flex;align-items:center;justify-content:center;font-size:16px;}
.brand{font-size:1rem;font-weight:700;}
.brand span{display:block;font-size:0.65rem;color:#64748b;font-weight:400;margin-top:1px;}
h2{font-size:1.2rem;font-weight:700;margin-bottom:6px;}
p{font-size:0.78rem;color:#64748b;margin-bottom:28px;}
label{display:block;font-size:0.72rem;font-weight:600;color:#94a3b8;margin-bottom:6px;}
input{width:100%;background:#1c2035;border:1px solid rgba(255,255,255,0.08);border-radius:8px;padding:10px 14px;color:#e2e8f0;font-size:0.85rem;font-family:inherit;outline:none;transition:border 0.15s;}
input:focus{border-color:rgba(155,138,251,0.5);}
.field{margin-bottom:18px;}
.error{background:rgba(248,113,113,0.1);border:1px solid rgba(248,113,113,0.25);border-radius:8px;padding:10px 14px;font-size:0.75rem;color:#f87171;margin-bottom:18px;}
button{width:100%;background:linear-gradient(135deg,#7c6ce0,#4f46e5);color:#fff;border:none;border-radius:8px;padding:11px;font-size:0.85rem;font-weight:600;font-family:inherit;cursor:pointer;margin-top:4px;transition:opacity 0.15s;}
button:hover{opacity:0.9;}
</style>
</head>
<body>
<div class="card">
  <div class="logo">
    <div class="mark">🤖</div>
    <div class="brand">AgentOS<span>Loan Nurture System</span></div>
  </div>
  <h2>Welcome back</h2>
  <p>Sign in to manage your agents</p>
  {% if error %}<div class="error">{{ error }}</div>{% endif %}
  <form method="POST">
    <div class="field">
      <label>Username</label>
      <input type="text" name="username" autocomplete="username" autofocus required/>
    </div>
    <div class="field">
      <label>Password</label>
      <input type="password" name="password" autocomplete="current-password" required/>
    </div>
    <button type="submit">Sign in</button>
  </form>
</div>
</body>
</html>
"""

@app.route("/login", methods=["GET", "POST"])
def login():
    if is_logged_in():
        return redirect("/dashboard")

    error = None
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        if username == DASHBOARD_USERNAME and check_password(password):
            session["logged_in"] = True
            session["username"]  = username
            log.info(f"Login successful for {username}")
            next_url = request.args.get("next", "/dashboard")
            return redirect(next_url)
        else:
            log.warning(f"Failed login attempt for username: {username}")
            error = "Incorrect username or password."

    return render_template_string(LOGIN_PAGE, error=error)


@app.route("/logout")
def logout():
    session.clear()
    log.info("User logged out")
    return redirect("/login")


@app.route("/dashboard")
@login_required
def dashboard():
    """Serves the main AgentOS dashboard."""
    dashboard_path = os.path.join(os.path.dirname(__file__), "dashboard.html")
    return send_file(dashboard_path)


@app.route("/")
def index():
    if is_logged_in():
        return redirect("/dashboard")
    return redirect("/login")


# ════════════════════════════════════════════════════════════
# GoHighLevel Webhooks
# ════════════════════════════════════════════════════════════

@app.route("/webhook/ghl", methods=["POST"])
def ghl_webhook():
    """
    Receives all GoHighLevel webhook events.
    GHL sends events for: contact.created, contact.updated,
    opportunity.created, conversation.message.added, etc.
    """
    # ── Verify webhook signature ──────────────────────────
    if not _verify_ghl_signature(request):
        log.warning("GHL webhook signature verification failed")
        abort(401)

    payload = request.get_json(silent=True)
    if not payload:
        return jsonify({"error": "Empty payload"}), 400

    event_type = payload.get("type") or payload.get("event")
    log.info(f"GHL event received: {event_type}")

    # ── Route by event type ───────────────────────────────
    if event_type == "contact.created":
        _handle_new_contact(payload)

    elif event_type == "conversation.message.added":
        _handle_inbound_message(payload)

    elif event_type == "opportunity.created":
        _handle_opportunity_created(payload)

    elif event_type == "opportunity.updated":
        _handle_opportunity_updated(payload)

    elif event_type == "contact.updated":
        _handle_contact_updated(payload)

    elif event_type == "contact.deleted":
        contact_id = payload.get("id") or payload.get("contactId")
        if contact_id:
            log.info(f"Contact deleted: {contact_id} — stopping any active sequence")
            manager.nurturer.stop(contact_id, reason="contact_deleted")

    else:
        log.info(f"Unhandled GHL event: {event_type}")

    return jsonify({"status": "ok"}), 200


def _handle_new_contact(payload: dict):
    """
    Fires when a new contact lands in GHL (e.g. from Facebook Lead Ads).
    Normalises the GHL payload into a clean lead dict and hands to ManagerAgent.
    """
    contact = payload.get("contact", payload)   # GHL wraps in 'contact' sometimes

    lead = {
        "id":         contact.get("id"),
        "name":       f"{contact.get('firstName', '')} {contact.get('lastName', '')}".strip(),
        "email":      contact.get("email"),
        "phone":      contact.get("phone"),
        "fb_psid":    contact.get("customField", {}).get("fb_psid"),  # map your custom field
        "lead_type":  _detect_lead_type(contact),
        "source":     contact.get("source", "unknown"),
        "notes":      contact.get("notes", ""),
        "raw":        contact,
    }

    if not lead["id"] or not lead["phone"]:
        log.warning("Contact missing ID or phone — skipping")
        return

    manager.handle_new_lead(lead)


def _handle_inbound_message(payload: dict):
    """
    Fires when a contact replies via SMS or email (routed through GHL).
    1. Checks for opt-out keywords → sets DND and stops sequence
    2. Otherwise routes to ManagerAgent for AI response
    """
    message   = payload.get("message", {})
    lead_id   = payload.get("contactId") or message.get("contactId")
    body      = message.get("body", "").strip()
    direction = message.get("direction", "")
    channel   = message.get("type", "sms").lower()

    if direction.lower() == "outbound":
        return   # We sent this — ignore

    if not lead_id or not body:
        return

    log.info(f"Inbound reply from {lead_id} via {channel}: {body[:60]}")

    # ── Opt-out detection ─────────────────────────────────
    if _is_opt_out(body):
        log.info(f"Opt-out detected from {lead_id} — setting DND and stopping sequence")
        ghl.set_dnd(lead_id, dnd=True)
        ghl.tag_contact(lead_id, ["unsubscribed", "do-not-contact"])
        ghl.add_note(lead_id, f"[AgentOS] Lead opted out via {channel}. Message: \"{body}\". DND set.")
        manager.nurturer.stop(lead_id, reason="opted_out")
        # Send one final confirmation reply
        if channel == "sms":
            ghl.send_sms(lead_id, "You've been unsubscribed and won't receive further messages. Reply START anytime to re-subscribe.")
        return

    # ── Re-subscribe detection ────────────────────────────
    if _is_resubscribe(body):
        log.info(f"Re-subscribe request from {lead_id}")
        ghl.set_dnd(lead_id, dnd=False)
        ghl.tag_contact(lead_id, ["resubscribed"])
        ghl.add_note(lead_id, f"[AgentOS] Lead re-subscribed via {channel}.")
        if channel == "sms":
            ghl.send_sms(lead_id, "You're back! We'll be in touch shortly.")
        return

    # ── Normal reply → AI response ────────────────────────
    manager.handle_lead_reply(lead_id, channel, body)


def _handle_contact_updated(payload: dict):
    """
    Fires when a GHL contact is updated.
    Key checks:
    - DND turned ON externally (e.g. by LO in GHL UI) → stop sequence
    - Tags changed → re-classify lead type if needed
    """
    contact_id = payload.get("id") or payload.get("contactId")
    if not contact_id:
        return

    # Check if DND was turned on in GHL manually
    dnd = payload.get("dnd") or (payload.get("contact", {}).get("dnd"))
    if dnd:
        log.info(f"DND set externally for {contact_id} — stopping sequence")
        manager.nurturer.stop(contact_id, reason="dnd_set_externally")
        ghl.add_note(contact_id, "[AgentOS] Nurture sequence stopped — DND was set in GHL.")
        return

    log.info(f"Contact updated: {contact_id}")


def _handle_opportunity_created(payload: dict):
    """
    Fires when a new opportunity is created in GHL.
    Tags the contact and logs it.
    """
    opp        = payload.get("opportunity", payload)
    contact_id = opp.get("contactId")
    pipeline   = opp.get("pipelineId")
    stage      = opp.get("stageId")
    opp_name   = opp.get("name", "unknown")

    log.info(f"Opportunity created: {opp_name} | contact: {contact_id} | stage: {stage}")

    if contact_id:
        ghl.tag_contact(contact_id, ["opportunity-created"])


def _handle_opportunity_updated(payload: dict):
    """
    Fires when an opportunity stage changes in GHL.
    If moved to Won → stop nurture (LO closed the deal).
    If moved to Lost → stop nurture and tag appropriately.
    """
    opp        = payload.get("opportunity", payload)
    contact_id = opp.get("contactId")
    status     = opp.get("status", "").lower()   # "won", "lost", "open", "abandoned"
    stage_name = opp.get("stageName", "").lower()

    if not contact_id:
        return

    if status == "won":
        log.info(f"Deal WON for contact {contact_id} — stopping nurture sequence")
        manager.nurturer.stop(contact_id, reason="deal_won")
        ghl.tag_contact(contact_id, ["closed-won"])
        ghl.add_note(contact_id, "[AgentOS] Nurture sequence stopped — deal marked as Won.")

    elif status in ("lost", "abandoned"):
        log.info(f"Deal {status} for contact {contact_id} — stopping nurture sequence")
        manager.nurturer.stop(contact_id, reason=f"deal_{status}")
        ghl.tag_contact(contact_id, [f"closed-{status}"])
        ghl.add_note(contact_id, f"[AgentOS] Nurture sequence stopped — deal marked as {status.title()}.")


def _is_opt_out(text: str) -> bool:
    """
    Returns True if the message is an opt-out request.
    Covers TCPA-required keywords plus common variations.
    """
    keywords = {
        "stop", "unsubscribe", "cancel", "quit", "end",
        "optout", "opt out", "opt-out", "remove me",
        "take me off", "don't text", "dont text",
        "no more", "stop texting", "stop messaging",
    }
    normalized = text.lower().strip().rstrip("!.").strip()
    return any(normalized == kw or normalized.startswith(kw + " ") for kw in keywords)


def _is_resubscribe(text: str) -> bool:
    """Returns True if the message is a re-subscribe request."""
    keywords = {"start", "resubscribe", "re-subscribe", "subscribe", "yes"}
    normalized = text.lower().strip().rstrip("!.").strip()
    return normalized in keywords


def _detect_lead_type(contact: dict) -> str:
    """
    Tries to detect purchase vs refinance from GHL tags or custom fields.
    Adjust field names to match your GHL setup.
    """
    tags   = contact.get("tags", [])
    source = contact.get("source", "").lower()
    notes  = contact.get("notes", "").lower()

    if any("refi" in t.lower() or "refinance" in t.lower() for t in tags):
        return "refinance"
    if any("purchase" in t.lower() or "buy" in t.lower() for t in tags):
        return "purchase"
    if "refi" in source or "refi" in notes:
        return "refinance"
    if "purchase" in source or "buy" in notes:
        return "purchase"
    return "unknown"


def _verify_ghl_signature(req) -> bool:
    """
    Verifies the HMAC-SHA256 signature on incoming GHL webhooks.
    Skip in dev if GHL_WEBHOOK_SECRET is not set.
    """
    if not GHL_WEBHOOK_SECRET or GHL_WEBHOOK_SECRET == "your-webhook-secret":
        return True   # Skip verification in dev

    sig_header = req.headers.get("X-GHL-Signature", "")
    body       = req.get_data()
    expected   = hmac.new(
        GHL_WEBHOOK_SECRET.encode(), body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(sig_header, expected)


# ════════════════════════════════════════════════════════════
# Facebook Messenger Webhooks
# ════════════════════════════════════════════════════════════

@app.route("/webhook/messenger", methods=["GET"])
def messenger_verify():
    """
    Facebook webhook verification endpoint.
    FB calls this once with a challenge when you register the webhook.
    """
    mode      = request.args.get("hub.mode")
    token     = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")

    if mode == "subscribe" and token == FB_VERIFY_TOKEN:
        log.info("Facebook Messenger webhook verified ✓")
        return challenge, 200
    else:
        log.warning("Facebook Messenger webhook verification failed")
        abort(403)


@app.route("/webhook/messenger", methods=["POST"])
def messenger_event():
    """
    Receives inbound Facebook Messenger messages from leads.
    Routes them to ManagerAgent for AI response.
    """
    payload = request.get_json(silent=True)
    if not payload or payload.get("object") != "page":
        return jsonify({"error": "Not a page event"}), 400

    for entry in payload.get("entry", []):
        for event in entry.get("messaging", []):
            psid    = event.get("sender", {}).get("id")
            message = event.get("message", {})
            text    = message.get("text", "")

            if not psid or not text:
                continue

            log.info(f"Messenger message from PSID {psid}: {text[:60]}")

            # Look up lead_id by PSID
            # TODO: query GHL API or your DB to find the matching contact
            lead_id = _find_lead_id_by_psid(psid)
            if lead_id:
                manager.handle_lead_reply(lead_id, "messenger", text)
            else:
                log.warning(f"No lead found for PSID {psid}")

    return jsonify({"status": "ok"}), 200


def _find_lead_id_by_psid(psid: str) -> str | None:
    """
    Looks up a GHL contact by their Facebook Page-Scoped User ID.
    Requires a custom field named 'fb_psid' on GHL contacts.
    """
    return ghl.find_contact_by_psid(psid)


# ════════════════════════════════════════════════════════════
# Bland AI Callback (Voice Call Results)
# ════════════════════════════════════════════════════════════

@app.route("/webhook/bland", methods=["POST"])
def bland_callback():
    """
    Bland AI calls this URL when a voice call completes.
    Parses the transcript and updates GHL with a call summary note.

    Bland payload includes:
      call_id, completed, transcript (string or list),
      duration, to (phone number), variables
    """
    payload = request.get_json(silent=True)
    if not payload:
        return jsonify({"error": "Empty payload"}), 400

    call_id    = payload.get("call_id", "unknown")
    transcript = payload.get("transcript", "")
    completed  = payload.get("completed", False)
    duration   = payload.get("call_length", 0)         # seconds
    to_number  = payload.get("to", "")

    # Bland can return transcript as a list of {role, text} dicts
    if isinstance(transcript, list):
        transcript = "\n".join(
            f"{t.get('role', '').upper()}: {t.get('text', '')}"
            for t in transcript
        )

    log.info(f"Bland callback — call_id: {call_id}, completed: {completed}, duration: {duration}s")
    log.info(f"Transcript preview: {transcript[:200]}")

    # Find the GHL contact by phone number and attach call note
    contacts = ghl.search_contacts(to_number)
    if contacts:
        contact_id = contacts[0].get("id")
        outcome_str = "completed" if completed else "did not connect / voicemail"
        note = (
            f"[AgentOS Voice Call]\n"
            f"Call ID: {call_id}\n"
            f"Duration: {duration}s | Outcome: {outcome_str}\n\n"
            f"Transcript:\n{transcript[:2000]}"
        )
        ghl.add_note(contact_id, note)

        # If lead answered and call was substantial (>30s), treat as engagement
        if completed and duration > 30:
            ghl.tag_contact(contact_id, ["voice-engaged"])
            log.info(f"Tagged {contact_id} as voice-engaged")
    else:
        log.warning(f"Could not find GHL contact for number {to_number}")

    return jsonify({"status": "ok"}), 200


# ════════════════════════════════════════════════════════════
# Dashboard API — Real Data Endpoints
# ════════════════════════════════════════════════════════════

@app.route("/api/stats", methods=["GET"])
@login_required
def api_stats():
    stats = db.get_stats()
    return jsonify(stats), 200


@app.route("/api/leads", methods=["GET"])
@login_required
def api_leads():
    leads = db.get_all_leads()
    return jsonify(leads), 200


@app.route("/api/leads/<lead_id>", methods=["GET"])
@login_required
def api_lead_detail(lead_id):
    lead = db.get_lead(lead_id)
    if not lead:
        return jsonify({"error": "Not found"}), 404
    steps = db.get_sequence_steps(lead_id)
    convo = db.get_conversation(lead_id)
    return jsonify({"lead": lead, "steps": steps, "conversation": convo}), 200


@app.route("/api/conversations/<lead_id>", methods=["GET"])
@login_required
def api_conversation(lead_id):
    convo = db.get_conversation(lead_id)
    return jsonify(convo), 200


@app.route("/api/logs", methods=["GET"])
@login_required
def api_logs():
    logs = db.get_logs(limit=300)
    return jsonify(logs), 200


@app.route("/api/leads/new", methods=["POST"])
@login_required
def api_new_lead():
    """Create a new lead manually from the dashboard and fire agents."""
    data = request.get_json(silent=True) or {}
    name  = data.get("name", "").strip()
    phone = data.get("phone", "").strip()
    email = data.get("email", "").strip()
    lead_type = data.get("lead_type", "unknown")

    if not name or not phone:
        return jsonify({"error": "name and phone required"}), 400

    # Create contact in GHL first
    try:
        result = ghl.create_contact(name=name, phone=phone, email=email)
        contact_id = result.get("contact", {}).get("id") or result.get("id")
    except Exception as e:
        log.error(f"GHL create_contact failed: {e}")
        return jsonify({"error": "Failed to create GHL contact"}), 500

    if not contact_id:
        return jsonify({"error": "GHL did not return a contact ID"}), 500

    lead = {
        "id": contact_id,
        "name": name,
        "phone": phone,
        "email": email,
        "lead_type": lead_type,
        "source": "manual",
        "notes": "",
        "fb_psid": None,
        "raw": {},
    }

    import threading
    threading.Thread(target=manager.handle_new_lead, args=(lead,), daemon=True).start()
    db.add_log("INFO", f"Manual lead added from dashboard: {name} ({phone})", "SERVER")

    return jsonify({"status": "ok", "contact_id": contact_id}), 200


# ════════════════════════════════════════════════════════════
# Health Check
# ════════════════════════════════════════════════════════════

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "healthy", "service": "loan-agent-system"}), 200


# ════════════════════════════════════════════════════════════
# Run
# ════════════════════════════════════════════════════════════

if __name__ == "__main__":
    log.info(f"Starting webhook server on port {FLASK_PORT}")
    app.run(host="0.0.0.0", port=FLASK_PORT, debug=FLASK_DEBUG)
