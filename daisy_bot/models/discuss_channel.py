import logging
import re
import threading

import requests
from markupsafe import Markup

from odoo import api, models, SUPERUSER_ID
import odoo

_logger = logging.getLogger(__name__)
_logger.info("*** DAISY BOT MODULE LOADED ***")


class DiscussChannel(models.Model):
    _inherit = 'discuss.channel'

    def _message_post_after_hook(self, message, msg_vals):
        """Hook after message_post — same pattern as OdooBot."""
        _logger.info("*** DAISY _message_post_after_hook channel=%s ***", self.id)
        self._daisy_handle_message(msg_vals)
        return super()._message_post_after_hook(message, msg_vals)

    def _daisy_handle_message(self, msg_vals):
        """Check if Daisy should respond and spawn async API call."""
        self.ensure_one()

        daisy_partner = self.env.ref('daisy_bot.partner_daisy', raise_if_not_found=False)
        if not daisy_partner:
            _logger.warning("Daisy Bot: partner_daisy not found")
            return

        # Ignore messages from Daisy herself
        author_id = msg_vals.get('author_id')
        if author_id == daisy_partner.id:
            return

        # Only respond to user comments, not system notifications
        if msg_vals.get('message_type') != 'comment':
            return

        # Check if Daisy is a member of this channel
        if daisy_partner.id not in self.channel_member_ids.partner_id.ids:
            return

        # In group/public channels, only respond when @mentioned
        if self.channel_type not in ('chat', 'group'):
            mentioned_partners = []
            raw = msg_vals.get('partner_ids', [])
            for cmd in raw:
                if isinstance(cmd, (list, tuple)):
                    if cmd[0] == 4:
                        mentioned_partners.append(cmd[1])
                    elif cmd[0] == 6:
                        mentioned_partners.extend(cmd[2])
                elif isinstance(cmd, int):
                    mentioned_partners.append(cmd)

            if daisy_partner.id not in mentioned_partners:
                return

        # Extract clean text from HTML body
        body = msg_vals.get('body', '')
        clean_text = re.sub(r'<[^>]+>', '', str(body)).strip()
        if not clean_text:
            return

        _logger.info("Daisy Bot: processing message from author %s: %s", author_id, clean_text[:80])

        # Fire off API call in background thread so we don't block the user
        dbname = self.env.cr.dbname
        channel_id = self.id
        daisy_partner_id = daisy_partner.id

        thread = threading.Thread(
            target=DiscussChannel._daisy_respond_async,
            args=(dbname, channel_id, daisy_partner_id, author_id, clean_text),
            daemon=True,
        )
        thread.start()

    @staticmethod
    def _daisy_respond_async(dbname, channel_id, daisy_partner_id, author_id, question):
        """Call Daisy API and post the response. Runs in a separate thread."""
        try:
            registry = odoo.registry(dbname)
            with registry.cursor() as cr:
                env = api.Environment(cr, SUPERUSER_ID, {})

                ICP = env['ir.config_parameter'].sudo()
                api_url = ICP.get_param('daisy_bot.api_url', '')
                api_key = ICP.get_param('daisy_bot.api_key', '')

                if not api_url or not api_key:
                    _logger.warning("Daisy Bot: api_url or api_key not configured in System Parameters")
                    return

                # Session ID ties conversation memory to this channel + user
                session_id = f"odoo-ch{channel_id}-p{author_id}"

                response = requests.post(
                    api_url,
                    headers={
                        'Content-Type': 'application/json',
                        'Authorization': f'Bearer {api_key}',
                    },
                    json={
                        'question': question,
                        'overrideConfig': {
                            'sessionId': session_id,
                        },
                    },
                    timeout=60,
                )
                response.raise_for_status()
                data = response.json()

                answer = data.get('text', '')
                if not answer:
                    _logger.warning("Daisy Bot: empty 'text' in API response")
                    return

                # Convert newlines to HTML line breaks for Discuss
                html_answer = answer.replace('\n', '<br/>')

                channel = env['discuss.channel'].browse(channel_id)
                channel.with_context(mail_create_nosubscribe=True).message_post(
                    body=Markup(html_answer),
                    message_type='comment',
                    subtype_xmlid='mail.mt_comment',
                    author_id=daisy_partner_id,
                    silent=True,
                )

                _logger.info("Daisy Bot: replied in channel %s (session %s)", channel_id, session_id)

        except requests.Timeout:
            _logger.error("Daisy Bot: API request timed out")
        except requests.RequestException as e:
            _logger.error("Daisy Bot: API request failed: %s", e)
        except Exception:
            _logger.exception("Daisy Bot: unexpected error in async response")
