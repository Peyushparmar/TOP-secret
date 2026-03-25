# ============================================================
# opener_agent.py — Agent 1: Multi-channel outreach
#                   Fires within 2 minutes of lead creation
#                   Channels: SMS (via GHL), Email, Facebook
#                             Messenger, AI Voice (Bland AI)
# ============================================================

from __future__ import annotations

import logging
import requests

import anthropic

from config import (
    ANTHROPIC_API_KEY, CLAUDE_MODEL, MAX_TOKENS,
    BLAND_API_KEY, BLAND_BASE_URL, BLAND_VOICE_ID,
    FB_PAGE_ACCESS_TOKEN,
    LO_NAME, LO_COMPANY, LO_PHONE, LO_NMLS,
    CHANNELS,
)
import ghl_client as ghl

log = logging.getLogger(__name__)
log.setLevel(logging.INFO)

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)


class OpenerAgent:
    """
    Agent 1 — The Speed Agent.

    As soon as a lead comes in, this agent blasts outreach across
    every enabled channel simultaneously. The goal is to make
    contact within 2 minutes before the lead goes cold.

    Channels:
      ① SMS via Twilio
      ② Email via SendGrid
      ③ Facebook Messenger via Graph API
      ④ AI Voice Call via Bland AI
    """

    def run(self, lead: dict, classification: dict):
        """
        Main entry point. Called by ManagerAgent after the delay timer.
        Fires all enabled channels in sequence (can be parallelised with threading).
        """
        log.info(f"OpenerAgent running for {lead.get('name')}")

        # Generate personalised messages for each channel with Claude
        messages = self._generate_messages(lead, classification)

        # Fire each enabled channel
        contact_id = lead["id"]   # GHL contact ID used for all GHL messaging
        results = {}
        if CHANNELS.get("sms"):
            results["sms"]       = self.send_sms(contact_id, messages["sms"])
        if CHANNELS.get("email"):
            results["email"]     = self.send_email(
                contact_id, lead["name"],
                messages["email_subject"], messages["email_body"]
            )
        if CHANNELS.get("messenger") and lead.get("fb_psid"):
            results["messenger"] = self.send_messenger(lead["fb_psid"], messages["messenger"])
        if CHANNELS.get("voice"):
            results["voice"]     = self.make_voice_call(lead["phone"], lead["name"], classification)

        log.info(f"Opener results: {results}")
        return results

    # ── Message Generation (Claude) ───────────────────────
    def _generate_messages(self, lead: dict, classification: dict) -> dict:
        """
        Uses Claude to write channel-specific opening messages
        tailored to the lead's type (purchase vs refinance) and tone.
        """
        lead_type   = classification.get("lead_type", "mortgage")
        tone        = classification.get("tone", "professional")
        opening_hook = classification.get("opening_hook", "")

        prompt = f"""
You are writing the very first outreach messages for a mortgage enquiry.

Loan Officer: {LO_NAME} at {LO_COMPANY}
Lead Name: {lead.get('name', 'there')}
Loan Type: {lead_type}
Tone: {tone}
Opening Hook: {opening_hook}

Write 4 separate messages — one per channel.
Rules:
- SMS: max 160 characters, casual, include first name, end with a question
- Email Subject: max 9 words, curiosity-driven
- Email Body: 3–4 short paragraphs, warm, include NMLS #{LO_NMLS}, no pushy CTA
- Messenger: same as SMS but slightly more casual
- All messages must feel human — never robotic or salesy

Respond in this exact JSON format:
{{
  "sms": "...",
  "email_subject": "...",
  "email_body": "...",
  "messenger": "..."
}}
"""
        try:
            resp = client.messages.create(
                model=CLAUDE_MODEL,
                max_tokens=1024,
                messages=[{"role": "user", "content": prompt}]
            )
            import json
            return json.loads(resp.content[0].text)
        except Exception as e:
            log.error(f"Message generation failed: {e}")
            # Fallback messages
            name = lead.get("name", "there").split()[0]
            return {
                "sms":           f"Hi {name}! This is {LO_NAME} from {LO_COMPANY}. I saw you were interested in a {lead_type} loan — have a quick minute to chat?",
                "email_subject": f"Your {lead_type} loan enquiry — {LO_NAME}",
                "email_body":    f"Hi {lead.get('name')},\n\nThanks for your interest! I'd love to help you with your {lead_type} loan.\n\nI'm {LO_NAME}, NMLS #{LO_NMLS} at {LO_COMPANY}. Can we schedule a quick 15-minute call?\n\nBest,\n{LO_NAME}",
                "messenger":     f"Hey {name}! {LO_NAME} here from {LO_COMPANY} 👋 Saw you're looking into a {lead_type} — want to chat?",
            }

    # ── Channel 1: SMS via GHL (A2P verified) ────────────
    def send_sms(self, contact_id: str, body: str) -> dict:
        """
        Sends SMS through GHL's built-in messaging (A2P 10DLC verified).
        Uses the GHL conversations/messages API so the message appears
        in the GHL conversation thread automatically.

        contact_id: GHL contact ID (not phone number — GHL resolves the number)
        """
        log.info(f"Sending SMS via GHL to contact {contact_id}")
        return ghl.send_sms(contact_id, body)

    # ── Channel 2: Email via GHL ──────────────────────────
    def send_email(self, contact_id: str, name: str, subject: str, body: str) -> dict:
        """
        Sends email through GHL's built-in email system.
        Message appears in the GHL conversation thread automatically.
        """
        log.info(f"Sending email via GHL to contact {contact_id}")
        return ghl.send_email(contact_id, subject, body)

    # ── Channel 3: Facebook Messenger ─────────────────────
    def send_messenger(self, psid: str, message: str) -> dict:
        """
        Sends a Facebook Messenger message via the Graph API.
        psid = Page-Scoped User ID (comes from GHL or FB Lead Ads).
        """
        log.info(f"Sending Messenger message to PSID {psid}")
        try:
            url = "https://graph.facebook.com/v18.0/me/messages"
            payload = {
                "recipient": {"id": psid},
                "message":   {"text": message},
                "messaging_type": "RESPONSE"
            }
            params = {"access_token": FB_PAGE_ACCESS_TOKEN}
            resp = requests.post(url, json=payload, params=params, timeout=10)
            resp.raise_for_status()
            log.info(f"Messenger sent — {resp.json()}")
            return {"success": True, "response": resp.json()}
        except Exception as e:
            log.error(f"Messenger failed: {e}")
            return {"success": False, "error": str(e)}

    # ── Channel 4: AI Voice Call via Bland AI ─────────────
    def make_voice_call(self, to_number: str, lead_name: str, classification: dict) -> dict:
        """
        Initiates an AI voice call using Bland AI.
        The AI introduces itself as an assistant for the loan officer
        and tries to book a callback appointment.
        """
        log.info(f"Initiating Bland AI voice call to {to_number}")
        lead_type = classification.get("lead_type", "mortgage")
        first_name = lead_name.split()[0] if lead_name else "there"

        task_prompt = f"""
You are a friendly assistant calling on behalf of {LO_NAME} at {LO_COMPANY}.
You are calling {first_name} about their {lead_type} loan enquiry.

Your goal:
1. Confirm they submitted an enquiry
2. Let them know {LO_NAME} will be in touch very shortly
3. Ask if now is a good time or schedule a callback

Keep it short — under 90 seconds total.
Be warm, professional, and human. Never be pushy.
If they don't answer, leave a brief voicemail.
"""
        try:
            resp = requests.post(
                f"{BLAND_BASE_URL}/calls",
                headers={"authorization": BLAND_API_KEY},
                json={
                    "phone_number":   to_number,
                    "task":           task_prompt,
                    "voice":          BLAND_VOICE_ID,
                    "reduce_latency": True,
                    "record":         True,
                    "max_duration":   2,         # minutes
                    "voicemail_message": (
                        f"Hi {first_name}, this is an automated call from {LO_COMPANY}. "
                        f"{LO_NAME} received your mortgage enquiry and will call you back shortly. "
                        f"You can also reach us at {LO_PHONE}. Have a great day!"
                    ),
                },
                timeout=15
            )
            resp.raise_for_status()
            data = resp.json()
            log.info(f"Voice call initiated — call_id: {data.get('call_id')}")
            return {"success": True, "call_id": data.get("call_id")}
        except Exception as e:
            log.error(f"Voice call failed: {e}")
            return {"success": False, "error": str(e)}
