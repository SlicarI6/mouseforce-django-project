"""Rewards schema tests. These create records, never perform redemptions."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from hashlib import sha256
from importlib import import_module
from threading import Barrier
from uuid import uuid4

from cryptography.fernet import Fernet
from django.contrib.auth import get_user_model
from django.db import IntegrityError, close_old_connections, connection, connections, transaction
from django.db.migrations.loader import MigrationLoader
from django.db.models.deletion import ProtectedError
from django.test import RequestFactory, SimpleTestCase, TestCase, TransactionTestCase, skipUnlessDBFeature
from django.utils import timezone

from .models import (
    CustomerPoints, Redemption, RedemptionEvent, Reward, RewardCode,
    RewardFulfillment, RewardRequest,
)
from .views import customer_rewards


def make_reward(**overrides):
    data = dict(title='Schema test reward', short_description='Test summary',
                full_description='The complete test benefit.', category='beauty',
                points_required=100, fulfillment_type='voucher', terms='Test conditions.')
    data.update(overrides)
    return Reward.objects.create(**data)


def make_redemption(user, reward, **overrides):
    data = dict(user=user, reward=reward, idempotency_key=uuid4(), points_spent=reward.points_required,
                balance_after=160, snapshot_title=reward.title, snapshot_description=reward.full_description,
                snapshot_fulfillment_type=reward.fulfillment_type, snapshot_terms=reward.terms,
                snapshot_reward_revision=reward.revision)
    data.update(overrides)
    return Redemption.objects.create(**data)


def make_code(reward, **overrides):
    # Test-only key and plaintext. Production import/reveal is intentionally absent.
    secret = ('private-test-voucher-' + uuid4().hex).encode()
    data = dict(reward=reward, payload_kind='code', encryption_key_id='test-key',
                encrypted_payload=Fernet(Fernet.generate_key()).encrypt(secret),
                payload_fingerprint=sha256(secret).hexdigest())
    data.update(overrides)
    return RewardCode.objects.create(**data)


class RewardsSchemaTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_user(username='schema-customer', role='customer')
        cls.other_user = get_user_model().objects.create_user(username='schema-other', role='customer')
        cls.reward = make_reward()
        cls.redemption = make_redemption(cls.user, cls.reward)
        CustomerPoints.objects.create(user=cls.user, total_points=260, streak_days=7,
                                      last_daily_claim_date=timezone.now().date())

    def reject(self, operation):
        with self.assertRaises(IntegrityError), transaction.atomic():
            operation()

    def test_all_six_models_have_uuid_ids(self):
        for model in (Reward, RewardCode, Redemption, RewardFulfillment, RewardRequest, RedemptionEvent):
            with self.subTest(model=model.__name__):
                self.assertEqual(model._meta.pk.get_internal_type(), 'UUIDField')

    def test_reward_defaults_are_unpublished_and_one_per_customer(self):
        self.assertFalse(self.reward.is_active)
        self.assertEqual(self.reward.max_redemptions_per_customer, 1)
        self.assertIsNone(self.reward.stock_remaining)

    def test_reward_cost_must_be_positive_in_database(self):
        for cost in (0, -1):
            with self.subTest(cost=cost):
                self.reject(lambda: make_reward(points_required=cost))

    def test_customer_limit_is_positive_or_unlimited(self):
        for limit in (0, -1):
            with self.subTest(limit=limit):
                self.reject(lambda: make_reward(max_redemptions_per_customer=limit))
        self.assertIsNone(make_reward(max_redemptions_per_customer=None).max_redemptions_per_customer)
        self.assertEqual(make_reward(max_redemptions_per_customer=3).max_redemptions_per_customer, 3)

    def test_stock_cannot_be_negative_and_zero_is_valid(self):
        self.reject(lambda: make_reward(fulfillment_type='physical', stock_remaining=-1))
        self.assertEqual(make_reward(fulfillment_type='physical', stock_remaining=0).stock_remaining, 0)

    def test_codes_and_partner_links_do_not_have_a_second_stock_counter(self):
        for kind in ('voucher', 'external'):
            with self.subTest(kind=kind):
                self.reject(lambda: make_reward(fulfillment_type=kind, stock_remaining=5))

    def test_invalid_reward_dates_are_rejected(self):
        now = timezone.now()
        for end in (now, now - timedelta(days=1)):
            self.reject(lambda: make_reward(valid_from=now, valid_until=end))
        self.reject(lambda: make_reward(valid_from=now, benefit_valid_until=now))
        self.reject(lambda: make_reward(valid_until=now, benefit_valid_until=now - timedelta(days=1)))
        make_reward(valid_from=now, valid_until=now + timedelta(days=1), benefit_valid_until=now + timedelta(days=2))

    def test_invalid_category_type_access_and_revision_are_rejected(self):
        for field, value in [('category', 'all'), ('fulfillment_type', 'cash'), ('access_scope', 'public'), ('revision', 0)]:
            with self.subTest(field=field):
                self.reject(lambda: make_reward(**{field: value}))

    def test_selected_customer_relationship(self):
        reward = make_reward(access_scope='selected_customers')
        reward.eligible_users.add(self.user)
        self.assertEqual(list(reward.eligible_users.all()), [self.user])

    def test_idempotency_is_unique_per_customer(self):
        token = self.redemption.idempotency_key
        self.reject(lambda: make_redemption(self.user, self.reward, idempotency_key=token))
        make_redemption(self.other_user, self.reward, idempotency_key=token)
        make_redemption(self.user, self.reward)  # Separate intent; limits are a future service responsibility.

    def test_redemption_cost_and_revision_are_positive(self):
        self.reject(lambda: make_redemption(self.user, self.reward, points_spent=0))
        self.reject(lambda: make_redemption(self.user, self.reward, snapshot_reward_revision=0))

    def test_all_snapshot_fields_are_database_immutable(self):
        # QuerySet.update bypasses Model.save()/forms; the DB must still reject it.
        changes = {
            'id': uuid4(), 'user_id': self.other_user.pk, 'reward_id': make_reward().pk,
            'idempotency_key': uuid4(), 'points_spent': 50, 'balance_after': 999,
            'snapshot_title': 'Changed', 'snapshot_description': 'Changed',
            'snapshot_fulfillment_type': 'external', 'snapshot_partner_name': 'Changed',
            'snapshot_terms': 'Changed', 'snapshot_fulfillment_instructions': 'Changed',
            'snapshot_information_url': 'https://example.com/', 'snapshot_country_code': 'GB',
            'snapshot_region': 'Changed', 'snapshot_city': 'Changed', 'snapshot_location_details': 'Changed',
            'snapshot_valid_from': timezone.now(), 'snapshot_valid_until': timezone.now(),
            'snapshot_benefit_valid_until': timezone.now(), 'snapshot_reward_revision': 2,
            'created_at': timezone.now() - timedelta(days=1),
        }
        for field, value in changes.items():
            with self.subTest(field=field):
                self.reject(lambda: Redemption.objects.filter(pk=self.redemption.pk).update(**{field: value}))

    def test_snapshot_is_stable_when_catalogue_changes(self):
        self.reward.title = 'Updated catalogue title'
        self.reward.points_required = 300
        self.reward.save()
        self.redemption.refresh_from_db()
        self.assertEqual(self.redemption.snapshot_title, 'Schema test reward')
        self.assertEqual(self.redemption.points_spent, 100)

    def test_snapshot_guard_also_blocks_raw_sql_and_deletion(self):
        def raw_update():
            with connection.cursor() as cursor:
                cursor.execute('UPDATE customerpanel_redemption SET snapshot_title = %s', ['Changed'])
        self.reject(raw_update)
        self.reject(lambda: Redemption.objects.filter(pk=self.redemption.pk).delete())

    def test_status_changes_remain_possible(self):
        self.redemption.status = 'processing'
        self.redemption.save(update_fields=['status'])
        self.redemption.refresh_from_db()
        self.assertEqual(self.redemption.status, 'processing')

    def test_full_refund_amount_and_timestamp_must_match(self):
        for changes in ({'refunded_points': 50, 'refunded_at': timezone.now()},
                        {'refunded_points': 101, 'refunded_at': timezone.now()},
                        {'refunded_points': 100}, {'refunded_at': timezone.now()}):
            self.reject(lambda: Redemption.objects.filter(pk=self.redemption.pk).update(**changes))
        Redemption.objects.filter(pk=self.redemption.pk).update(refunded_points=100, refunded_at=timezone.now())
        self.assertEqual(CustomerPoints.objects.get(user=self.user).total_points, 260)

    def test_redeemed_reward_and_customer_are_protected_from_deletion(self):
        for obj in (self.reward, self.user):
            with self.assertRaises(ProtectedError):
                obj.delete()

    def test_ciphertext_storage_and_safe_object_representations(self):
        secret = b'SECRET-CODE-MUST-NOT-APPEAR'
        key = Fernet.generate_key()
        encrypted = Fernet(key).encrypt(secret)
        code = make_code(self.reward, encrypted_payload=encrypted)
        code.refresh_from_db()
        self.assertEqual(Fernet(key).decrypt(bytes(code.encrypted_payload)), secret)
        self.assertNotIn(secret, bytes(code.encrypted_payload))
        self.assertNotIn(secret.decode(), str(code))
        self.assertNotIn(secret.decode(), repr(code))
        self.assertFalse(RewardCode._meta.get_field('encrypted_payload').editable)
        self.assertFalse(any(field.name in ('code', 'value', 'private_url') for field in RewardCode._meta.fields))

    def test_duplicate_code_fingerprints_are_rejected(self):
        code = make_code(self.reward)
        self.reject(lambda: make_code(self.reward, payload_fingerprint=code.payload_fingerprint))

    def test_code_payload_metadata_cannot_be_empty_or_invalid(self):
        for values in ({'encrypted_payload': b''}, {'encryption_key_id': ''},
                       {'payload_fingerprint': 'invalid'}, {'payload_kind': 'public'}):
            self.reject(lambda: make_code(self.reward, **values))

    def test_code_inventory_type_matches_reward(self):
        self.reject(lambda: make_code(make_reward(fulfillment_type='physical')))
        external = make_reward(fulfillment_type='external')
        self.reject(lambda: make_code(external))
        make_code(external, payload_kind='claim_link')

    def test_code_assignment_requires_date_and_matching_reward(self):
        code = make_code(self.reward)
        self.reject(lambda: RewardCode.objects.filter(pk=code.pk).update(redemption=self.redemption))
        other = make_redemption(self.user, make_reward())
        self.reject(lambda: RewardCode.objects.filter(pk=code.pk).update(redemption=other, assigned_at=timezone.now()))

    def test_code_cannot_be_assigned_twice_or_reassigned(self):
        code = make_code(self.reward)
        RewardCode.objects.filter(pk=code.pk).update(redemption=self.redemption, assigned_at=timezone.now())
        second = make_code(self.reward)
        self.reject(lambda: RewardCode.objects.filter(pk=second.pk).update(redemption=self.redemption, assigned_at=timezone.now()))
        other = make_redemption(self.user, self.reward)
        for values in ({'redemption': other}, {'redemption': None, 'assigned_at': None},
                       {'encrypted_payload': b'replaced'}, {'payload_fingerprint': 'a' * 64}):
            self.reject(lambda: RewardCode.objects.filter(pk=code.pk).update(**values))
        self.reject(lambda: RewardCode.objects.filter(pk=code.pk).delete())

    def test_expired_code_cannot_be_assigned(self):
        code = make_code(self.reward, expires_at=timezone.now() - timedelta(days=1))
        self.reject(lambda: RewardCode.objects.filter(pk=code.pk).update(redemption=self.redemption, assigned_at=timezone.now()))

    def test_only_physical_and_manual_redemptions_get_fulfillment(self):
        self.reject(lambda: RewardFulfillment.objects.create(redemption=self.redemption))
        for kind in ('physical', 'manual'):
            redemption = make_redemption(self.user, make_reward(fulfillment_type=kind))
            fulfillment = RewardFulfillment.objects.create(redemption=redemption)
            self.assertEqual(fulfillment.status, 'pending')
            self.reject(lambda: RewardFulfillment.objects.create(redemption=redemption))

    def test_manual_request_and_approval_do_not_touch_points(self):
        before = CustomerPoints.objects.filter(user=self.user).values().get()
        request = RewardRequest.objects.create(user=self.user, title='An experience', description='Please review my idea.')
        self.assertIsNone(request.redemption_id)
        self.reject(lambda: RewardRequest.objects.filter(pk=request.pk).update(status='approved'))
        request.status = 'approved'
        request.approved_reward = make_reward(fulfillment_type='manual')
        request.save()
        self.assertEqual(CustomerPoints.objects.filter(user=self.user).values().get(), before)

    def test_request_cannot_link_another_customers_redemption(self):
        request = RewardRequest.objects.create(user=self.other_user, title='Test', description='Test', approved_reward=self.reward)
        self.reject(lambda: RewardRequest.objects.filter(pk=request.pk).update(redemption=self.redemption))
        request.user = self.user
        request.redemption = self.redemption
        request.save()
        self.reject(lambda: RewardRequest.objects.filter(pk=request.pk).update(redemption=None))

    def test_audit_is_append_only_even_with_bulk_update_and_delete(self):
        event = RedemptionEvent.objects.create(redemption=self.redemption, event_type='processing')
        self.reject(lambda: RedemptionEvent.objects.filter(pk=event.pk).update(internal_note='Rewrite'))
        self.reject(lambda: RedemptionEvent.objects.filter(pk=event.pk).delete())

    def test_charge_and_refund_events_are_unique_and_have_correct_signs(self):
        for event_type, delta in [('redeemed', -100), ('refunded', 100)]:
            RedemptionEvent.objects.create(redemption=self.redemption, event_type=event_type, points_delta=delta, balance_after=160)
            self.reject(lambda: RedemptionEvent.objects.create(redemption=self.redemption, event_type=event_type, points_delta=delta, balance_after=160))
        for event_type, delta in [('redeemed', 100), ('refunded', -100), ('processing', 10), ('invented', 0)]:
            self.reject(lambda: RedemptionEvent.objects.create(redemption=self.redemption, event_type=event_type, points_delta=delta, balance_after=160))

    def test_saving_all_record_types_does_not_deduct_points(self):
        before = CustomerPoints.objects.filter(user=self.user).values().get()
        reward = make_reward(fulfillment_type='physical', stock_remaining=4)
        redemption = make_redemption(self.user, reward)
        RewardFulfillment.objects.create(redemption=redemption)
        RewardRequest.objects.create(user=self.user, title='Request', description='Test')
        RedemptionEvent.objects.create(redemption=redemption, event_type='redeemed', points_delta=-100, balance_after=160)
        make_code(self.reward)
        self.assertEqual(CustomerPoints.objects.filter(user=self.user).values().get(), before)
        reward.refresh_from_db()
        self.assertEqual(reward.stock_remaining, 4)

    def test_unpublished_catalogue_contains_no_private_inventory(self):
        from .section_test_support import seed_paid_access
        seed_paid_access(self.user)
        code = make_code(self.reward)
        request = RequestFactory().get('/customer/rewards/')
        request.user = self.user
        response = customer_rewards(request)
        self.assertContains(response, 'New rewards are on the way.')
        self.assertNotContains(response, self.reward.title)
        self.assertNotContains(response, code.payload_fingerprint)
        self.assertNotContains(response, bytes(code.encrypted_payload).decode())

    def test_expected_database_indexes_exist(self):
        expected = {
            Reward: ['reward_catalog_idx', 'reward_expiry_idx'],
            Redemption: ['redemption_history_idx', 'redemption_customer_reward_idx', 'redemption_queue_idx', 'redemption_unique_attempt'],
            RewardCode: ['rewardcode_available_idx', 'rewardcode_unique_fingerprint'],
            RewardFulfillment: ['fulfillment_queue_idx'],
            RewardRequest: ['rewardrequest_customer_idx', 'rewardrequest_queue_idx'],
            RedemptionEvent: ['redemptionevent_history_idx', 'redemptionevent_once_per_charge'],
        }
        with connection.cursor() as cursor:
            for model, names in expected.items():
                constraints = connection.introspection.get_constraints(cursor, model._meta.db_table)
                for name in names:
                    self.assertIn(name, constraints)


@skipUnlessDBFeature('has_select_for_update')
class ConcurrentRewardsSchemaTests(TransactionTestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username='concurrent-schema', role='customer')
        self.reward = make_reward()

    def run_contenders(self, operation):
        barrier = Barrier(2)
        def contender(number):
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                with transaction.atomic():
                    operation(number)
                return 'created'
            except IntegrityError:
                return 'rejected'
            finally:
                connections.close_all()
        with ThreadPoolExecutor(max_workers=2) as workers:
            outcomes = list(workers.map(contender, (0, 1)))
        self.assertCountEqual(outcomes, ['created', 'rejected'])

    def test_concurrent_duplicate_attempt_has_one_record(self):
        token = uuid4()
        self.run_contenders(lambda _: make_redemption(self.user, self.reward, idempotency_key=token))
        self.assertEqual(Redemption.objects.filter(user=self.user, idempotency_key=token).count(), 1)
        self.assertFalse(CustomerPoints.objects.filter(user=self.user).exists())

    def test_concurrent_code_assignment_cannot_issue_same_code_twice(self):
        code = make_code(self.reward)
        redemptions = [make_redemption(self.user, self.reward) for _ in range(2)]
        self.run_contenders(lambda number: RewardCode.objects.filter(pk=code.pk).update(redemption=redemptions[number], assigned_at=timezone.now()))
        code.refresh_from_db()
        self.assertIn(code.redemption_id, [redemption.pk for redemption in redemptions])


class RewardsMigrationStructureTests(SimpleTestCase):
    def test_migration_adds_only_new_rewards_schema(self):
        loader = MigrationLoader(None)
        before = loader.project_state([('customerpanel', '0005_customerpoints')])
        after = loader.project_state([('customerpanel', '0006_rewards_database')])
        for key in [('customerpanel', 'customerpoints'), ('accounts', 'customuser')]:
            self.assertEqual(before.models[key], after.models[key])
        added = set(after.models) - set(before.models)
        self.assertEqual(added, {('customerpanel', name) for name in (
            'reward', 'rewardcode', 'redemption', 'rewardfulfillment', 'rewardrequest', 'redemptionevent',
        )})

    def test_guards_target_only_new_reward_tables(self):
        migration = import_module('mouse_force_first_step.customerpanel.migrations.0006_rewards_database')
        for vendor in ('postgresql', 'sqlite'):
            for _, table, _, condition, _ in migration.guard_specs(vendor):
                self.assertIn(table, ['customerpanel_' + name for name in (
                    'redemption', 'rewardcode', 'rewardfulfillment', 'rewardrequest', 'redemptionevent',
                )])
                self.assertNotIn('customerpoints', condition)
                self.assertNotIn('accounts_customuser', condition)
