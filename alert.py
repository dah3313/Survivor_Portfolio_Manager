# alert.py — Survivor Portfolio Manager (SPM)
# ============================================
# Sends operational alerts via email (full detail) and SMS (short summary).
# SMS uses Verizon's email-to-text gateway as primary, with Twilio as an
# optional upgrade later.
#
# Credentials are loaded from environment variables so they never appear
# in source code or state files.

import os
import smtplib
import traceback
import logging
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

logger = logging.getLogger('spm.alert')

# --- Credential source: environment variables ---
SMTP_SERVER = os.environ.get('SPM_SMTP_SERVER', 'smtp.gmail.com')
SMTP_PORT = int(os.environ.get('SPM_SMTP_PORT', '587'))
EMAIL_SENDER = os.environ.get('SPM_EMAIL_SENDER', '')
EMAIL_PASSWORD = os.environ.get('SPM_EMAIL_PASSWORD', '')  # Gmail App Password
EMAIL_RECIPIENT = os.environ.get('SPM_EMAIL_RECIPIENT', '')
SMS_GATEWAY = os.environ.get('SPM_SMS_GATEWAY', '')  # e.g. 5551234567@vtext.com

# Feature flags
USE_EMAIL = bool(EMAIL_SENDER and EMAIL_RECIPIENT)
USE_SMS = bool(SMS_GATEWAY)


class AlertManager:
    """Dispatches operational alerts to email and/or SMS."""

    def send_success(self, message):
        subject = '[SPM] Run Successful'
        self._dispatch(subject, message)

    def send_error(self, error_message, exception=None):
        subject = '[SPM] FAILURE'
        body_full = error_message + '\n'
        if exception:
            body_full += f'\nTraceback:\n{traceback.format_exc()}'
        body_short = f'{error_message} Check email for details.'
        self._dispatch(subject, body_full, body_short)

    def send_heartbeat(self):
        """
        Called by a separate cron job or systemd timer to confirm the
        host is alive. If this stops arriving, something is wrong with
        the machine itself.
        """
        self._dispatch('[SPM] Heartbeat', 'SPM host is alive and reachable.')

    def send_custom(self, subject, body):
        """For ad-hoc alerts (e.g., buffer refill progress)."""
        self._dispatch(subject, body)

    # ------------------------------------------------------------------
    # Internal routing
    # ------------------------------------------------------------------
    def _dispatch(self, subject, body_full, body_short=None):
        if body_short is None:
            body_short = body_full

        if USE_EMAIL:
            self._send_email(EMAIL_RECIPIENT, subject, body_full)

        if USE_SMS:
            # SMS gateways work best with short, flat text
            sms_text = f'{subject}: {body_short}'
            self._send_email(SMS_GATEWAY, '', sms_text)

    def _send_email(self, target, subject, body):
        try:
            msg = MIMEMultipart()
            msg['From'] = EMAIL_SENDER
            msg['To'] = target
            if subject:
                msg['Subject'] = subject
            msg.attach(MIMEText(body, 'plain'))

            with smtplib.SMTP(SMTP_SERVER, SMTP_PORT, timeout=30) as server:
                server.starttls()
                server.login(EMAIL_SENDER, EMAIL_PASSWORD)
                server.send_message(msg)

            logger.info('Alert sent to %s', target)
        except Exception as e:
            # Alert failures must never crash the main program
            logger.error('Failed to send alert to %s: %s', target, e)
