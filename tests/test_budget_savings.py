from decimal import Decimal
import unittest
from backend.app.config import Settings
from backend.app.schemas import Budget, BudgetItem, BudgetSavingsPlan, TripRequest, Fact
from backend.app.services.budget_savings import repair_budget, savings_plan


def f(value,source='user:budget-savings-test'):
    return Fact(value=value,source=source,confidence=1)


def example():
    trip=TripRequest(destination=f('Đà Nẵng'),departure=f('Hà Nội'),days=f(3),travelers=f(3),budget=f(10000000),currency=f('VND'))
    def line(key,category,amount,source):
        return BudgetItem(id=key,category=category,description=f(key),amount=f(amount,source),price_kind='estimated')
    budget=Budget(currency=trip.currency,limit=trip.budget,items=[
        line('hotel','accommodation',2400000,'estimate:configuration:live_hotel_vnd_per_room_night'),
        line('return-travel','transport',6000000,'estimate:configuration:round-trip-allocation'),
        line('food','food',1800000,'estimate:food'),line('local','transport',750000,'estimate:local'),
        line('tickets','tickets',1200000,'estimate:tickets'),
        line('contingency','contingency',1215000,'estimate:configuration:contingency-percent-of-subtotal')])
    return trip,budget


class BudgetSavingsTests(unittest.TestCase):
    def test_exact_example_repaired_without_increasing_budget_or_dropping_costs(self):
        trip,budget=example()
        self.assertEqual(budget.total.value,13365000)
        fixed,options=repair_budget(budget,trip,Settings(_env_file=None))
        self.assertEqual(fixed.total.value,9845000)
        self.assertEqual(fixed.limit.value,10000000)
        amounts={item.id:item.amount.value for item in fixed.items}
        self.assertEqual(amounts['hotel'],1600000)  # Two rooms, two nights, no capacity shortcut.
        self.assertEqual(amounts['return-travel'],3600000)
        self.assertEqual(amounts['contingency'],895000)
        self.assertEqual(amounts['food'],1800000)
        self.assertEqual(amounts['tickets'],1200000)
        self.assertEqual(len(options),2)
        self.assertEqual(budget.total.value,13365000)

    def test_verified_or_confirmed_prices_are_never_reduced_in_place(self):
        trip,budget=example()
        for item in budget.items[:2]:
            item.price_kind='sourced'
            item.amount.source='https://supplier.example/confirmed-booking'
        fixed,_=repair_budget(budget,trip)
        self.assertEqual(fixed.total.value,13365000)
        plan=savings_plan(fixed,trip)
        self.assertEqual(plan.current_total,13365000)
        self.assertEqual(plan.proposed_total,9845000)
        self.assertTrue(plan.requires_confirmation)

    def test_constraints_prevent_unapproved_downgrade(self):
        trip,budget=example()
        trip.requirements=[f('Khách sạn 4 sao; đi máy bay')]
        fixed,options=repair_budget(budget,trip)
        self.assertEqual(fixed.total.value,13365000)
        self.assertEqual(options,[])
        plan=savings_plan(fixed,trip)
        self.assertEqual(plan.proposed_total,13365000)
        self.assertTrue(any('vẫn vượt' in note for note in plan.notes))

    def test_savings_report_round_trips_and_reconciles_original_and_current(self):
        trip,budget=example()
        fixed,_=repair_budget(budget,trip)
        plan=savings_plan(fixed,trip,baseline=budget)
        self.assertEqual(plan.initial_total,13365000)
        self.assertEqual(plan.current_total,9845000)
        self.assertEqual(plan.proposed_total,9845000)
        self.assertEqual(sum(option.saving for option in plan.options),Decimal(3200000))
        self.assertEqual(BudgetSavingsPlan.model_validate_json(plan.model_dump_json()),plan)

    def test_foreign_currency_is_not_converted_or_invented(self):
        trip,budget=example()
        trip.currency=f('USD')
        budget.currency=trip.currency
        fixed,options=repair_budget(budget,trip)
        self.assertEqual(fixed.total.value,13365000)
        self.assertEqual(options,[])
