from odoo.tests.common import TransactionCase


class MintPtlDealCommon(TransactionCase):
    """Fixtures for PTL-deal targeting tests.

    Builds: one mint.brand, ten product.template records (5 flower, 5 vape),
    each tagged with a `default_code` so excluded_skus tests can target them.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        Brand = cls.env["mint.brand"]
        Category = cls.env["product.category"]
        Template = cls.env["product.template"]

        cls.brand = Brand.create({"name": "FA-93642 Test Brand"})
        cls.other_brand = Brand.create({"name": "FA-93642 Other Brand"})

        # Categories whose names land in MASTER_CATEGORY_PATTERNS buckets.
        cls.cat_flower = Category.create({"name": "Prepack Flower"})
        cls.cat_vape = Category.create({"name": "Cartridge"})

        def _mk(name, brand, cat, code):
            return Template.create({
                "name": name,
                "brand_id": brand.id,
                "categ_id": cat.id,
                "default_code": code,
                "type": "consu",
            })

        cls.flowers = Template
        for i in range(5):
            cls.flowers |= _mk(f"Test Flower {i}", cls.brand, cls.cat_flower, f"FA-FLW-{i}")

        cls.vapes = Template
        for i in range(5):
            cls.vapes |= _mk(f"Test Vape {i}", cls.brand, cls.cat_vape, f"FA-VAP-{i}")

        # Sibling brand's flowers — must never appear in matching results.
        cls.other_flowers = Template
        for i in range(3):
            cls.other_flowers |= _mk(
                f"Other Flower {i}", cls.other_brand, cls.cat_flower, f"FA-OFL-{i}"
            )

        cls.PtlDeal = cls.env["mint.ptl.deal"]
        cls.PtlDay = cls.env["mint.ptl.day"]
