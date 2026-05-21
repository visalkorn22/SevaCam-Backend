import smtplib
from email.message import EmailMessage
from typing import Optional

from app.core.config import settings


def _build_from_header() -> str:
    from_email = settings.SMTP_FROM_EMAIL or ""
    from_name = settings.SMTP_FROM_NAME
    if from_name:
        return f"{from_name} <{from_email}>"
    return from_email


def _smtp_configured() -> bool:
    return bool(settings.SMTP_HOST and settings.SMTP_FROM_EMAIL)


def send_email(to_email: str, subject: str, body: str) -> None:
    if not _smtp_configured():
        raise RuntimeError("SMTP is not configured")

    message = EmailMessage()
    message["From"] = _build_from_header()
    message["To"] = to_email
    message["Subject"] = subject
    message.set_content(body)

    smtp_host = settings.SMTP_HOST or ""
    smtp_port = settings.SMTP_PORT
    username: Optional[str] = settings.SMTP_USERNAME
    password: Optional[str] = settings.SMTP_PASSWORD

    if settings.SMTP_USE_SSL:
        server = smtplib.SMTP_SSL(smtp_host, smtp_port, timeout=10)
    else:
        server = smtplib.SMTP(smtp_host, smtp_port, timeout=10)

    try:
        server.ehlo()
        if settings.SMTP_USE_TLS and not settings.SMTP_USE_SSL:
            server.starttls()
            server.ehlo()

        if username and password:
            server.login(username, password)

        server.send_message(message)
    finally:
        server.quit()
