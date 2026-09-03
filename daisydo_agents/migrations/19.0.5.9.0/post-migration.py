"""Backfill bot_identity for existing agents.

The new `bot_identity` flag is default-deny: an agent can only auto-reply when it
is True. On upgrade every existing row is False, which would silence the legit
bot agents too. This migration re-blesses ONLY dedicated bot identities and can
never flip a real person's account to True — the rule denies anything with an
interactive SSO login (oauth_uid) or a real human mailbox domain, and only
blesses the synthetic service logins (@daisy.plus, @letsgomint.us).
"""
import logging

from odoo import api, SUPERUSER_ID

_logger = logging.getLogger(__name__)

# Real mailboxes — never auto-blessed (these are people).
HUMAN_DOMAINS = (
    "@letsgomint.com",
    "@brightroot.com",
    "@themintdispensary.com",
    "@on-sitesecurity.co",
    "@gmail.com",
)
# Synthetic, purpose-built bot/service accounts — safe to bless.
BOT_DOMAINS = ("@daisy.plus", "@letsgomint.us")


def _is_bot_identity(user):
    login = (user.login or "").lower()
    if user.oauth_uid:                                  # interactive human SSO
        return False
    if any(login.endswith(d) for d in HUMAN_DOMAINS):   # real mailbox -> a person
        return False
    return any(login.endswith(d) for d in BOT_DOMAINS)


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    agents = env["daisy.agent"].with_context(active_test=False).search(
        [("user_id", "!=", False)]
    )
    to_bless = env["daisy.agent"]
    skipped = []
    for agent in agents:
        if _is_bot_identity(agent.user_id):
            to_bless |= agent
        else:
            skipped.append((agent.name, agent.user_id.login))
    if to_bless:
        to_bless.write({"bot_identity": True})
    _logger.info(
        "bot_identity backfill: blessed %d bot agents; left %d human/ambiguous "
        "agents False (cannot auto-reply as a human). Blessed=%s | Blocked=%s",
        len(to_bless), len(skipped),
        to_bless.mapped("name"), skipped,
    )
