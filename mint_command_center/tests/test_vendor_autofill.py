from odoo.tests import Form
from odoo.tests.common import TransactionCase


class TestVendorAutofill(TransactionCase):
    """Picking a brand (Vendor Details brand_id or Inclusions brand_ids) —
    or typing a resolvable Vendor / Brand Name — autofills the vendor
    contact details from the brand's portal contact, falling back to the
    latest prior submission for the same brand. Empty fields only; typed
    values are never overwritten."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        Brand = cls.env['mint.brand']
        cls.partner = cls.env['res.partner'].create({
            'name': 'Pat Portal',
            'email': 'pat@contactbrand.example',
            'phone': '+1 480 555 0100',
        })
        cls.brand_with_contact = Brand.create({
            'name': 'AF Contact Brand',
            'vendor_partner_ids': [(6, 0, cls.partner.ids)],
        })
        cls.brand_with_history = Brand.create({
            'name': 'AF History Brand',
            'aliases': 'History Bros',
        })
        cls.prior = cls.env['mint.deal.submission'].create({
            'name': 'AF prior deal',
            'vendor_name': 'Acme Distribution LLC',
            'vendor_contact': 'Harry History',
            'vendor_email': 'harry@acmedist.example',
            'vendor_phone': '+1 480 555 0199',
            'brand_id': cls.brand_with_history.id,
        })

    def _form(self):
        form = Form(self.env['mint.deal.submission'])
        form.name = 'AF test deal'
        return form

    def test_brand_portal_contact_fills_details(self):
        form = self._form()
        form.brand_id = self.brand_with_contact
        self.assertEqual(form.vendor_contact, 'Pat Portal')
        self.assertEqual(form.vendor_email, 'pat@contactbrand.example')
        self.assertEqual(form.vendor_phone, '+1 480 555 0100')
        # No prior submission for this brand -> vendor_name defaults to
        # the brand name itself.
        self.assertEqual(form.vendor_name, 'AF Contact Brand')

    def test_prior_submission_fallback(self):
        form = self._form()
        form.brand_id = self.brand_with_history
        self.assertEqual(form.vendor_name, 'Acme Distribution LLC')
        self.assertEqual(form.vendor_contact, 'Harry History')
        self.assertEqual(form.vendor_email, 'harry@acmedist.example')
        self.assertEqual(form.vendor_phone, '+1 480 555 0199')

    def test_inclusions_multi_brand_picker_fills_details(self):
        form = self._form()
        form.brand_ids.add(self.brand_with_history)
        self.assertEqual(form.vendor_email, 'harry@acmedist.example')
        self.assertEqual(form.vendor_contact, 'Harry History')

    def test_typed_values_never_overwritten(self):
        form = self._form()
        form.vendor_name = 'Hand Typed Vendor'
        form.vendor_email = 'typed@example.com'
        form.brand_id = self.brand_with_history
        self.assertEqual(form.vendor_name, 'Hand Typed Vendor')
        self.assertEqual(form.vendor_email, 'typed@example.com')
        # Still-empty fields do get filled.
        self.assertEqual(form.vendor_contact, 'Harry History')
        self.assertEqual(form.vendor_phone, '+1 480 555 0199')

    def test_vendor_name_resolves_brand_and_fills(self):
        form = self._form()
        # Alias line on the brand ('History Bros') resolves the typed name.
        form.vendor_name = 'History Bros'
        self.assertEqual(form.brand_id, self.brand_with_history)
        self.assertEqual(form.vendor_contact, 'Harry History')
        self.assertEqual(form.vendor_email, 'harry@acmedist.example')

    def test_unresolvable_vendor_name_is_noop(self):
        form = self._form()
        form.vendor_name = 'Totally Unknown Vendor 9000'
        self.assertFalse(form.brand_id)
        self.assertFalse(form.vendor_email)
