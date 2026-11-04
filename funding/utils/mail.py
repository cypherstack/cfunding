import asyncio
import smtplib
import ssl
from email.message import EmailMessage

import settings


class MailError(Exception):
    pass


def cfg(name: str, default=None):
    return getattr(settings, name, default)


def mail_enabled() -> bool:
    if not cfg("MAIL_ENABLED", False):
        return False
    return bool(cfg("SMTP_HOST")) and bool(cfg("MAIL_FROM"))


def _send_blocking(msg: EmailMessage) -> None:
    host = cfg("SMTP_HOST")
    port = int(cfg("SMTP_PORT", 587))
    timeout = int(cfg("SMTP_TIMEOUT", 10))

    if cfg("SMTP_SSL", False):
        server = smtplib.SMTP_SSL(host, port, timeout=timeout, context=ssl.create_default_context())
    else:
        server = smtplib.SMTP(host, port, timeout=timeout)

    with server:
        if cfg("SMTP_STARTTLS", True) and not cfg("SMTP_SSL", False):
            server.starttls(context=ssl.create_default_context())
        user = cfg("SMTP_USER")
        if user:
            server.login(user, cfg("SMTP_PASSWORD", ""))
        server.send_message(msg)


async def send_mail(to: str, subject: str, body: str) -> None:
    if not mail_enabled():
        raise MailError("mail is not configured")

    msg = EmailMessage()
    msg["From"] = cfg("MAIL_FROM")
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)

    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _send_blocking, msg)
