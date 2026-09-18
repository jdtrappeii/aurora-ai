"""Send the weekly report by email over SMTP (STARTTLS). Off until SMTP_HOST
and REPORT_TO are set; then the scheduler sends it every REPORT_WEEKDAY."""
from __future__ import annotations

import smtplib
from email.message import EmailMessage


def send_report(settings, subject: str, html_body: str, text_body: str, to: list[str] | None = None) -> dict:
    recipients = to or [a.strip() for a in settings.report_to.split(",") if a.strip()]
    if not settings.smtp_host or not recipients:
        return {"sent": False, "reason": "SMTP_HOST or REPORT_TO not set", "recipients": recipients}
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = settings.smtp_from or settings.smtp_user
    msg["To"] = ", ".join(recipients)
    msg.set_content(text_body)
    msg.add_alternative(html_body, subtype="html")
    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=30) as s:
        if settings.smtp_starttls:
            s.starttls()
        if settings.smtp_user:
            s.login(settings.smtp_user, settings.smtp_password)
        s.send_message(msg)
    return {"sent": True, "recipients": recipients, "subject": subject}
