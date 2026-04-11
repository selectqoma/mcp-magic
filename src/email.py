"""Transactional email via Resend.

Four emails:
  1. signup_welcome   — sent on /register; delivers API key + onboarding
  2. resend_api_key   — sent via /resend-key; re-delivers existing API key
  3. payment_receipt  — sent on subscription.created / subscription.updated
  4. payment_failed   — sent on subscription.past_due
"""

import logging

from src.config import settings

logger = logging.getLogger(__name__)

FROM_ADDRESS = "GovCon MCP <hello@govconmcp.com>"
REPLY_TO = "support@govconmcp.com"


def _client():
    """Lazy import so missing resend package doesn't break import at startup."""
    try:
        import resend
        resend.api_key = settings.resend_api_key
        return resend
    except ImportError:
        logger.error("resend package not installed — emails disabled")
        return None


async def send_welcome(email: str, api_key: str, trial_ends_at: str | None) -> None:
    """Send signup welcome + API key delivery email."""
    resend = _client()
    if not resend or not settings.resend_api_key:
        logger.warning("Resend not configured — skipping welcome email")
        return

    trial_line = (
        f"<p>Your <strong>14-day Pro trial</strong> is active until <strong>{trial_ends_at}</strong>. "
        "No credit card needed — enjoy unlimited searches while you evaluate.</p>"
        if trial_ends_at
        else ""
    )

    mcp_config = (
        '{"mcpServers":{"govcon-mcp":{'
        '"command":"npx","args":["-y","mcp-remote","https://api.govconmcp.com/mcp/"],'
        f'"env":{{"MCP_API_KEY":"{api_key}"}}'
        "}}}"
    )

    html = f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"></head>
<body style="font-family:sans-serif;max-width:600px;margin:40px auto;color:#1a1a1a">
  <h1 style="font-size:24px;margin-bottom:4px">Welcome to GovCon MCP 🎉</h1>
  <p style="color:#666;margin-top:0">Your API key is ready</p>

  <p>Here is your API key. Keep it safe — it grants full access to your account.</p>
  <pre style="background:#f4f4f4;border:1px solid #ddd;border-radius:6px;padding:16px;font-size:14px;word-break:break-all">{api_key}</pre>

  {trial_line}

  <h2 style="font-size:18px">Quick start (Claude Desktop)</h2>
  <p>Add this to your <code>claude_desktop_config.json</code>:</p>
  <pre style="background:#f4f4f4;border:1px solid #ddd;border-radius:6px;padding:16px;font-size:13px">{mcp_config}</pre>

  <h2 style="font-size:18px">What you can do</h2>
  <ul>
    <li>Search 100k+ live government solicitations by keyword, NAICS, agency, or value</li>
    <li>Parse PDF solicitations into structured requirements (Pro)</li>
    <li>Match opportunities to your company profile (Pro)</li>
    <li>Track competitor awards on USASpending.gov (Pro)</li>
  </ul>

  <p>
    <a href="https://govconmcp.com/docs" style="background:#2563eb;color:#fff;padding:10px 20px;border-radius:6px;text-decoration:none;display:inline-block">
      Read the Docs
    </a>
  </p>

  <hr style="border:none;border-top:1px solid #eee;margin:32px 0">
  <p style="color:#999;font-size:12px">
    GovCon MCP · GovCon Intelligence for AI Agents<br>
    Questions? Reply to this email or visit <a href="https://govconmcp.com">govconmcp.com</a>
  </p>
</body>
</html>"""

    try:
        resend.Emails.send({
            "from": FROM_ADDRESS,
            "to": [email],
            "reply_to": REPLY_TO,
            "subject": "Your GovCon MCP API key",
            "html": html,
        })
        logger.info(f"Welcome email sent to {email}")
    except Exception as e:
        logger.error(f"Failed to send welcome email to {email}: {e}")


async def send_resend_api_key(email: str, api_key: str) -> None:
    """Re-send the API key to an existing user (forgot access)."""
    resend = _client()
    if not resend or not settings.resend_api_key:
        logger.warning("Resend not configured — skipping resend-key email")
        return

    html = f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"></head>
<body style="font-family:sans-serif;max-width:600px;margin:40px auto;color:#1a1a1a">
  <h1 style="font-size:24px">Your GovCon MCP API key</h1>
  <p>You requested your API key. Here it is:</p>
  <pre style="background:#f4f4f4;border:1px solid #ddd;border-radius:6px;padding:16px;font-size:14px;word-break:break-all">{api_key}</pre>

  <p>
    Add it to your MCP config as <code>MCP_API_KEY</code>. See the
    <a href="https://govconmcp.com/docs">docs</a> for setup instructions.
  </p>

  <p style="color:#999;font-size:12px;margin-top:32px">
    Didn't request this? Your key is unchanged — no action needed.
    Contact <a href="mailto:support@govconmcp.com">support@govconmcp.com</a> if you're concerned.
  </p>
</body>
</html>"""

    try:
        resend.Emails.send({
            "from": FROM_ADDRESS,
            "to": [email],
            "reply_to": REPLY_TO,
            "subject": "Your GovCon MCP API key",
            "html": html,
        })
        logger.info(f"API key re-sent to {email}")
    except Exception as e:
        logger.error(f"Failed to re-send API key to {email}: {e}")


async def send_payment_receipt(
    email: str,
    tier: str,
    amount_str: str,
    subscription_id: str,
) -> None:
    """Send payment confirmation when a subscription is created or upgraded."""
    resend = _client()
    if not resend or not settings.resend_api_key:
        return

    tier_label = tier.title()

    html = f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"></head>
<body style="font-family:sans-serif;max-width:600px;margin:40px auto;color:#1a1a1a">
  <h1 style="font-size:24px">Payment confirmed ✓</h1>
  <p>Thank you — your <strong>GovCon MCP {tier_label}</strong> subscription is active.</p>

  <table style="width:100%;border-collapse:collapse;margin:24px 0">
    <tr style="border-bottom:1px solid #eee">
      <td style="padding:8px 0;color:#666">Plan</td>
      <td style="padding:8px 0;text-align:right"><strong>{tier_label}</strong></td>
    </tr>
    <tr style="border-bottom:1px solid #eee">
      <td style="padding:8px 0;color:#666">Amount</td>
      <td style="padding:8px 0;text-align:right"><strong>{amount_str}</strong></td>
    </tr>
    <tr>
      <td style="padding:8px 0;color:#666">Subscription ID</td>
      <td style="padding:8px 0;text-align:right;font-size:12px;color:#999">{subscription_id}</td>
    </tr>
  </table>

  <p>
    All Pro features are now unlocked — including PDF parsing, profile matching,
    and competitor tracking.
  </p>

  <p>
    <a href="https://govconmcp.com/docs" style="background:#2563eb;color:#fff;padding:10px 20px;border-radius:6px;text-decoration:none;display:inline-block">
      View Docs
    </a>
  </p>

  <p style="color:#999;font-size:12px;margin-top:32px">
    Manage your subscription at <a href="https://govconmcp.com/billing">govconmcp.com/billing</a>.<br>
    Questions? <a href="mailto:support@govconmcp.com">support@govconmcp.com</a>
  </p>
</body>
</html>"""

    try:
        resend.Emails.send({
            "from": FROM_ADDRESS,
            "to": [email],
            "reply_to": REPLY_TO,
            "subject": f"GovCon MCP {tier_label} — payment confirmed",
            "html": html,
        })
        logger.info(f"Payment receipt sent to {email} ({tier})")
    except Exception as e:
        logger.error(f"Failed to send payment receipt to {email}: {e}")


async def send_payment_failed(email: str, subscription_id: str) -> None:
    """Send payment failure notice when subscription goes past_due."""
    resend = _client()
    if not resend or not settings.resend_api_key:
        return

    html = f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"></head>
<body style="font-family:sans-serif;max-width:600px;margin:40px auto;color:#1a1a1a">
  <h1 style="font-size:24px">Action required: payment failed</h1>
  <p>
    We couldn't process your GovCon MCP subscription payment.
    Your account has been temporarily downgraded to the free tier.
  </p>

  <p>
    <a href="https://govconmcp.com/billing" style="background:#dc2626;color:#fff;padding:10px 20px;border-radius:6px;text-decoration:none;display:inline-block">
      Update Payment Method
    </a>
  </p>

  <p>Once updated, your Pro access will be restored immediately.</p>

  <p style="color:#999;font-size:12px;margin-top:32px">
    Subscription ID: {subscription_id}<br>
    Need help? <a href="mailto:support@govconmcp.com">support@govconmcp.com</a>
  </p>
</body>
</html>"""

    try:
        resend.Emails.send({
            "from": FROM_ADDRESS,
            "to": [email],
            "reply_to": REPLY_TO,
            "subject": "GovCon MCP — payment failed, action required",
            "html": html,
        })
        logger.info(f"Payment failed email sent to {email}")
    except Exception as e:
        logger.error(f"Failed to send payment failed email to {email}: {e}")
