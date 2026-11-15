"""Real email for the playdate app: off unless PLAYDATE_EMAILS=on.

Every message the app writes still goes to Admin > Messages exactly as before. When
PLAYDATE_EMAILS=on (and MAIL_PROVIDER=resend with PLAYDATE_MAIL_FROM set), emails are also
sent for real through mailer.py. Texts are untouched (Quo comes later).

Off by default so the online test version can't email anyone while playdates are in test mode.
"""
import os

import mailer

ENABLED = os.environ.get("PLAYDATE_EMAILS", "off").strip().lower() in ("on", "yes", "true", "1")
MAILER = mailer.build_mailer("PLAYDATE_MAIL_FROM") if ENABLED else None


def deliver(to, subject, body):
    """Send one email if real email is switched on. Never raises; failures are written to the log."""
    if MAILER is None or not to:
        return False
    return MAILER.send(to, subject or "Gooseberry Bridge Farm", body)
