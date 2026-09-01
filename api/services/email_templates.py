"""Jinja2-rendered email templates (templates/emails/*.html + .txt)."""
import os

from jinja2 import Environment, FileSystemLoader, select_autoescape

from api.config import settings

TEMPLATE_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "templates", "emails")

_env = Environment(
    loader=FileSystemLoader(TEMPLATE_DIR),
    autoescape=select_autoescape(["html"]),
)

TEMPLATES = {"welcome", "first_purchase", "purchase_summary",
             "kyc_approved", "kyc_rejected", "downtime_alert", "milestone"}


def render(template: str, **context) -> tuple:
    """(html, text) for a template name from TEMPLATES."""
    if template not in TEMPLATES:
        raise KeyError(f"unknown email template {template!r}")
    context.setdefault("support_email", settings.support_email)
    context.setdefault("show_unsubscribe", False)
    context.setdefault("unsubscribe_url", "#")
    html = _env.get_template(f"{template}.html").render(**context)
    text = _env.get_template(f"{template}.txt").render(**context)
    return html, text
