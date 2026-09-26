from datetime import timedelta
from unittest.mock import MagicMock, patch

from odoo import fields
from odoo.exceptions import AccessError, UserError
from odoo.tests.common import TransactionCase, tagged

from ..models import daisy_workbench as wb_module

SERVICE = "odoo.addons.daisydo_agents.models.daisy_provisioning.DaisyAIServiceProvisioning.create_apikey"


def _fake_create_apikey(self, api_key, key_name):
    return {"id": "key-" + key_name, "apiKey": "sk-daisy-" + key_name, "keyName": key_name}


@tagged("post_install", "-at_install")
class TestDaisyWorkbench(TransactionCase):
    """LGM sign-in for Daisy Workbench: one daisy.plus key per install."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env["ir.config_parameter"].sudo().set_param("daisy.global_api_key", "workspace-test")
        ctx = {"skip_daisy_agent_autocreate": True, "no_reset_password": True}
        Users = cls.env["res.users"].with_context(**ctx)
        cls.employee_user = Users.create({"name": "WB Employee", "login": "wb-employee@test"})
        cls.env["hr.employee"].with_context(**ctx).create(
            {"name": "WB Employee", "user_id": cls.employee_user.id}
        )
        cls.plain_user = Users.create({"name": "WB No Employee", "login": "wb-plain@test"})
        cls.Token = cls.env["daisy.workbench.token"]

    def _issue(self, host="mac-1"):
        code = self.Token._start(self.employee_user, host)
        with patch(SERVICE, _fake_create_apikey):
            return self.Token._redeem(code)

    def test_non_employee_is_refused(self):
        with self.assertRaises(AccessError):
            self.Token._start(self.plain_user, "mac-1")

    def test_code_redeems_once_for_a_named_key(self):
        code = self.Token._start(self.employee_user, "Juan's Mac!")
        with patch(SERVICE, _fake_create_apikey):
            payload = self.Token._redeem(code)
            self.assertIsNone(self.Token._redeem(code), "a code must not redeem twice")
        rec = self.Token.sudo().browse(payload["install_id"])
        self.assertEqual(rec.state, "issued")
        self.assertEqual(rec.hostname, "JuansMac", "hostname is sanitised")
        self.assertEqual(rec.key_name, "workbench:wb-employee@test@JuansMac#%d" % rec.id)
        self.assertTrue(payload["api_key"].startswith("sk-daisy-"))
        self.assertNotEqual(rec.key_hash, payload["api_key"], "only the hash is stored")
        self.assertFalse(rec.code_hash)

    def test_expired_code_is_rejected_and_mints_nothing(self):
        code = self.Token._start(self.employee_user, "mac-1")
        rec = self.Token.sudo().search([("code_hash", "=", wb_module._sha256(code))])
        rec.code_expires = fields.Datetime.now() - timedelta(seconds=1)
        with patch(SERVICE) as create:
            self.assertIsNone(self.Token._redeem(code))
            create.assert_not_called()

    def test_unproven_key_is_never_handed_out(self):
        code = self.Token._start(self.employee_user, "mac-1")
        # create_apikey falls back to the last key in the list when nothing matches.
        other = {"id": "k", "apiKey": "someone-elses-key", "keyName": "claude mcp"}
        with patch(SERVICE, return_value=other), self.assertRaises(UserError):
            self.Token._redeem(code)
        self.assertFalse(self.Token.sudo().search([("state", "=", "issued")]))

    def test_active_install_cap(self):
        self.env["ir.config_parameter"].sudo().set_param("daisy.workbench.max_active", "2")
        self._issue("a")
        self._issue("b")
        with self.assertRaises(UserError):
            self.Token._start(self.employee_user, "c")

    def test_self_revoke_deletes_key_on_daisy(self):
        payload = self._issue()
        with patch.object(wb_module.requests, "delete", return_value=MagicMock(status_code=200)) as delete:
            self.assertTrue(self.Token._revoke_by_key(payload["api_key"]))
            self.assertFalse(self.Token._revoke_by_key(payload["api_key"]), "already revoked")
            self.assertFalse(self.Token._revoke_by_key("not-a-key"))
        delete.assert_called_once()
        self.assertTrue(delete.call_args[0][0].endswith("/apikey/" + "key-" +
                        self.Token.sudo().browse(payload["install_id"]).key_name))
        self.assertEqual(self.Token.sudo().browse(payload["install_id"]).state, "revoked")

    def test_failed_delete_leaves_install_active(self):
        payload = self._issue()
        with patch.object(wb_module.requests, "delete", return_value=MagicMock(status_code=500)):
            with self.assertRaises(UserError):
                self.Token._revoke_by_key(payload["api_key"])
        self.assertEqual(self.Token.sudo().browse(payload["install_id"]).state, "issued")

    def test_deactivating_user_revokes_installs(self):
        payload = self._issue()
        with patch.object(wb_module.requests, "delete", return_value=MagicMock(status_code=204)):
            self.employee_user.active = False
        self.assertEqual(self.Token.sudo().browse(payload["install_id"]).state, "revoked")

    def test_user_cannot_revoke_someone_elses_install(self):
        payload = self._issue()
        rec = self.Token.browse(payload["install_id"]).with_user(self.plain_user)
        with self.assertRaises(AccessError):
            rec.action_revoke()
