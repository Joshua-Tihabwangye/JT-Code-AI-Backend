#!/usr/bin/env python3
# ruff: noqa: E501  (inline-styled email HTML does not wrap usefully)
"""Generates the Supabase Auth (GoTrue) email templates in this folder from one branded layout.

Edit the texts here and run `python3 infra/native/supabase-templates/build.py`. GoTrue fetches the HTML
files from the gateway (`/templates/<name>.html`, GOTRUE_MAILER_TEMPLATES_*) and fills the {{ ... }}
variables: .ConfirmationURL, .Token, .Email, .NewEmail and .Data (user metadata, e.g. first_name).
Colours always contain a letter: the tests read the reset code as the first 6-digit number in the mail.
"""

import re
from pathlib import Path

HERE = Path(__file__).resolve().parent
TEXT = "#3b4a5e"
MUTED = "#64748b"

LAYOUT = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light">
<title>{title}</title>
</head>
<body style="margin:0;padding:0;background:#eef2f9;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;color:#0f172a;">
<div style="display:none;max-height:0;overflow:hidden;">{preheader}</div>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#eef2f9;padding:32px 12px;">
<tr><td align="center">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:520px;background:#ffffff;border-radius:16px;overflow:hidden;box-shadow:0 8px 28px rgba(15,23,42,0.08);">
<tr><td style="background:#2563eb;background-image:linear-gradient(135deg,#2563eb,#0ea5e9);padding:24px 32px;">
<table role="presentation" cellpadding="0" cellspacing="0"><tr>
<td style="background:#ffffff;color:#2563eb;font-weight:800;font-size:14px;border-radius:10px;padding:8px 10px;">JT</td>
<td style="padding-left:12px;color:#ffffff;font-size:18px;font-weight:700;">JT-Code</td>
</tr></table>
</td></tr>
<tr><td style="padding:32px;">
<h1 style="margin:0 0 12px;font-size:22px;line-height:1.3;color:#0f172a;">{heading}</h1>
<p style="margin:0 0 16px;font-size:15px;line-height:1.6;color:{text};">{{{{ if .Data.first_name }}}}Hi {{{{ .Data.first_name }}}},{{{{ else }}}}Hi there,{{{{ end }}}}</p>
{body}
</td></tr>
<tr><td style="padding:20px 32px;background:#f8fafc;border-top:1px solid #e2e8f0;">
<p style="margin:0;font-size:12px;line-height:1.6;color:{muted};">{footer}</p>
<p style="margin:8px 0 0;font-size:12px;line-height:1.6;color:#94a3b8;">JT-Code · AI workspace for chat, images, documents and data</p>
</td></tr>
</table>
</td></tr>
</table>
</body>
</html>
"""


def para(text: str) -> str:
    return f'<p style="margin:0 0 16px;font-size:15px;line-height:1.6;color:{TEXT};">{text}</p>'


def button(label: str, url: str = "{{ .ConfirmationURL }}") -> str:
    return (
        '<table role="presentation" cellpadding="0" cellspacing="0" style="margin:8px 0 24px;"><tr>'
        '<td style="border-radius:10px;background:#2563eb;">'
        f'<a href="{url}" style="display:inline-block;padding:13px 26px;font-size:15px;font-weight:600;'
        f'color:#ffffff;text-decoration:none;border-radius:10px;">{label}</a></td></tr></table>'
    )


def fallback(url: str = "{{ .ConfirmationURL }}") -> str:
    return (
        f'<p style="margin:0 0 6px;font-size:13px;line-height:1.6;color:{MUTED};">'
        "If the button does not work, copy this link into your browser:</p>"
        '<p style="margin:0 0 16px;font-size:13px;line-height:1.5;word-break:break-all;">'
        f'<a href="{url}" style="color:#2563eb;">{url}</a></p>'
    )


def code() -> str:
    return (
        '<div style="margin:8px 0 24px;padding:18px;border-radius:12px;background:#eff6ff;'
        'border:1px solid #bfdbfe;text-align:center;"><div style="font-size:32px;font-weight:800;'
        'letter-spacing:8px;color:#1e3a8a;font-family:Menlo,Consolas,monospace;">{{ .Token }}</div></div>'
    )


TEMPLATES = {
    "confirmation.html": {
        "title": "Confirm your JT-Code account",
        "preheader": "One click to activate your JT-Code account.",
        "heading": "Confirm your email address",
        "body": para(
            "Welcome to JT-Code! Confirm <strong>{{ .Email }}</strong> to activate your account and start "
            "building with chat, images, documents and data dashboards."
        )
        + button("Confirm my account")
        + fallback(),
        "footer": "You are receiving this because someone signed up for JT-Code with this address. "
        "If it was not you, ignore this email and no account will be activated.",
    },
    "recovery.html": {
        "title": "Reset your JT-Code password",
        "preheader": "Your JT-Code password reset code is inside.",
        "heading": "Reset your password",
        "body": para(
            "We received a request to reset the password for <strong>{{ .Email }}</strong>. "
            "Enter this code on the password reset page:"
        )
        + code()
        + para("The code can be used once and expires shortly."),
        "footer": "If you did not ask to reset your password, ignore this email. Your password stays the same.",
    },
    "magic_link.html": {
        "title": "Your JT-Code sign-in link",
        "preheader": "Sign in to JT-Code with one click.",
        "heading": "Sign in to JT-Code",
        "body": para(
            "Use the button below to sign in as <strong>{{ .Email }}</strong>. The link works once and expires shortly."
        )
        + button("Sign in")
        + fallback(),
        "footer": "If you did not try to sign in, ignore this email.",
    },
    "email_change.html": {
        "title": "Confirm your new JT-Code email",
        "preheader": "Confirm the new email address for your JT-Code account.",
        "heading": "Confirm your new email address",
        "body": para(
            "You asked to change your JT-Code email from <strong>{{ .Email }}</strong> to "
            "<strong>{{ .NewEmail }}</strong>. Confirm the change:"
        )
        + button("Confirm new email")
        + fallback(),
        "footer": "If you did not ask for this change, ignore this email. Your email stays the same.",
    },
    "invite.html": {
        "title": "You're invited to JT-Code",
        "preheader": "Join your team on JT-Code.",
        "heading": "You have been invited to JT-Code",
        "body": para(
            "You have been invited to join a JT-Code workspace as <strong>{{ .Email }}</strong>. "
            "Accept the invitation to set up your account:"
        )
        + button("Accept invitation")
        + fallback(),
        "footer": "If you were not expecting this invitation, you can ignore this email.",
    },
}


def main() -> None:
    for name, parts in TEMPLATES.items():
        html = LAYOUT.format(text=TEXT, muted=MUTED, **parts)
        digits_only = re.findall(r"#\d{6}\b", html)
        assert not digits_only, f"{name}: colours made only of digits look like a reset code: {digits_only}"
        (HERE / name).write_text(html)
    print(f"wrote {', '.join(sorted(TEMPLATES))}")


if __name__ == "__main__":
    main()
