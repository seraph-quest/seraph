"""Email integration tool — Gmail via simplegmail."""

import logging
from pathlib import Path

from smolagents import tool

from config.settings import settings

logger = logging.getLogger(__name__)


def _get_gmail():
    """Get a Gmail client, handling OAuth."""
    from simplegmail import Gmail

    credentials_path = Path(settings.google_credentials_path)
    token_path = Path(settings.google_gmail_token_path)

    if not credentials_path.exists():
        raise FileNotFoundError(
            f"Google credentials not found at {credentials_path}. "
            "Please set up OAuth credentials for Gmail."
        )

    return Gmail(
        client_secret_file=str(credentials_path),
        creds_file=str(token_path),
    )


@tool
def read_emails(query: str = "", max_results: int = 10) -> str:
    """Read emails from your Gmail inbox.

    Use this tool to check for new messages, find specific emails,
    or review recent communications.

    Args:
        query: Gmail search query (e.g., 'from:boss@company.com', 'is:unread',
               'subject:project update'). Empty string returns recent inbox messages.
        max_results: Maximum number of emails to return (default: 10).

    Returns:
        A formatted list of emails with sender, subject, date, and snippet.
    """
    try:
        gmail = _get_gmail()

        if query:
            messages = gmail.get_messages(query=query, max_results=max_results)
        else:
            messages = gmail.get_inbox_messages(max_results=max_results)

        if not messages:
            return "No emails found matching your query." if query else "Your inbox is empty."

        lines = [f"📧 {len(messages)} email(s) found:\n"]
        for msg in messages:
            date_str = msg.date if msg.date else "Unknown date"
            sender = msg.sender if msg.sender else "Unknown sender"
            subject = msg.subject if msg.subject else "(no subject)"
            snippet = msg.snippet[:120] if msg.snippet else ""

            lines.append(f"- From: {sender}")
            lines.append(f"  Subject: {subject}")
            lines.append(f"  Date: {date_str}")
            if snippet:
                lines.append(f"  Preview: {snippet}...")
            lines.append("")

        return "\n".join(lines)

    except FileNotFoundError as e:
        return str(e)
    except Exception as e:
        logger.exception("Email read failed")
        return f"Error reading emails: {e}"


@tool
def send_email(to: str, subject: str, body: str) -> str:
    """Send an email via Gmail.

    Use this tool to send messages, follow up on conversations,
    or communicate with contacts. Use with care — emails are sent
    immediately and cannot be undone.

    Args:
        to: Recipient email address.
        subject: Email subject line.
        body: Email body text (plain text).

    Returns:
        Confirmation that the email was sent.
    """
    if not to or "@" not in to:
        return f"Error: Invalid email address '{to}'."

    try:
        gmail = _get_gmail()

        params = {
            "to": to,
            "subject": subject,
            "msg_plain": body,
            "signature": True,
        }

        msg = gmail.send_message(**params)

        return f"Email sent to {to} with subject '{subject}'."

    except FileNotFoundError as e:
        return str(e)
    except Exception as e:
        logger.exception("Email send failed")
        return f"Error sending email: {e}"
