# Over-budget repair and savings alternatives

Reviewer treats `BUDGET_TOTAL` as a blocker. RevisionController also compares
the numeric current total with the confirmed trip budget, so an erroneous PASS
or a human approval cannot skip a real overrun. Budget repairs take priority
over other unresolved issues. Non-budget approval behavior is unchanged.
When review approval is enabled, new approval requests are deferred until the
automatic budget repairs finish or reach the configured limit. Already pending
approvals remain answerable, but approval cannot waive the numeric budget check.

Budget repairs refresh Budget -> Content Editor -> Map Agent -> Reviewer.
Unchanged place photos and itinerary illustrations are retained. The budget
limit, itinerary, sourced tariffs, and user-confirmed amounts are not silently
reduced. Only application-owned estimated hotel and return-travel lines may
switch to the configurable economy assumptions. Room capacity remains two
travelers per room. Explicit high-tier lodging/flight/nonrefundable-booking
requirements prevent incompatible changes. Generated percentage contingency
is recomputed; fixed/user-supplied reserves remain unchanged.

Defaults: 400,000 VND/room/night and 1,200,000 VND/person round trip. These are
unverified comparison targets, not fetched hotel rates or verified transport
fares. Route feasibility, travel time and availability still require confirmation.
The original budget is retained as `budget_baseline`, with a structured
`budget_savings` report. No exchange-rate conversion is invented.

Layout Editor blocks over-budget export before the configured revision limit.
When revisions are exhausted and documented, it exports a clearly explained
draft with a cover notice and a "Phương án tiết kiệm" section. The section
compares original/current/scenario totals, hotel/return-travel alternatives,
remaining excess, and confirmation requirements. It explicitly says when even
the scenario still exceeds budget. Successful estimate repairs retain the
same explanation so lower targets are not mistaken for confirmed bookings.

Example: 13,365,000 VND for three travelers, two rooms/two nights, 10,000,000 VND
limit. Reducing the application-owned hotel estimate from 2,400,000 to 1,600,000
and return-travel estimate from 6,000,000 to 3,600,000, then recalculating the 10%
reserve, yields 9,845,000 VND. This is an economy estimate requiring confirmation.

Validation:

    python -m unittest discover -s tests -p test_budget_savings.py
    python -m unittest discover -s tests -p test_revisions.py
