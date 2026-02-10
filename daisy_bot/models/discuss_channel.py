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

    def _notify_thread(self, message, msg_vals=False, **kwargs):
        _logger.info("*** DAISY _notify_thread called channel=%s ***", self.id)
        rdata = super()._notify_thread(message, msg_vals=msg_vals, **kwargs)

        daisy_partner = self.env.ref('daisy_bot.partner_daisy', raise_if_not_found=False)
        if not daisy_partner:
            return rdata

        # Ignore messages from Daisy herself
        author_id = msg_vals.get('author_id') if msg_vals else message.author_id.id
        if author_id == daisy_partner.id:
            return rdata

        # Only respond to user comments, not system notifications
        msg_type = msg_vals.get('message_type', '') if msg_vals else message.message_type
        if msg_type != 'comment':
            return rdata

        # Check if Daisy is a member of this channel
        daisy_is_member = self.env['discuss.channel.member'].search_count([
            ('channel_id', '=', self.id),
            ('partner_id', '=', daisy_partner.id),
        ], limit=1)
        if not daisy_is_member:
            return rdata

        # In group/public channels, only respond when @mentioned
        if self.channel_type not in ('chat', 'group'):
            mentioned_partners = []
            if msg_vals:
                # partner_ids in msg_vals is a list of (4, id) or (6, 0, [ids]) commands
                raw = msg_vals.get('partner_ids', [])
                for cmd in raw:
                    if isinstance(cmd, (list, tuple)):
                        if cmd[0] == 4:
                            mentioned_partners.append(cmd[1])
                        elif cmd[0] == 6:
                            mentioned_partners.extend(cmd[2])
                    elif isinstance(cmd, int):
                        mentioned_partners.append(cmd)
            else:
                mentioned_partners = message.partner_ids.ids

            if daisy_partner.id not in mentioned_partners:
                return rdata

        # Extract clean text from HTML body
        body = msg_vals.get('body', '') if msg_vals else message.body
        clean_text = re.sub(r'<[^>]+>', '', str(body)).strip()
        if not clean_text:
            return rdata

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

        return rdata

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
                )

                _logger.info("Daisy Bot: replied in channel %s (session %s)", channel_id, session_id)

        except requests.Timeout:
            _logger.error("Daisy Bot: API request timed out")
        except requests.RequestException as e:
            _logger.error("Daisy Bot: API request failed: %s", e)
        except Exception:
            _logger.exception("Daisy Bot: unexpected error in async response")
