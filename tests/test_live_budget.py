from datetime import date, time
from decimal import Decimal
import unittest

from backend.app.config import Settings
from backend.app.live_workflow import LiveTravelBookWorkflow
from backend.app.agents.budget import BudgetAgent
from backend.app.schemas import TripRequest, Place, DayPlan, PlanStop, Fact, BudgetItem, AdmissionQuote


def fact(value):
    return Fact(value=value, source='user:test-budget', confidence=1)


class LiveBudgetTests(unittest.TestCase):
    def setUp(self):
        self.workflow = object.__new__(LiveTravelBookWorkflow)
        self.workflow.settings = Settings(_env_file=None)
        self.trip = TripRequest(destination=fact('Huế'), departure=fact('Hà Nội'), days=fact(3),
            travelers=fact(2), budget=fact(10000000), currency=fact('VND'))
        self.state = {'places': [Place(id='p', name=fact('Điểm tham quan'), latitude=fact(16), longitude=fact(107))],
            'itinerary': [DayPlan(day_number=i, stops=[PlanStop(place_id='p', arrival=fact(time(9)),
                departure=fact(time(10)), travel_minutes=fact(0))]) for i in range(1, 4)]}

    def test_complete_group_allocation_and_contingency(self):
        items, warnings = self.workflow.costs(self.state, self.trip)
        amounts = {item.id: item.amount.value for item in items}
        self.assertEqual(amounts['food-1'], 400000)
        self.assertEqual(amounts['hotel'], 1200000)
        self.assertEqual(amounts['return-travel'], 4000000)
        self.assertEqual(amounts['ticket-reserve-1-p'], 100000)
        self.assertEqual(amounts['contingency'], 745000)
        result = BudgetAgent(None, '.').calculate(self.trip, items)
        self.assertEqual(result.budget.total.value, Decimal('8195000'))
        self.assertTrue(all(item.price_kind == 'estimated' for item in items))
        self.assertTrue(any('tạm dự trù' in warning.value for warning in warnings))

    def test_large_group_scales_rooms_and_vehicles(self):
        trip = self.trip.model_copy(update={'travelers': fact(5)})
        items, _ = self.workflow.costs(self.state, trip)
        amounts = {item.id: item.amount.value for item in items}
        self.assertEqual(amounts['hotel'], 3600000)
        self.assertEqual(amounts['transport-1'], 500000)

    def test_missing_departure_is_disclosed_and_free_admission_stays_zero(self):
        self.state['places'][0].ticket_price = fact(0)
        self.state['places'][0].currency = fact('VND')
        items, warnings = self.workflow.costs(self.state, self.trip.model_copy(update={'departure': None}))
        self.assertNotIn('return-travel', {item.id for item in items})
        self.assertEqual(next(item for item in items if item.category == 'tickets').amount.value, 0)
        self.assertTrue(any('Chưa có nơi khởi hành' in warning.value for warning in warnings))

    def test_user_category_total_is_preserved_without_dropping_other_costs(self):
        self.state['cost_items'] = [BudgetItem(id='user-food', category='food', description=fact('Ăn uống cả nhóm'),
            amount=fact(900000), price_kind='estimated').model_dump(mode='json')]
        items, _ = self.workflow.costs(self.state, self.trip)
        self.assertEqual(sum(item.amount.value for item in items if item.category == 'food'), 900000)
        self.assertIn('return-travel', {item.id for item in items})

    def test_non_vnd_does_not_invent_currency_conversion(self):
        items, warnings = self.workflow.costs(self.state, self.trip.model_copy(update={'currency': fact('USD')}))
        self.assertFalse(items)
        self.assertTrue(any(w.source == 'policy:no-implicit-currency-conversion' for w in warnings))

    def set_quote(self, **changes):
        self.state['places'][0].ticket_quote = AdmissionQuote.model_validate({
            'amount': '150000', 'currency': 'VND', 'audience': 'adult',
            'source_url': 'https://museum.example/gia-ve', 'retrieved_at': '2026-11-23T12:00:00+00:00',
            'excerpt': 'Vé người lớn: 150.000 đồng/người', 'conditions': 'Vé phổ thông', **changes})

    def test_official_price_used_and_provenance_retained(self):
        self.set_quote()
        items, _ = self.workflow.costs(self.state, self.trip)
        ticket = next(item for item in items if item.id == 'ticket-1-p')
        self.assertEqual(ticket.amount.value, 300000)
        self.assertEqual(ticket.price_kind, 'sourced')
        self.assertEqual(str(ticket.price_evidence.source_url), 'https://museum.example/gia-ve')
        self.assertNotIn('ticket-reserve-1-p', {item.id for item in items})

    def test_child_does_not_receive_adult_price(self):
        self.set_quote()
        trip = self.trip.model_copy(update={'traveler_ages': [fact(30), fact(10)]})
        items, _ = self.workflow.costs(self.state, trip)
        self.assertEqual(next(item.amount.value for item in items if item.id == 'ticket-1-p'), 150000)
        self.assertEqual(next(item.amount.value for item in items if item.id == 'ticket-reserve-1-p'), 50000)

    def test_expired_quote_does_not_become_current_sourced_price(self):
        self.set_quote(valid_until='2026-12-24')
        trip = self.trip.model_copy(update={'start_date': fact(date(2026, 12, 23))})
        items, _ = self.workflow.costs(self.state, trip)
        self.assertIn('ticket-1-p', {item.id for item in items})
        self.assertNotIn('ticket-3-p', {item.id for item in items})
        self.assertIn('ticket-reserve-3-p', {item.id for item in items})
