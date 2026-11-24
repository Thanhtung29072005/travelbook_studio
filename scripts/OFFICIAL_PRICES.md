# Official admission prices

Researcher now resolves the official website linked by OpenStreetMap's
`website` / `contact:website` or Wikidata's `P856`. Missing linked websites are
looked up in one bounded Wikidata entity request. No URL is invented by the model.

The service reads public HTML, follows relevant same-site admission-price links,
and asks the existing configured text model to select a verbatim admission-price
excerpt. It does not submit forms, purchase tickets, or bypass authentication.

Acceptance requires a matching place identity, a monetary token occurring in
the excerpt, a single unambiguous VND price, per-person admission scope, and
date evidence for any declared validity period. Discount, child/student,
nationality-specific, group, and bundled prices are excluded from standard
adult pricing. They remain estimated rather than being guessed.

`Place.ticket_quote` and `BudgetItem.price_evidence` retain the actual source URL,
retrieval timestamp, price excerpt, place-name excerpt, validity evidence, and
conditions. Budget applies verified adult prices only to known adults, or
explicitly discloses the adult assumption when ages have not been supplied.
Unknown children's prices remain separate reserves. Known free admission is
preserved as zero; missing data is never converted into free admission.

PDFs show a compact clickable "Nguồn giá đã tra" section after the budget table.
The layout manifest also retains the structured evidence. Reviewer checks
that official-price claims have matching evidence and consistent arithmetic.

Defaults in `.env.example`: enabled, at most 12 places, 4 pages per site,
12 seconds per page, 120 seconds overall, up to 3 simultaneous place lookups.
Successful pages are cached for six hours with their original retrieval time.
The existing `NINE_ROUTER_MODELS` and API credentials are used for extraction.
Model output without source evidence is rejected.

Limits: linked websites may be missing, outdated, inaccessible, require
JavaScript/login/CAPTCHA, or publish tariffs only as scans/PDFs. These cases
retain estimates and an audit warning. This is admission-price discovery, not
a hotel/flight booking API integration. Published prices are a timestamped
snapshot, not a guarantee of future availability or unchanged prices.

Validation:

    python -m unittest discover -s tests -p test_official_prices.py
    python -m unittest discover -s tests -p test_live_budget.py
