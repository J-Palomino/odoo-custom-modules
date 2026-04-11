{
    "name": "Purchase Enhancements",
    "version": "19.0.7.2.0",
    "category": "Purchase",
    "summary": "Order tracking, packaging, shipping & artwork fields for purchase orders",
    "description": """
        Adds custom fields and views to purchase.order:
        - Order tracking (status, payment, shipment)
        - Packaging tracking tab with artwork & shipping
        - Line-level packaging details (category, brand, size, costs)
        - Grand total including shipping costs
    """,
    "depends": ["purchase", "purchase_stock", "stock", "mail"],
    "data": [
        "security/ir.model.access.csv",
        "data/purchase_data.xml",
        "views/purchase_order_views.xml",
        "views/stock_picking_views.xml",
        "views/bill_of_lading_views.xml",
    ],
    "installable": True,
    "auto_install": False,
    "license": "LGPL-3",
}
