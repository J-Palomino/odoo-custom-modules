{
    'name': 'Mint Markov — Shopping & Buying Patterns',
    'version': '19.0.1.0.0',
    'category': 'Tools/Analytics',
    'summary': 'Catalogs shopping-intent (PostHog) and buying (Dutchie/POS) '
               'transition matrices as time-stamped Markov snapshots.',
    'description': """
Mint Markov
===========

Builds a first-order Markov catalog of how customers move through the funnel,
stitched across two data sources at the Dutchie handoff boundary:

* **intent** phase — storefront shopping events pulled from PostHog
  (page_viewed -> products_viewed -> add_to_cart -> ... -> handoff).
  Requires a PostHog personal API key with query scope; if absent, this phase
  is skipped and only the buying phase is recorded.
* **buying** phase — realized order outcomes computed locally from
  ``mint.pos.order`` (order_placed -> completed / cancelled / returned),
  filtered to online order sources.
* **bridge** — aggregate P(order | handoff) joining the two at the window level.

An ``ir.cron`` writes one ``mint.markov.snapshot`` per run so the chain is
queryable over time. Read-only catalog; no customer PII is stored.
""",
    'author': 'Mint Dispensaries',
    'website': 'https://mintdispensaries.com',
    'license': 'LGPL-3',
    'depends': ['base', 'mint_pos_bridge'],
    'data': [
        'security/ir.model.access.csv',
        'data/ir_cron.xml',
        'views/markov_views.xml',
    ],
    'installable': True,
    'application': False,
    'auto_install': False,
}
