"""Jinja2-rendered email templates (templates/emails/*.html + .txt).

Every template receives ``app_name`` (APP_NAME) and ``support_email``
(SUPPORT_EMAIL) from configuration. Links in templates must be built with
``api.utils.links.build_app_link``; no template may contain a placeholder
("#") link.
"""
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
             "kyc_approved", "kyc_rejected", "downtime_alert", "milestone", "low_balance", "unified_account"}


def render(template: str, **context) -> tuple:
    """(html, text) for a template name from TEMPLATES."""
    if template not in TEMPLATES:
        raise KeyError(f"unknown email template {template!r}")
    context.setdefault("app_name", settings.app_name)
    context.setdefault("support_email", settings.support_email)
    html = _env.get_template(f"{template}.html").render(**context)
    text = _env.get_template(f"{template}.txt").render(**context)
    return html, text
