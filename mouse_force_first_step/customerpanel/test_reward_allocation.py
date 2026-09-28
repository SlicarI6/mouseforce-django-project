"""Real internal digital allocation; no customer endpoints or production data."""
import base64
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import json
import logging
import os
from threading import Event
from unittest.mock import patch
from uuid import uuid4

from cryptography.fernet import Fernet
from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.core.exceptions import PermissionDenied
from django.db import IntegrityError, close_old_connections, connection, connections, transaction
from django.test import TestCase, TransactionTestCase, override_settings, skipUnlessDBFeature
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from .models import CustomerPoints, Redemption, RedemptionEvent, Reward, RewardCode, RewardFulfillment, RewardRequest
from .points import claim_daily_points, claim_streak_bonus
from .reward_confirmation import ConfirmationError, create_reward_confirmation
from .reward_inventory import import_private_inventory
from .reward_private_access import PrivateBenefitUnavailable, get_private_reward_benefit
from .reward_redemption import PrivateCodeAllocation, redeem_reward
from .test_reward_models import make_reward
from .test_reward_redemption import SESSION

NOW = timezone.now() + timedelta(days=1)

PRIVATE_MODULE = 'mouse_force_first_step.customerpanel.reward_private_access'
TEST_KEYS = {
    'REWARDS_CODE_ENCRYPTION_KEY': Fernet.generate_key().decode(),
    'REWARDS_CODE_FINGERPRINT_KEY': base64.urlsafe_b64encode(os.urandom(32)).decode(),
    'REWARDS_CODE_KEY_ID': 'test-digital-v1',
}


class DigitalFixtures:
    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_user(username='digital-owner', role='customer')
        cls.other = get_user_model().objects.create_user(username='digital-other', role='customer')
        cls.staff = get_user_model().objects.create_user(username='digital-staff', role='customer', is_staff=True, is_superuser=True)
        cls.reward = make_reward(is_active=True, max_redemptions_per_customer=None)
        for user in (cls.user, cls.other):
            CustomerPoints.objects.create(user=user, total_points=260, streak_days=7, last_daily_claim_date=NOW.date())

    def setUp(self):
        clock = patch('django.utils.timezone.now', return_value=NOW)
        clock.start()
        self.addCleanup(clock.stop)

    def inventory(self, reward=None, value=None, expires_at=None, is_active=True):
        reward = reward or self.reward
        value = value or ('https://partner.example/claim/' + uuid4().hex if reward.fulfillment_type == 'external' else 'PRIVATE-' + uuid4().hex)
        _, codes = import_private_inventory(self.staff, reward.pk, [value], expires_at=expires_at, is_active=is_active)
        return codes[0], value

    def quote(self, reward=None, user=None):
        return create_reward_confirmation(user or self.user, (reward or self.reward).pk, session_key=SESSION)

    def redeem(self, quote, user=None):
        return redeem_reward(user or self.user, quote.reward_id, quote.token, session_key=SESSION)

    def balance(self, user=None):
        return CustomerPoints.objects.get(user=user or self.user).total_points

    def assert_error(self, expected, operation):
        with self.assertRaises(ConfirmationError) as caught:
            operation()
        self.assertEqual(caught.exception.code, expected)

    def assert_unspent(self):
        self.assertEqual(self.balance(), 260)
        self.assertFalse(Redemption.objects.exists())
        self.assertFalse(RedemptionEvent.objects.exists())
        self.assertFalse(RewardCode.objects.filter(redemption__isnull=False).exists())


@override_settings(**TEST_KEYS)
class DigitalAllocationTests(DigitalFixtures, TestCase):
    def test_voucher_uses_earliest_suitable_expiry_without_decrypting(self):
        undated, _ = self.inventory()
        later, _ = self.inventory(expires_at=NOW + timedelta(days=10))
        earliest, secret = self.inventory(expires_at=NOW + timedelta(days=2))
        before = CustomerPoints.objects.values().get(user=self.user)
        ciphertext = bytes(earliest.encrypted_payload)
        with patch.object(Fernet, 'decrypt', side_effect=AssertionError('Pre-commit decryption')), CaptureQueriesContext(connection) as queries:
            result = self.redeem(self.quote())
        record = Redemption.objects.get(pk=result.redemption_id)
        self.assertEqual(record.private_code.pk, earliest.pk)
        self.assertEqual((record.status, record.completed_at, record.snapshot_benefit_valid_until), ('fulfilled', NOW, earliest.expires_at))
        self.assertEqual(record.stock_reserved_quantity, 0)
        self.assertEqual(CustomerPoints.objects.values().get(user=self.user), {**before, 'total_points': 160})
        earliest.refresh_from_db()
        self.assertEqual(bytes(earliest.encrypted_payload), ciphertext)
        self.assertNotIn(secret.encode(), ciphertext)
        self.assertNotIn(secret, repr(result))
        self.assertTrue(all('encrypted_payload' not in q['sql'] and 'payload_fingerprint' not in q['sql'] for q in queries))
        for unused in (later, undated):
            unused.refresh_from_db()
            self.assertIsNone(unused.redemption_id)
        self.assertFalse(RewardFulfillment.objects.exists())
        self.assertFalse(RewardRequest.objects.exists())

    def test_equal_expiry_uses_creation_time_then_uuid_deterministically(self):
        first, _ = self.inventory(expires_at=NOW + timedelta(days=2))
        second, _ = self.inventory(expires_at=first.expires_at)
        # UUID breaks ties only after expiry AND creation time are equal.
        RewardCode.objects.filter(pk__in=[first.pk, second.pk]).update(created_at=NOW)
        result = self.redeem(self.quote())
        self.assertEqual(Redemption.objects.get(pk=result.redemption_id).private_code.pk, min(first.pk, second.pk))

    def test_external_requires_private_claim_link_inventory(self):
        reward = make_reward(is_active=True, fulfillment_type='external', information_url='https://partner.example/public')
        self.assert_error('out_of_stock', lambda: self.quote(reward=reward))
        code, secret = self.inventory(reward)
        result = self.redeem(self.quote(reward=reward))
        record = Redemption.objects.get(pk=result.redemption_id)
        self.assertEqual(record.private_code.pk, code.pk)
        self.assertEqual(record.private_code.payload_kind, 'claim_link')
        self.assertEqual(record.snapshot_information_url, reward.information_url)
        self.assertNotEqual(secret, reward.information_url)
        self.assertNotIn(secret, repr(result))

    def test_inactive_expired_and_other_reward_codes_are_skipped(self):
        disabled, _ = self.inventory(is_active=False)
        expired, _ = self.inventory()
        RewardCode.objects.filter(pk=expired.pk).update(expires_at=NOW)
        other_reward = make_reward(is_active=True, fulfillment_type='external')
        other_code, _ = self.inventory(other_reward)
        valid, _ = self.inventory()
        result = self.redeem(self.quote())
        self.assertEqual(Redemption.objects.get(pk=result.redemption_id).private_code.pk, valid.pk)
        for code in (disabled, expired, other_code):
            code.refresh_from_db()
            self.assertIsNone(code.redemption_id)

    def test_incompatible_inventory_cannot_be_saved_or_allocated(self):
        code, _ = self.inventory()
        with self.assertRaises(IntegrityError), transaction.atomic():
            RewardCode.objects.filter(pk=code.pk).update(payload_kind='claim_link')
        code.refresh_from_db()
        self.assertEqual(code.payload_kind, 'code')
        result = self.redeem(self.quote())
        self.assertEqual(Redemption.objects.get(pk=result.redemption_id).private_code.pk, code.pk)

    def test_finite_inventory_must_cover_confirmed_benefit_expiry(self):
        required = NOW + timedelta(days=10)
        Reward.objects.filter(pk=self.reward.pk).update(benefit_valid_until=required)
        shorter, _ = self.inventory(expires_at=NOW + timedelta(days=2))
        suitable, _ = self.inventory(expires_at=required)
        result = self.redeem(self.quote())
        self.assertEqual(Redemption.objects.get(pk=result.redemption_id).private_code.pk, suitable.pk)
        shorter.refresh_from_db()
        self.assertIsNone(shorter.redemption_id)

    def test_assigned_code_is_not_reused_by_a_new_confirmation(self):
        first, _ = self.inventory()
        result = self.redeem(self.quote())
        self.assert_error('out_of_stock', self.quote)
        second, _ = self.inventory()
        next_result = self.redeem(self.quote())
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(first.redemption_id, result.redemption_id)
        self.assertEqual(second.redemption_id, next_result.redemption_id)
        self.assertEqual(self.balance(), 60)

    def test_same_confirmation_retry_does_not_assign_another_code(self):
        self.inventory()
        self.inventory()
        quote = self.quote()
        first = self.redeem(quote)
        second = self.redeem(quote)
        self.assertTrue(second.replayed)
        self.assertEqual(first.redemption_id, second.redemption_id)
        self.assertEqual(self.balance(), 160)
        self.assertEqual(RewardCode.objects.filter(redemption__isnull=False).count(), 1)
        self.assertEqual(RedemptionEvent.objects.filter(event_type='redeemed').count(), 1)

    def test_post_commit_replay_survives_expired_token_and_changed_reward(self):
        self.inventory()
        with patch('django.core.signing.time.time', return_value=1000000):
            quote = self.quote()
            first = self.redeem(quote)
        Reward.objects.filter(pk=self.reward.pk).update(is_active=False, points_required=500)
        with patch('django.core.signing.time.time', return_value=1001000):
            replay = self.redeem(quote)
        self.assertEqual(replay.redemption_id, first.redemption_id)
        self.assertTrue(replay.replayed)
        self.assertEqual(self.balance(), 160)

    def test_missing_key_configuration_and_unknown_key_id_cannot_charge(self):
        code, _ = self.inventory()
        quote = self.quote()
        with override_settings(REWARDS_CODE_ENCRYPTION_KEY=''):
            self.assert_error('inventory_unavailable', lambda: self.redeem(quote))
        RewardCode.objects.filter(pk=code.pk).update(encryption_key_id='unconfigured-key')
        self.assert_error('out_of_stock', lambda: self.redeem(quote))
        self.assert_unspent()

    def test_failure_after_points_deduction_before_assignment_rolls_back(self):
        self.inventory()
        quote = self.quote()

        def fail_allocation(plan, redemption):
            self.assertEqual(self.balance(), 160)
            raise IntegrityError('Test allocation failure')

        with patch.object(PrivateCodeAllocation, 'reserve', fail_allocation):
            with self.assertRaises(IntegrityError):
                self.redeem(quote)
        self.assert_unspent()
        self.assertFalse(self.redeem(quote).replayed)

    def test_assignment_database_failure_rolls_back_entire_transaction(self):
        self.inventory()
        with patch.object(RewardCode, 'save', side_effect=IntegrityError('Test assignment failure')):
            with self.assertRaises(IntegrityError):
                self.redeem(self.quote())
        self.assert_unspent()

    def test_audit_failure_rolls_back_assignment_and_charge(self):
        self.inventory()
        with patch.object(RedemptionEvent.objects, 'create', side_effect=IntegrityError('Test audit failure')):
            with self.assertRaises(IntegrityError):
                self.redeem(self.quote())
        self.assert_unspent()

    def test_expiry_after_row_lock_wait_cannot_assign(self):
        self.inventory(expires_at=NOW + timedelta(seconds=1))
        quote = self.quote()
        # Service eligibility, candidate query and assignment clock respectively.
        with patch('django.utils.timezone.now', side_effect=[NOW, NOW, NOW, NOW + timedelta(seconds=2)]):
            self.assert_error('out_of_stock', lambda: self.redeem(quote))
        self.assert_unspent()

    def test_nested_redemption_never_reveals_and_outer_rollback_unassigns(self):
        self.inventory()
        quote = self.quote()
        with self.assertRaises(RuntimeError):
            with transaction.atomic():
                result = self.redeem(quote)
                with patch(PRIVATE_MODULE + '._decode_benefit') as decode:
                    with self.assertRaises(PrivateBenefitUnavailable):
                        get_private_reward_benefit(self.user, result.redemption_id)
                    decode.assert_not_called()
                raise RuntimeError('Test outer rollback')
        self.assert_unspent()

    def test_catalogue_detail_and_web_actions_never_reveal_or_redeem(self):
        from .section_test_support import seed_paid_access
        seed_paid_access(self.user)
        secrets = []
        rewards = [self.reward, make_reward(is_active=True, fulfillment_type='external')]
        for reward in rewards:
            _, secret = self.inventory(reward)
            secrets.append(secret)
            self.redeem(self.quote(reward=reward))
        self.client.force_login(self.user)
        for url in [reverse('customer_rewards'), *[reverse('customer_reward_detail', args=[r.pk]) for r in rewards]]:
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200)
            for secret in secrets:
                self.assertNotContains(response, secret)
            for field in ('encrypted_payload', 'payload_fingerprint', 'encryption_key_id'):
                self.assertNotContains(response, field)
            self.assertEqual(self.client.post(url).status_code, 405)
        self.assertEqual(self.balance(), 60)


@skipUnlessDBFeature('has_select_for_update')
@override_settings(**TEST_KEYS)
class DigitalCommitAndConcurrencyTests(DigitalFixtures, TransactionTestCase):
    # Limit fixture setup/flush to these apps; production schema is still fully
    # migrated. This avoids unrelated app post_migrate work for each race test.
    available_apps = ['django.contrib.auth', 'django.contrib.contenttypes',
                      'mouse_force_first_step.accounts', 'mouse_force_first_step.customerpanel']

    def setUp(self):
        self.setUpTestData()
        super().setUp()

    def worker(self, operation, attempted, table):
        close_old_connections()

        def track_lock(execute, sql, params, many, context):
            if 'FOR UPDATE' in sql and ('FROM "' + table + '"') in sql:
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

    def simultaneous(self, operations, *, reward_lock=False):
        attempted = [Event() for _ in operations]
        model = Reward if reward_lock else get_user_model()
        pk = self.reward.pk if reward_lock else self.user.pk
        with ThreadPoolExecutor(max_workers=len(operations)) as pool:
            with transaction.atomic():
                model.objects.select_for_update().get(pk=pk)
                futures = [pool.submit(self.worker, op, event, model._meta.db_table) for op, event in zip(operations, attempted)]
                for event in attempted:
                    self.assertTrue(event.wait(10), 'Expected row lock was not attempted')
                self.assertTrue(all(not future.done() for future in futures))
            return [future.result(timeout=20) for future in futures]

    def test_two_customers_competing_for_last_voucher_only_charge_winner(self):
        code, _ = self.inventory()
        first, second = self.quote(), self.quote(user=self.other)
        results = self.simultaneous([lambda: self.redeem(first), lambda: self.redeem(second, user=self.other)], reward_lock=True)
        self.assertEqual(results.count('out_of_stock'), 1)
        self.assertCountEqual([self.balance(), self.balance(self.other)], [160, 260])
        self.assertEqual(Redemption.objects.count(), 1)
        code.refresh_from_db()
        self.assertIsNotNone(code.redemption_id)

    def test_simultaneous_duplicate_confirmation_allocates_once(self):
        self.inventory()
        self.inventory()
        quote = self.quote()
        results = self.simultaneous([lambda: self.redeem(quote), lambda: self.redeem(quote)])
        self.assertCountEqual([result.replayed for result in results], [False, True])
        self.assertEqual(results[0].redemption_id, results[1].redemption_id)
        self.assertEqual(self.balance(), 160)
        self.assertEqual(RewardCode.objects.filter(redemption__isnull=False).count(), 1)

    def test_separate_confirmations_cannot_spend_same_points(self):
        CustomerPoints.objects.filter(user=self.user).update(total_points=160)
        self.inventory()
        self.inventory()
        first, second = self.quote(), self.quote()
        results = self.simultaneous([lambda: self.redeem(first), lambda: self.redeem(second)])
        self.assertEqual(results.count('insufficient_points'), 1)
        self.assertEqual(self.balance(), 60)
        self.assertEqual(RewardCode.objects.filter(redemption__isnull=False).count(), 1)

    def test_code_lock_follows_user_points_reward_and_redemption(self):
        self.inventory()
        quote = self.quote()
        with CaptureQueriesContext(connection) as queries:
            self.redeem(quote)
        locks = [query['sql'] for query in queries if 'FOR UPDATE' in query['sql']]
        expected = [get_user_model(), CustomerPoints, Reward, Redemption, RewardCode]
        self.assertEqual(len(locks), len(expected))
        for query, model in zip(locks, expected):
            self.assertIn('FROM "' + model._meta.db_table + '"', query)

    def test_daily_and_streak_changes_force_waiting_redemption_to_review(self):
        self.inventory()
        # Both interleavings reuse actual allocation and actual Points services.
        for operation in (claim_daily_points, claim_streak_bonus):
            with self.subTest(operation=operation.__name__):
                CustomerPoints.objects.filter(user=self.user).update(
                    total_points=260, streak_days=7, last_daily_claim_date=NOW.date() - timedelta(days=1),
                    day_7_bonus_awarded=False, day_14_bonus_awarded=False)
                quote = self.quote()
                attempted = Event()
                with ThreadPoolExecutor(max_workers=1) as pool:
                    with transaction.atomic():
                        get_user_model().objects.select_for_update().get(pk=self.user.pk)
                        future = pool.submit(self.worker, lambda: self.redeem(quote), attempted, get_user_model()._meta.db_table)
                        self.assertTrue(attempted.wait(10))
                        operation(self.user)
                    self.assertEqual(future.result(timeout=20), 'balance_changed')
                self.assertEqual(self.balance(), 305 if operation == claim_daily_points else 295)
                self.assertFalse(Redemption.objects.exists())

    def test_bonus_waiting_for_redemption_reads_committed_balance(self):
        self.inventory()
        attempted = Event()
        with ThreadPoolExecutor(max_workers=1) as pool:
            with transaction.atomic():
                result = self.redeem(self.quote())
                future = pool.submit(self.worker, lambda: claim_streak_bonus(self.user), attempted, get_user_model()._meta.db_table)
                self.assertTrue(attempted.wait(10))
                self.assertFalse(future.done())
            self.assertEqual(future.result(timeout=20).awarded_amount, 35)
        self.assertEqual(self.balance(), 195)
        self.assertEqual(RewardCode.objects.get(redemption_id=result.redemption_id).payload_kind, 'code')

    def test_private_access_only_after_outermost_commit_and_only_for_owner(self):
        for kind in ('voucher', 'external'):
            with self.subTest(kind=kind):
                reward = make_reward(is_active=True, fulfillment_type=kind)
                code, secret = self.inventory(reward)
                revealed = []
                with transaction.atomic():
                    result = self.redeem(self.quote(reward=reward))
                    with self.assertRaises(PrivateBenefitUnavailable):
                        get_private_reward_benefit(self.user, result.redemption_id)
                    transaction.on_commit(lambda: revealed.append(get_private_reward_benefit(self.user, result.redemption_id)))
                    self.assertEqual(revealed, [])
                self.assertEqual(revealed[0].value, secret)
                self.assertEqual(revealed[0].payload_kind, code.payload_kind)
                self.assertNotIn(secret, repr(revealed[0]))
                with patch(PRIVATE_MODULE + '._decode_benefit') as decode:
                    for user, record_id in ((self.other, result.redemption_id), (self.user, uuid4()), (AnonymousUser(), result.redemption_id)):
                        with self.assertRaises(PermissionDenied):
                            get_private_reward_benefit(user, record_id)
                    decode.assert_not_called()
                self.assertEqual(get_private_reward_benefit(self.user, result.redemption_id).value, secret)

    def test_uncommitted_redemption_is_not_visible_to_other_connection(self):
        self.inventory()
        with ThreadPoolExecutor(max_workers=1) as pool:
            with transaction.atomic():
                result = self.redeem(self.quote())

                def visible():
                    close_old_connections()
                    try:
                        return Redemption.objects.filter(pk=result.redemption_id).exists()
                    finally:
                        connections.close_all()

                self.assertFalse(pool.submit(visible).result(timeout=10))
                transaction.set_rollback(True)
        self.assert_unspent()
        with self.assertRaises(PermissionDenied):
            get_private_reward_benefit(self.user, result.redemption_id)

    def test_private_access_denies_inactive_user_revoked_or_refunded_benefit(self):
        code, _ = self.inventory()
        result = self.redeem(self.quote())
        get_user_model().objects.filter(pk=self.user.pk).update(is_active=False)
        with self.assertRaises(PermissionDenied):
            get_private_reward_benefit(self.user, result.redemption_id)
        get_user_model().objects.filter(pk=self.user.pk).update(is_active=True)
        RewardCode.objects.filter(pk=code.pk).update(is_active=False)
        with self.assertRaises(PrivateBenefitUnavailable):
            get_private_reward_benefit(self.user, result.redemption_id)
        RewardCode.objects.filter(pk=code.pk).update(is_active=True)
        Redemption.objects.filter(pk=result.redemption_id).update(refunded_points=100, refunded_at=NOW)
        with self.assertRaises(PrivateBenefitUnavailable):
            get_private_reward_benefit(self.user, result.redemption_id)
        self.assertEqual(self.balance(), 160)  # Fixture mutation is not a refund service.

    def test_private_values_are_not_logged_and_corrupt_payloads_fail_closed(self):
        secret = 'PRIVATE-NEVER-LOG-THIS'
        failures = [b'invalid-ciphertext',
                    Fernet(TEST_KEYS['REWARDS_CODE_ENCRYPTION_KEY'].encode()).encrypt(json.dumps(
                        {'reward': str(uuid4()), 'kind': 'code', 'value': secret}).encode())]
        for payload in failures:
            with self.subTest(payload_length=len(payload)):
                reward = make_reward(is_active=True)
                code, _ = self.inventory(reward, value=secret)
                RewardCode.objects.filter(pk=code.pk).update(encrypted_payload=payload)
                quote = self.quote(reward=reward)
                with patch.object(logging.Logger, '_log') as logs:
                    result = self.redeem(quote)
                    with self.assertRaises(PrivateBenefitUnavailable) as caught:
                        get_private_reward_benefit(self.user, result.redemption_id)
                self.assertNotIn(secret, str(caught.exception))
                self.assertNotIn(secret, repr(logs.call_args_list))
                self.assertEqual(self.redeem(quote).redemption_id, result.redemption_id)

    def test_private_access_rejects_unknown_keys_and_fingerprint_mismatch(self):
        for field, value in [('encryption_key_id', 'another-key'), ('payload_fingerprint', '0' * 64)]:
            with self.subTest(field=field):
                reward = make_reward(is_active=True)
                code, _ = self.inventory(reward)
                if field == 'payload_fingerprint':
                    RewardCode.objects.filter(pk=code.pk).update(**{field: value})
                result = self.redeem(self.quote(reward=reward))
                overrides = {'REWARDS_CODE_KEY_ID': 'another-key'} if field == 'encryption_key_id' else {}
                with override_settings(**overrides), self.assertRaises(PrivateBenefitUnavailable):
                    get_private_reward_benefit(self.user, result.redemption_id)
