# ============================================================
# config.py — All API keys, LO details, and channel settings
# ============================================================
# Replace every placeholder value with your real credentials.
# NEVER commit this file to GitHub — add it to .gitignore.

import os

# ── Loan Officer Info ─────────────────────────────────────
LO_NAME        = "Uday Yewale"
LO_COMPANY     = "YZX - Headd Media"
LO_PHONE       = "+919022303987"         # Update with your preferred contact number
LO_EMAIL       = "Udayyewale4213@gmail.com"
LO_NMLS        = "12345678"              # Update with real NMLS number
LO_TIMEZONE    = "America/Chicago"

# ── Claude API ────────────────────────────────────────────
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "sk-ant-XXXXXXXXXXXXXXXX")
CLAUDE_MODEL      = "claude-sonnet-4-6"
MAX_TOKENS        = 1024

# ── GoHighLevel (GHL) ─────────────────────────────────────
GHL_API_KEY        = os.getenv("GHL_API_KEY", "your-ghl-api-key")
GHL_LOCATION_ID    = os.getenv("GHL_LOCATION_ID", "your-location-id")
GHL_WEBHOOK_SECRET = os.getenv("GHL_WEBHOOK_SECRET", "your-webhook-secret")
GHL_BASE_URL       = "https://services.leadconnectorhq.com"

# Pipeline IDs — find these in GHL → Settings → Pipelines
# Copy the IDs from your browser URL when viewing each pipeline/stage
GHL_PIPELINE_ID     = os.getenv("GHL_PIPELINE_ID", "")      # your main mortgage pipeline
GHL_STAGE_NEW       = os.getenv("GHL_STAGE_NEW", "")        # "New Lead" stage ID
GHL_STAGE_CONTACTED = os.getenv("GHL_STAGE_CONTACTED", "")  # "Contacted" stage ID
GHL_STAGE_NURTURING = os.getenv("GHL_STAGE_NURTURING", "")  # "Nurturing" stage ID
GHL_STAGE_QUALIFIED = os.getenv("GHL_STAGE_QUALIFIED", "")  # "Qualified" stage ID (triggers LO handoff)

# ── SMS + Email ───────────────────────────────────────────
# Sent via GHL's built-in messaging (A2P 10DLC verified)
# No Twilio or SendGrid needed — GHL handles both channels
EMAIL_FROM_ADDRESS  = LO_EMAIL
EMAIL_FROM_NAME     = LO_NAME

# ── Bland AI (Voice Calls) ────────────────────────────────
BLAND_API_KEY       = os.getenv("BLAND_API_KEY", "your-bland-api-key")
BLAND_BASE_URL      = "https://api.bland.ai/v1"
BLAND_VOICE_ID      = "mason"            # Bland AI voice preset

# ── Facebook Messenger ────────────────────────────────────
FB_PAGE_ACCESS_TOKEN = os.getenv("FB_PAGE_ACCESS_TOKEN", "your-fb-page-token")
FB_VERIFY_TOKEN      = os.getenv("FB_VERIFY_TOKEN",      "your-fb-verify-token")

# ── Webhook Server ────────────────────────────────────────
FLASK_PORT          = 5000
FLASK_DEBUG         = False
WEBHOOK_BASE_URL    = "https://yourdomain.com"  # Your public server URL (ngrok during dev)

# ── Timing ────────────────────────────────────────────────
OPENER_DELAY_SECONDS  = 90       # Fire Agent 1 within 90 seconds of lead
NURTURER_SCHEDULE_DAYS = [       # Days to follow up after initial contact
    1, 2, 3, 5, 7, 9, 11, 14
]

# ── Lead Types ────────────────────────────────────────────
LEAD_TYPES = {
    "purchase":   "Purchase",
    "refinance":  "Refinance",
    "unknown":    "Unknown"
}

# ── Channel Toggle — set False to disable a channel ───────
CHANNELS = {
    "sms":        True,
    "email":      True,
    "messenger":  False,
    "voice":      False,   # Enable later with Bland AI key
}
