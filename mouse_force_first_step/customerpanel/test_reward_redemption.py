"""Internal foundations only. Test allocation adapters are never web handlers."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from importlib import import_module
from threading import Event
from unittest.mock import patch
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.core import signing
from django.core.exceptions import PermissionDenied
from django.db import IntegrityError, close_old_connections, connection, connections, transaction
from django.db.migrations import AddConstraint, AddField, RunPython
from django.test import SimpleTestCase, TestCase, TransactionTestCase, skipUnlessDBFeature
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from .models import CustomerPoints, Redemption, RedemptionEvent, Reward, RewardCode, RewardFulfillment, RewardRequest
from .points import claim_daily_points, claim_streak_bonus
from .reward_confirmation import (
    CONFIRMATION_SECONDS, TOKEN_SALT, ConfirmationError,
    create_reward_confirmation, read_confirmation_token,
)
from .reward_redemption import AllocationPlan, redeem_reward
from .test_reward_models import make_code, make_redemption, make_reward

MODULE = 'mouse_force_first_step.customerpanel.reward_redemption'
SESSION = 'server-session-for-foundation-tests'
# Keep simulated assignment/refund dates after real model creation defaults.
# A fixed calendar timestamp becomes invalid as the test suite ages.
NOW = timezone.now() + timedelta(days=1)


class TestAllocation(AllocationPlan):
    """Test-only substitute to exercise atomic writes without real fulfillment."""
    __test__ = False

    def __init__(self, reward):
        self.reward = reward
        self.stock_reserved_quantity = int(reward.stock_remaining is not None)

    def reserve(self, redemption):
        if self.stock_reserved_quantity:
            self.reward.stock_remaining -= 1
            self.reward.save(update_fields=['stock_remaining'])


def test_allocation(reward, **kwargs):
    return TestAllocation(reward)


class ConfirmationFixtures:
    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_user(username='confirmation-customer', role='customer')
        cls.other = get_user_model().objects.create_user(username='confirmation-other', role='customer')
        cls.reward = make_reward(is_active=True, fulfillment_type='manual', stock_remaining=3,
                                 max_redemptions_per_customer=None, fulfillment_instructions='We arrange this benefit.')
        CustomerPoints.objects.create(user=cls.user, total_points=260, streak_days=7,
                                      last_daily_claim_date=NOW.date())

    def setUp(self):
        clock = patch('django.utils.timezone.now', return_value=NOW)
        clock.start()
        self.addCleanup(clock.stop)

    def quote(self, reward=None, user=None, session=SESSION):
        return create_reward_confirmation(user or self.user, (reward or self.reward).pk, session_key=session)

    def redeem(self, quote, user=None, session=SESSION):
        return redeem_reward(user or self.user, quote.reward_id, quote.token, session_key=session)

    def assert_error(self, code, function):
        with self.assertRaises(ConfirmationError) as caught:
            function()
        self.assertEqual(caught.exception.code, code)

    def balance(self):
        return CustomerPoints.objects.get(user=self.user).total_points

    def assert_no_spending(self):
        self.assertEqual(self.balance(), 260)
        self.assertFalse(Redemption.objects.exists())
        self.assertFalse(RedemptionEvent.objects.exists())
        self.assertFalse(RewardFulfillment.objects.exists())
        self.assertFalse(RewardRequest.objects.exists())
        self.reward.refresh_from_db()
        self.assertEqual(self.reward.stock_remaining, 3)


class RewardConfirmationTests(ConfirmationFixtures, TestCase):
    def test_valid_quote_is_read_only_and_has_safe_signed_payload(self):
        before = CustomerPoints.objects.values().get(user=self.user)
        with CaptureQueriesContext(connection) as queries:
            quote = self.quote()
        self.assertEqual((quote.points_required, quote.balance, quote.balance_after), (100, 260, 160))
        self.assertEqual(quote.expires_at, NOW + timedelta(minutes=15))
        self.assertEqual(quote.idempotency_key.version, 4)
        self.assertTrue(all(q['sql'].lstrip().upper().startswith('SELECT') for q in queries))
        payload = signing.loads(quote.token, salt=TOKEN_SALT)
        self.assertEqual(set(payload), {'v', 'customer', 'session', 'reward', 'intent', 'balance', 'offer', 'expiry'})
        self.assertNotIn(SESSION, str(payload))
        self.assertNotIn(quote.token, repr(quote))
        self.assertEqual(CustomerPoints.objects.values().get(user=self.user), before)
        self.assertNotEqual(quote.idempotency_key, self.quote().idempotency_key)
        self.assert_no_spending()

    def test_token_tampering_and_malformed_signed_data_are_rejected(self):
        quote = self.quote()
        self.assert_error('invalid_confirmation', lambda: self.redeem(
            type(quote)(**{**quote.__dict__, 'token': quote.token + 'x'})))
        payload = signing.loads(quote.token, salt=TOKEN_SALT)
        for change in ({'balance': True}, {'intent': 'not-a-uuid'}, {'expiry': '2026-09-20'},
                       {'offer': 'bad'}, {'extra': 'unexpected'}, {'v': 2}):
            with self.subTest(change=change):
                token = signing.dumps({**payload, **change}, salt=TOKEN_SALT)
                self.assert_error('invalid_confirmation', lambda: read_confirmation_token(self.user, token, session_key=SESSION))
        self.assert_no_spending()

    def test_confirmation_expires_after_fifteen_minutes(self):
        with patch('django.core.signing.time.time', return_value=1000000):
            quote = self.quote()
        with patch('django.core.signing.time.time', return_value=1000000 + CONFIRMATION_SECONDS):
            self.assertEqual(read_confirmation_token(self.user, quote.token, session_key=SESSION).reward_id, self.reward.pk)
        with patch('django.core.signing.time.time', return_value=1000001 + CONFIRMATION_SECONDS):
            self.assert_error('expired_confirmation', lambda: self.redeem(quote))
        self.assert_no_spending()

    def test_wrong_customer_session_and_route_are_rejected(self):
        quote = self.quote()
        self.assert_error('wrong_confirmation_owner', lambda: self.redeem(quote, user=self.other))
        self.assert_error('wrong_confirmation_owner', lambda: self.redeem(quote, session='a-different-server-session'))
        self.assert_error('invalid_confirmation', lambda: redeem_reward(self.user, uuid4(), quote.token, session_key=SESSION))
        with self.assertRaises(PermissionDenied):
            self.quote(session='')
        self.assert_no_spending()

    def test_password_change_invalidates_previous_session_binding(self):
        quote = self.quote()
        self.user.set_password('new-test-password')
        self.user.save(update_fields=['password'])
        self.assert_error('wrong_confirmation_owner', lambda: self.redeem(quote))

    def test_permissions_are_reread_from_database_for_quote_and_redemption(self):
        quote = self.quote()
        for change in ({'role': 'worker'}, {'is_active': False}):
            with self.subTest(change=change):
                get_user_model().objects.filter(pk=self.user.pk).update(**change)
                for operation in (self.quote, lambda: self.redeem(quote)):
                    with self.assertRaises(PermissionDenied):
                        operation()
                get_user_model().objects.filter(pk=self.user.pk).update(role='customer', is_active=True)
        with self.assertRaises(PermissionDenied):
            self.quote(user=AnonymousUser())
        self.assert_no_spending()

    def test_missing_or_insufficient_points_never_creates_a_balance(self):
        self.assert_error('insufficient_points', lambda: self.quote(user=self.other))
        self.assertFalse(CustomerPoints.objects.filter(user=self.other).exists())
        CustomerPoints.objects.filter(user=self.user).update(total_points=99)
        self.assert_error('insufficient_points', self.quote)
        self.assertEqual(self.balance(), 99)

    def test_publication_dates_and_eligibility_are_checked_again(self):
        changes = ({'is_active': False}, {'valid_from': NOW + timedelta(seconds=1)},
                   {'valid_until': NOW}, {'benefit_valid_until': NOW}, {'access_scope': 'selected_customers'})
        for change in changes:
            with self.subTest(change=change):
                reward = make_reward(is_active=True, fulfillment_type='manual')
                quote = self.quote(reward=reward)
                Reward.objects.filter(pk=reward.pk).update(**change)
                self.assert_error('reward_unavailable', lambda: self.quote(reward=reward))
                self.assert_error('reward_unavailable', lambda: self.redeem(quote))
        self.assert_no_spending()

    def test_selected_customer_and_date_boundaries_are_valid(self):
        reward = make_reward(is_active=True, fulfillment_type='physical', access_scope='selected_customers',
                             valid_from=NOW, valid_until=NOW + timedelta(seconds=1))
        reward.eligible_users.add(self.user)
        self.assertEqual(self.quote(reward=reward).reward_id, reward.pk)

    def test_material_changes_require_a_new_review(self):
        quote = self.quote()
        changes = {'points_required': 120, 'terms': 'New conditions', 'full_description': 'Changed benefit',
                   'fulfillment_instructions': 'New delivery expectation', 'fulfillment_type': 'physical',
                   'partner_name': 'New partner', 'city': 'New city', 'benefit_valid_until': NOW + timedelta(days=3),
                   'valid_until': NOW + timedelta(days=1), 'max_redemptions_per_customer': 2}
        for field, value in changes.items():
            with self.subTest(field=field):
                original = getattr(self.reward, field)
                Reward.objects.filter(pk=self.reward.pk).update(**{field: value})
                self.assert_error('offer_changed', lambda: self.redeem(quote))
                Reward.objects.filter(pk=self.reward.pk).update(**{field: original})
        self.assert_no_spending()

    def test_changed_balance_requires_updated_confirmation(self):
        quote = self.quote()
        CustomerPoints.objects.filter(user=self.user).update(total_points=270)
        self.assert_error('balance_changed', lambda: self.redeem(quote))
        CustomerPoints.objects.filter(user=self.user).update(total_points=90)
        self.assert_error('insufficient_points', lambda: self.redeem(quote))
        self.assertFalse(Redemption.objects.exists())

    def test_every_committed_redemption_counts_including_refunds(self):
        Reward.objects.filter(pk=self.reward.pk).update(max_redemptions_per_customer=1)
        quote = self.quote()
        existing = make_redemption(self.user, self.reward)
        Redemption.objects.filter(pk=existing.pk).update(refunded_points=100, refunded_at=NOW)
        self.assert_error('limit_reached', self.quote)
        self.assert_error('limit_reached', lambda: self.redeem(quote))
        self.assertEqual(self.balance(), 260)

    def test_stock_is_checked_both_at_quote_and_before_allocation(self):
        quote = self.quote()
        Reward.objects.filter(pk=self.reward.pk).update(stock_remaining=0)
        self.assert_error('out_of_stock', self.quote)
        self.assert_error('out_of_stock', lambda: self.redeem(quote))
        self.assertEqual(self.balance(), 260)

    def test_private_inventory_is_only_read_as_availability_metadata(self):
        for kind, payload_kind in [('voucher', 'code'), ('external', 'claim_link')]:
            with self.subTest(kind=kind):
                reward = make_reward(is_active=True, fulfillment_type=kind, information_url='https://example.com/public')
                self.assert_error('out_of_stock', lambda: self.quote(reward=reward))
                code = make_code(reward, payload_kind=payload_kind, expires_at=NOW + timedelta(days=10))
                with CaptureQueriesContext(connection) as queries:
                    quote = self.quote(reward=reward)
                self.assertEqual(quote.promised_expiry, code.expires_at)
                for field in ('encrypted_payload', 'payload_fingerprint', 'encryption_key_id'):
                    self.assertTrue(all(field not in q['sql'] for q in queries))
                self.assertNotIn(code.payload_fingerprint, quote.token)
                self.assertNotIn(str(code.pk), quote.token)
                code.refresh_from_db()
                self.assertIsNone(code.redemption_id)
        self.assert_no_spending()

    def test_inactive_expired_and_assigned_codes_are_not_available(self):
        reward = make_reward(is_active=True)
        make_code(reward, is_active=False)
        make_code(reward, expires_at=NOW)
        existing = make_redemption(self.other, reward)
        make_code(reward, redemption=existing, assigned_at=NOW)
        self.assert_error('out_of_stock', lambda: self.quote(reward=reward))

    def test_inventory_cannot_shorten_the_promised_expiry(self):
        expiry = NOW + timedelta(days=10)
        reward = make_reward(is_active=True)
        code = make_code(reward, expires_at=expiry)
        quote = self.quote(reward=reward)
        RewardCode.objects.filter(pk=code.pk).update(expires_at=NOW + timedelta(days=2))
        self.assert_error('out_of_stock', lambda: self.redeem(quote))
        Reward.objects.filter(pk=reward.pk).update(benefit_valid_until=expiry)
        self.assert_error('out_of_stock', lambda: self.quote(reward=reward))
        RewardCode.objects.filter(pk=code.pk).update(expires_at=None)
        self.assertEqual(self.quote(reward=reward).promised_expiry, expiry)

    def test_undated_inventory_promise_cannot_become_dated(self):
        reward = make_reward(is_active=True)
        code = make_code(reward)
        quote = self.quote(reward=reward)
        self.assertIsNone(quote.promised_expiry)
        RewardCode.objects.filter(pk=code.pk).update(expires_at=NOW + timedelta(days=3))
        self.assert_error('out_of_stock', lambda: self.redeem(quote))

    def test_physical_and_configured_manual_require_valid_fulfillment(self):
        from .reward_fulfillment import FulfillmentInputError
        for kind in ('manual', 'physical'):
            with self.subTest(kind=kind):
                reward = make_reward(is_active=True, fulfillment_type=kind, requires_contact_details=True)
                quote = self.quote(reward=reward)
                with self.assertRaises(FulfillmentInputError):
                    self.redeem(quote)
        self.assert_no_spending()

    def test_existing_customer_reward_routes_remain_read_only(self):
        from .section_test_support import seed_paid_access
        seed_paid_access(self.user)
        self.client.force_login(self.user)
        for url in (reverse('customer_rewards'), reverse('customer_reward_detail', args=[self.reward.pk])):
            self.assertEqual(self.client.post(url, {'token': self.quote().token}).status_code, 405)
        self.assert_no_spending()


class RedemptionFoundationTests(ConfirmationFixtures, TestCase):
    def setUp(self):
        super().setUp()
        adapter = patch(MODULE + '._prepare_allocation', side_effect=test_allocation)
        self.adapter = adapter.start()
        self.addCleanup(adapter.stop)

    def test_internal_transaction_changes_only_total_and_records_snapshots(self):
        before = CustomerPoints.objects.values().get(user=self.user)
        result = self.redeem(self.quote())
        after = CustomerPoints.objects.values().get(user=self.user)
        self.assertEqual(after, {**before, 'total_points': 160})
        record = Redemption.objects.get(pk=result.redemption_id)
        self.assertEqual((record.points_spent, record.balance_after, record.stock_reserved_quantity), (100, 160, 1))
        self.assertEqual(record.snapshot_terms, self.reward.terms)
        self.assertEqual(record.snapshot_fulfillment_instructions, self.reward.fulfillment_instructions)
        self.assertIsNone(record.stock_restored_at)
        self.reward.refresh_from_db()
        self.assertEqual(self.reward.stock_remaining, 2)
        event = RedemptionEvent.objects.get(redemption=record)
        self.assertEqual((event.event_type, event.points_delta, event.balance_after), ('redeemed', -100, 160))
        self.assertFalse(result.replayed)
        self.assertFalse(RewardFulfillment.objects.exists())

    def test_unlimited_stock_records_zero_reserved_units(self):
        Reward.objects.filter(pk=self.reward.pk).update(stock_remaining=None)
        result = self.redeem(self.quote())
        self.assertEqual(Redemption.objects.get(pk=result.redemption_id).stock_reserved_quantity, 0)

    def test_stock_revision_and_cosmetic_changes_do_not_change_material_offer(self):
        quote = self.quote()
        Reward.objects.filter(pk=self.reward.pk).update(stock_remaining=2, revision=3, image_alt='New alt text')
        result = self.redeem(quote)
        self.assertEqual(result.balance_after, 160)
        self.reward.refresh_from_db()
        self.assertEqual(self.reward.stock_remaining, 1)

    def test_idempotent_retry_returns_committed_result_without_second_charge(self):
        quote = self.quote()
        first = self.redeem(quote)
        CustomerPoints.objects.filter(user=self.user).update(total_points=170)
        Reward.objects.filter(pk=self.reward.pk).update(is_active=False, points_required=200)
        second = self.redeem(quote)
        self.assertTrue(second.replayed)
        self.assertEqual(first.redemption_id, second.redemption_id)
        self.assertEqual((second.points_spent, second.balance_after, second.current_balance), (100, 160, 170))
        self.assertEqual(self.adapter.call_count, 1)
        self.assertEqual(Redemption.objects.count(), 1)
        self.assertEqual(RedemptionEvent.objects.count(), 1)

    def test_committed_retry_can_outlive_token_but_cannot_change_session(self):
        with patch('django.core.signing.time.time', return_value=1000000):
            quote = self.quote()
            first = self.redeem(quote)
        with patch('django.core.signing.time.time', return_value=1001000):
            self.assertEqual(self.redeem(quote).redemption_id, first.redemption_id)
            self.assert_error('wrong_confirmation_owner', lambda: self.redeem(quote, session='other-session'))
        self.assertEqual(self.balance(), 160)

    def test_allocation_database_failure_rolls_back_and_retry_reuses_intent(self):
        Reward.objects.filter(pk=self.reward.pk).update(max_redemptions_per_customer=1)
        quote = self.quote()

        def broken_reserve(plan, redemption):
            # Deliberate DB constraint violation AFTER the balance was updated.
            plan.reward.stock_remaining = -1
            plan.reward.save(update_fields=['stock_remaining'])

        with patch.object(TestAllocation, 'reserve', broken_reserve):
            with self.assertRaises(IntegrityError):
                self.redeem(quote)
        self.assert_no_spending()
        self.assertFalse(self.redeem(quote).replayed)
        self.assertEqual(Redemption.objects.count(), 1)

    def test_audit_database_failure_rolls_back_balance_and_stock(self):
        real_create = RedemptionEvent.objects.create

        def invalid_event(**kwargs):
            return real_create(**{**kwargs, 'points_delta': 0})

        with patch.object(RedemptionEvent.objects, 'create', side_effect=invalid_event):
            with self.assertRaises(IntegrityError):
                self.redeem(self.quote())
        self.assert_no_spending()

    def test_outer_transaction_rollback_undoes_internal_result(self):
        quote = self.quote()
        with self.assertRaises(RuntimeError):
            with transaction.atomic():
                self.redeem(quote)
                raise RuntimeError('Abort outer operation')
        self.assert_no_spending()

    def test_invalid_internal_stock_plan_cannot_spend(self):
        plan = TestAllocation(self.reward)
        plan.stock_reserved_quantity = 2
        with patch(MODULE + '._prepare_allocation', return_value=plan):
            with self.assertRaises(ValueError):
                self.redeem(self.quote())
        self.assert_no_spending()


class RedemptionStockTests(ConfirmationFixtures, TestCase):
    def rejected(self, operation):
        with self.assertRaises(IntegrityError), transaction.atomic():
            operation()

    def test_only_zero_or_one_reserved_unit_is_valid(self):
        for quantity in (-1, 2):
            self.rejected(lambda: make_redemption(self.user, self.reward, stock_reserved_quantity=quantity))
        for quantity in (0, 1):
            record = make_redemption(self.user, self.reward, stock_reserved_quantity=quantity)
            self.assertEqual(record.stock_reserved_quantity, quantity)
        voucher = make_reward()
        self.rejected(lambda: make_redemption(self.user, voucher, stock_reserved_quantity=1))

    def test_reserved_quantity_cannot_be_changed_by_orm_or_sql(self):
        record = make_redemption(self.user, self.reward, stock_reserved_quantity=1)
        self.rejected(lambda: Redemption.objects.filter(pk=record.pk).update(stock_reserved_quantity=0))

        def raw_change():
            with connection.cursor() as cursor:
                cursor.execute('UPDATE customerpanel_redemption SET stock_reserved_quantity = 0')

        self.rejected(raw_change)
        Redemption.objects.filter(pk=record.pk).update(status='processing')
        record.refresh_from_db()
        self.assertEqual(record.stock_reserved_quantity, 1)

    def test_restoration_requires_reserved_stock_and_full_refund_in_date_order(self):
        record = make_redemption(self.user, self.reward, stock_reserved_quantity=1)
        rows = Redemption.objects.filter(pk=record.pk)
        self.rejected(lambda: rows.update(stock_restored_at=NOW))
        self.rejected(lambda: rows.update(refunded_points=50, refunded_at=NOW, stock_restored_at=NOW))
        self.rejected(lambda: rows.update(refunded_points=100, refunded_at=NOW, stock_restored_at=NOW - timedelta(seconds=1)))
        no_stock = make_redemption(self.user, self.reward)
        self.rejected(lambda: Redemption.objects.filter(pk=no_stock.pk).update(
            refunded_points=100, refunded_at=NOW, stock_restored_at=NOW))
        rows.update(refunded_points=100, refunded_at=NOW, stock_restored_at=NOW)
        record.refresh_from_db()
        self.assertEqual(record.stock_restored_at, NOW)
        # Schema setup is not a refund/restock action: balances and stock stay put.
        self.assertEqual(self.balance(), 260)
        self.reward.refresh_from_db()
        self.assertEqual(self.reward.stock_remaining, 3)

    def test_recorded_restoration_cannot_be_cleared_or_moved(self):
        record = make_redemption(self.user, self.reward, stock_reserved_quantity=1,
                                 refunded_points=100, refunded_at=NOW, stock_restored_at=NOW)
        for value in (None, NOW + timedelta(seconds=1)):
            self.rejected(lambda: Redemption.objects.filter(pk=record.pk).update(stock_restored_at=value))


class StockMigrationScopeTests(SimpleTestCase):
    def test_migration_only_adds_approved_redemption_fields_and_guards(self):
        migration = import_module('mouse_force_first_step.customerpanel.migrations.0007_redemption_stock_tracking').Migration
        self.assertIn(('customerpanel', '0006_rewards_database'), migration.dependencies)
        fields = []
        for operation in migration.operations:
            self.assertIsInstance(operation, (AddField, AddConstraint, RunPython))
            if isinstance(operation, (AddField, AddConstraint)):
                self.assertEqual(operation.model_name, 'redemption')
            if isinstance(operation, AddField):
                fields.append(operation.name)
        self.assertCountEqual(fields, ['stock_reserved_quantity', 'stock_restored_at'])


@skipUnlessDBFeature('has_select_for_update')
class RedemptionConcurrencyTests(ConfirmationFixtures, TransactionTestCase):
    def setUp(self):
        self.setUpTestData()
        super().setUp()
        adapter = patch(MODULE + '._prepare_allocation', side_effect=test_allocation)
        adapter.start()
        self.addCleanup(adapter.stop)

    def worker(self, operation, attempted):
        close_old_connections()

        def track_lock(execute, sql, params, many, context):
            if 'FOR UPDATE' in sql and get_user_model()._meta.db_table in sql:
                attempted.set()
            return execute(sql, params, many, context)

        try:
            with connection.cursor() as cursor:
                cursor.execute("SET lock_timeout = '10s'")
            with connection.execute_wrapper(track_lock):
                try:
                    return operation()
                except ConfirmationError as error:
                    return error.code
        finally:
            connections.close_all()

    def simultaneous(self, operations):
        attempted = [Event() for _ in operations]
        with ThreadPoolExecutor(max_workers=len(operations)) as executor:
            with transaction.atomic():
                get_user_model().objects.select_for_update().get(pk=self.user.pk)
                futures = [executor.submit(self.worker, operation, signal) for operation, signal in zip(operations, attempted)]
                for signal in attempted:
                    self.assertTrue(signal.wait(10), 'Did not attempt common User lock')
                self.assertTrue(all(not future.done() for future in futures))
            return [future.result(timeout=20) for future in futures]

    def test_lock_order_is_user_points_reward_redemption(self):
        quote = self.quote()
        with CaptureQueriesContext(connection) as queries:
            self.redeem(quote)
        locked = [q['sql'] for q in queries if 'FOR UPDATE' in q['sql']]
        expected = [get_user_model()._meta.db_table, CustomerPoints._meta.db_table,
                    Reward._meta.db_table, Redemption._meta.db_table]
        self.assertEqual(len(locked), len(expected))
        for query, table in zip(locked, expected):
            self.assertIn('FROM "' + table + '"', query)

    def test_same_intent_on_two_connections_charges_once(self):
        quote = self.quote()
        results = self.simultaneous([lambda: self.redeem(quote), lambda: self.redeem(quote)])
        self.assertCountEqual([r.replayed for r in results], [False, True])
        self.assertEqual(results[0].redemption_id, results[1].redemption_id)
        self.assertEqual(self.balance(), 160)
        self.assertEqual(Redemption.objects.count(), 1)
        self.assertEqual(RedemptionEvent.objects.count(), 1)
        self.reward.refresh_from_db()
        self.assertEqual(self.reward.stock_remaining, 2)

    def test_distinct_intents_cannot_overspend_shared_points(self):
        CustomerPoints.objects.filter(user=self.user).update(total_points=160)
        first, second = self.quote(), self.quote()
        results = self.simultaneous([lambda: self.redeem(first), lambda: self.redeem(second)])
        self.assertEqual(results.count('insufficient_points'), 1)
        self.assertEqual(self.balance(), 60)
        self.assertEqual(Redemption.objects.count(), 1)

    def test_limit_is_enforced_again_under_common_user_lock(self):
        Reward.objects.filter(pk=self.reward.pk).update(max_redemptions_per_customer=1)
        first, second = self.quote(), self.quote()
        results = self.simultaneous([lambda: self.redeem(first), lambda: self.redeem(second)])
        self.assertEqual(results.count('limit_reached'), 1)
        self.assertEqual(self.balance(), 160)
        self.assertEqual(Redemption.objects.count(), 1)

    def assert_points_first(self, operation, increment):
        quote = self.quote()
        attempted = Event()
        with ThreadPoolExecutor(max_workers=1) as executor:
            with transaction.atomic():
                get_user_model().objects.select_for_update().get(pk=self.user.pk)
                future = executor.submit(self.worker, lambda: self.redeem(quote), attempted)
                self.assertTrue(attempted.wait(10))
                self.assertFalse(future.done())
                operation(self.user)
            self.assertEqual(future.result(timeout=20), 'balance_changed')
        self.assertEqual(self.balance(), 260 + increment)
        self.assertFalse(Redemption.objects.exists())

    def test_daily_claim_first_invalidates_stale_projected_balance(self):
        CustomerPoints.objects.filter(user=self.user).update(streak_days=3, last_daily_claim_date=NOW.date() - timedelta(days=1))
        self.assert_points_first(claim_daily_points, 10)
        self.assertEqual(CustomerPoints.objects.get(user=self.user).streak_days, 4)

    def test_bonus_claim_first_invalidates_stale_projected_balance(self):
        self.assert_points_first(claim_streak_bonus, 35)
        self.assertTrue(CustomerPoints.objects.get(user=self.user).day_7_bonus_awarded)

    def test_waiting_bonus_claim_reads_committed_deduction(self):
        quote = self.quote()
        attempted = Event()
        with ThreadPoolExecutor(max_workers=1) as executor:
            with transaction.atomic():
                self.redeem(quote)
                future = executor.submit(self.worker, lambda: claim_streak_bonus(self.user), attempted)
                self.assertTrue(attempted.wait(10))
                self.assertFalse(future.done())
            self.assertEqual(future.result(timeout=20).awarded_amount, 35)
        points = CustomerPoints.objects.get(user=self.user)
        self.assertEqual((points.total_points, points.streak_days, points.last_daily_claim_date), (195, 7, NOW.date()))
        self.assertTrue(points.day_7_bonus_awarded)
