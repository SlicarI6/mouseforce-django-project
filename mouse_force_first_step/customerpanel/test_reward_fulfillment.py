"""Stage 2D.4: isolated fulfillment, transport, staff and PostgreSQL races."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Event
from unittest.mock import patch

from django.contrib import admin
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core import signing
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, connection, transaction
from django.test import Client, RequestFactory, TestCase, TransactionTestCase, skipUnlessDBFeature
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from .models import CustomerPoints, Reward, RewardFulfillment, Redemption, RedemptionEvent, RewardRequest
from .points import claim_daily_points, claim_streak_bonus
from .reward_confirmation import create_reward_confirmation, ConfirmationError, TOKEN_SALT
from .reward_redemption import redeem_reward, FulfillmentAllocation
from .reward_fulfillment import FulfillmentForm, FulfillmentInputError, process_fulfillment
from .reward_admin_forms import RewardAdminForm
from . import test_reward_allocation as digital
from .test_reward_models import make_reward

NOW, SESSION = digital.NOW, digital.SESSION
DELIVERY = {'recipient_name': 'Test Recipient', 'contact_email': 'private-recipient@example.test',
    'address_line_1': '123 Private Test Street', 'address_line_2': '', 'city': 'London',
    'postal_code': 'AB12 3CD', 'country_code': 'GB'}


class FulfillmentFixtures(digital.DigitalFixtures):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        Reward.objects.filter(pk=cls.reward.pk).update(fulfillment_type='physical', stock_remaining=3)
        cls.reward.refresh_from_db()

    def redeem(self, quote, user=None, details=None):
        return redeem_reward(user or self.user, quote.reward_id, quote.token, session_key=SESSION,
                             fulfillment_data=DELIVERY if details is None else details)


class FulfillmentServiceTests(FulfillmentFixtures, TestCase):
    def test_physical_spends_reserves_and_creates_one_pending_fulfillment(self):
        before = CustomerPoints.objects.values().get(user=self.user)
        result = self.redeem(self.quote())
        record = Redemption.objects.get(pk=result.redemption_id)
        self.assertEqual(record.stock_reserved_quantity, 1)
        self.assertEqual(record.status, 'pending')
        self.assertEqual(record.fulfillment.status, 'pending')
        self.assertEqual(record.fulfillment.contact_email, DELIVERY['contact_email'])
        self.assertEqual(Reward.objects.get(pk=self.reward.pk).stock_remaining, 2)
        self.assertEqual(CustomerPoints.objects.values().get(user=self.user), {**before, 'total_points': 160})
        self.assertEqual(record.events.get().points_delta, -100)
        self.assertIsNone(record.stock_restored_at)
        self.assertFalse(RewardRequest.objects.exists())

    def test_defined_manual_without_shipping_or_contact(self):
        reward = make_reward(is_active=True, fulfillment_type='manual', stock_remaining=2)
        result = self.redeem(self.quote(reward), details={'request_details': 'Choose the afternoon session.'})
        record = Redemption.objects.get(pk=result.redemption_id)
        self.assertEqual(record.fulfillment.request_details, 'Choose the afternoon session.')
        self.assertEqual(record.fulfillment.address_line_1, '')
        self.assertEqual(record.fulfillment.contact_email, '')
        reward.refresh_from_db()
        self.assertEqual(reward.stock_remaining, 1)

    def test_unlimited_stock_has_zero_reserved_quantity(self):
        Reward.objects.filter(pk=self.reward.pk).update(stock_remaining=None)
        result = self.redeem(self.quote())
        self.assertEqual(Redemption.objects.get(pk=result.redemption_id).stock_reserved_quantity, 0)
        self.assertIsNone(Reward.objects.get(pk=self.reward.pk).stock_remaining)

    def test_retry_returns_same_record_without_requiring_or_overwriting_details(self):
        quote = self.quote()
        first = self.redeem(quote)
        second = self.redeem(quote, details={'recipient_name': 'Changed on retry'})
        self.assertEqual(first.redemption_id, second.redemption_id)
        self.assertTrue(second.replayed)
        self.assertEqual(RewardFulfillment.objects.count(), 1)
        self.assertEqual(RewardFulfillment.objects.get().recipient_name, DELIVERY['recipient_name'])
        self.assertEqual(self.balance(), 160)
        self.assertEqual(Reward.objects.get(pk=self.reward.pk).stock_remaining, 2)

    def test_missing_and_invalid_fields_cannot_spend(self):
        for data in ({}, {**DELIVERY, 'contact_email': 'invalid'}, {**DELIVERY, 'postal_code': ''},
                     {**DELIVERY, 'country_code': 'England'}, {**DELIVERY, 'recipient_name': 'x' * 201},
                     {**DELIVERY, 'contact_phone': '+44123456789'}):
            with self.subTest(keys=list(data)):
                with self.assertRaises(FulfillmentInputError):
                    self.redeem(self.quote(), details=data)
        self.assert_unspent()
        self.assertEqual(Reward.objects.get(pk=self.reward.pk).stock_remaining, 3)

    def test_explicit_delivery_restrictions_are_enforced(self):
        Reward.objects.filter(pk=self.reward.pk).update(country_code='GB', region='London', city='London', requires_region=True)
        with self.assertRaises(FulfillmentInputError):
            self.redeem(self.quote(), details={**DELIVERY, 'region': 'Elsewhere'})
        self.redeem(self.quote(), details={**DELIVERY, 'region': 'London'})

    def test_physical_always_requires_shipping_and_contact(self):
        form = FulfillmentForm(self.reward)
        self.assertTrue(set(DELIVERY).issubset(form.fields))
        self.assertNotIn('region', form.fields)
        self.assertNotIn('contact_phone', form.fields)
        for name in set(DELIVERY) - {'address_line_2'}:
            self.assertTrue(form.fields[name].required)

    def test_manual_uses_only_configured_fields(self):
        reward = make_reward(fulfillment_type='manual')
        self.assertEqual(set(FulfillmentForm(reward).fields), {'request_details'})
        reward.requires_shipping_address = True
        self.assertIn('address_line_1', FulfillmentForm(reward).fields)
        self.assertNotIn('contact_email', FulfillmentForm(reward).fields)
        reward.requires_shipping_address = False
        reward.requires_phone = reward.requires_contact_details = True
        self.assertEqual(set(FulfillmentForm(reward).fields), {'request_details', 'recipient_name', 'contact_email', 'contact_phone'})
        reward.requires_shipping_address = reward.requires_region = True
        self.assertIn('region', FulfillmentForm(reward).fields)
        self.assertIn('address_line_1', FulfillmentForm(reward).fields)

    def test_digital_forms_collect_nothing(self):
        for kind in ('voucher', 'external'):
            self.assertFalse(FulfillmentForm(make_reward(fulfillment_type=kind)).fields)

    def test_configured_phone_and_region_are_required_and_validated(self):
        reward = make_reward(is_active=True, fulfillment_type='manual', requires_shipping_address=True,
            requires_contact_details=True, requires_region=True, requires_phone=True)
        for phone in ('', '......', '+123', '1234567890123456'):
            form = FulfillmentForm(reward, {**DELIVERY, 'contact_phone': phone, 'region': 'London'})
            self.assertFalse(form.is_valid())
            self.assertIn('contact_phone', form.errors)
        with self.assertRaises(FulfillmentInputError):
            self.redeem(self.quote(reward), details={**DELIVERY, 'contact_phone': '+44 1234567890'})
        result = self.redeem(self.quote(reward), details={**DELIVERY, 'contact_phone': '+44 1234567890', 'region': 'London'})
        self.assertEqual(Redemption.objects.get(pk=result.redemption_id).fulfillment.region, 'London')

    def test_requirement_database_constraints_and_safe_defaults(self):
        for changes in ({'requires_region': True}, {'requires_phone': True}):
            with self.assertRaises(IntegrityError), transaction.atomic():
                make_reward(fulfillment_type='manual', **changes)
        for flag in ('requires_region', 'requires_phone', 'requires_contact_details', 'requires_shipping_address'):
            with self.assertRaises(IntegrityError), transaction.atomic():
                make_reward(**{flag: True})
            self.assertFalse(getattr(self.reward, flag))
        make_reward(fulfillment_type='physical', requires_phone=True, requires_region=True)

    def test_admin_phone_implies_manual_contact_and_rejects_region_without_shipping(self):
        from .test_reward_admin import reward_data
        reward = make_reward(fulfillment_type='manual')
        form = RewardAdminForm(reward_data(reward, requires_phone='on'), instance=reward)
        self.assertTrue(form.is_valid(), form.errors)
        self.assertTrue(form.save().requires_contact_details)
        form = RewardAdminForm(reward_data(reward, requires_region='on'), instance=reward)
        self.assertFalse(form.is_valid())
        self.assertIn('requires_region', form.errors)

    def test_requirement_changes_force_fresh_confirmation_and_token_has_no_pii(self):
        quote = self.quote()
        payload = signing.loads(quote.token, salt=TOKEN_SALT)
        for value in DELIVERY.values():
            if value:
                self.assertNotIn(value, str(payload))
        for flag in ('requires_region', 'requires_phone', 'requires_contact_details', 'requires_shipping_address'):
            with self.subTest(flag=flag):
                Reward.objects.filter(pk=self.reward.pk).update(**{flag: True})
                self.assert_error('offer_changed', lambda: self.redeem(quote))
                Reward.objects.filter(pk=self.reward.pk).update(**{flag: False})
        self.assert_unspent()

    def test_inactive_expired_future_ineligible_and_insufficient_checks(self):
        quote = self.quote()
        for changes in ({'is_active': False}, {'valid_until': NOW}, {'valid_from': NOW + timedelta(days=1)},
                        {'access_scope': 'selected_customers'}):
            Reward.objects.filter(pk=self.reward.pk).update(**changes)
            self.assert_error('reward_unavailable', lambda: self.redeem(quote))
            Reward.objects.filter(pk=self.reward.pk).update(is_active=True, valid_until=None, valid_from=None, access_scope='all_customers')
        CustomerPoints.objects.filter(user=self.user).update(total_points=1)
        self.assert_error('insufficient_points', lambda: self.redeem(quote))
        self.assertFalse(RewardFulfillment.objects.exists())

    def test_customer_limit_counts_committed_record(self):
        Reward.objects.filter(pk=self.reward.pk).update(max_redemptions_per_customer=1)
        self.redeem(self.quote())
        self.assert_error('limit_reached', self.quote)

    def test_failure_before_fulfillment_creation_rolls_back_stock_and_points(self):
        quote = self.quote()
        with patch.object(RewardFulfillment.objects, 'create', side_effect=IntegrityError('Isolated failure')):
            with self.assertRaises(IntegrityError):
                self.redeem(quote)
        self.assert_unspent()
        self.assertEqual(Reward.objects.get(pk=self.reward.pk).stock_remaining, 3)
        self.redeem(quote)

    def test_failed_stock_reservation_rolls_back_deduction_and_record(self):
        original = FulfillmentAllocation.reserve
        def fail(plan, record):
            Reward.objects.filter(pk=plan.reward.pk).update(stock_remaining=0)
            original(plan, record)
        with patch.object(FulfillmentAllocation, 'reserve', fail):
            self.assert_error('out_of_stock', lambda: self.redeem(self.quote()))
        self.assert_unspent()
        self.assertEqual(Reward.objects.get(pk=self.reward.pk).stock_remaining, 3)

    def test_audit_failure_rolls_back_all_fulfillment_writes(self):
        with patch.object(RedemptionEvent.objects, 'create', side_effect=IntegrityError('Isolated failure')):
            with self.assertRaises(IntegrityError):
                self.redeem(self.quote())
        self.assert_unspent()
        self.assertFalse(RewardFulfillment.objects.exists())
        self.assertEqual(Reward.objects.get(pk=self.reward.pk).stock_remaining, 3)

    def test_staff_actions_are_audited_idempotent_and_preserve_snapshots(self):
        result = self.redeem(self.quote())
        f = RewardFulfillment.objects.get(redemption_id=result.redemption_id)
        before = Redemption.objects.values().get(pk=result.redemption_id)
        for status in ('processing', 'dispatched', 'completed', 'completed'):
            process_fulfillment(self.staff, f.pk, status)
        f.refresh_from_db()
        self.assertEqual(f.status, 'completed')
        self.assertIsNotNone(f.dispatched_at)
        self.assertEqual(RedemptionEvent.objects.count(), 4)
        after = Redemption.objects.values().get(pk=result.redemption_id)
        for key in ('status', 'completed_at', 'updated_at'):
            before.pop(key); after.pop(key)
        self.assertEqual(before, after)
        self.assertEqual(self.balance(), 160)
        self.assertEqual(Reward.objects.get(pk=self.reward.pk).stock_remaining, 2)

    def test_staff_permissions_and_illegal_transitions(self):
        result = self.redeem(self.quote())
        f = RewardFulfillment.objects.get(redemption_id=result.redemption_id)
        with self.assertRaises(PermissionDenied):
            process_fulfillment(self.user, f.pk, 'processing')
        staff = get_user_model().objects.create_user(username='no-fulfillment-permission', is_staff=True)
        with self.assertRaises(PermissionDenied):
            process_fulfillment(staff, f.pk, 'processing')
        for status in ('dispatched', 'completed', 'cancelled'):
            with self.assertRaises(ValidationError):
                process_fulfillment(self.staff, f.pk, status)
        f.refresh_from_db()
        self.assertEqual(f.status, 'pending')

    def test_manual_without_delivery_cannot_be_dispatched(self):
        reward = make_reward(is_active=True, fulfillment_type='manual')
        result = self.redeem(self.quote(reward), details={})
        f = RewardFulfillment.objects.get(redemption_id=result.redemption_id)
        process_fulfillment(self.staff, f.pk, 'processing')
        with self.assertRaises(ValidationError):
            process_fulfillment(self.staff, f.pk, 'dispatched')
        process_fulfillment(self.staff, f.pk, 'completed')

    def test_admin_keeps_fields_readonly_and_exposes_only_controlled_actions(self):
        ma = admin.site._registry[RewardFulfillment]
        request = RequestFactory().get('/')
        request.user = self.staff
        self.assertFalse(ma.has_change_permission(request))
        self.assertIn('redemption', ma.get_readonly_fields(request))
        self.assertEqual(set(ma.get_actions(request)), {'begin_processing', 'mark_dispatched', 'complete_fulfillment'})
        request.user = self.user
        self.assertFalse(ma.get_actions(request))

    def test_admin_processing_post_uses_controlled_service(self):
        record = self.redeem(self.quote())
        fulfillment = RewardFulfillment.objects.get(redemption_id=record.redemption_id)
        self.client.force_login(self.staff)
        url = reverse('admin:customerpanel_rewardfulfillment_changelist')
        response = self.client.post(url, {'action': 'begin_processing', '_selected_action': str(fulfillment.pk)})
        self.assertEqual(response.status_code, 302)
        fulfillment.refresh_from_db()
        self.assertEqual(fulfillment.status, 'processing')
        self.assertEqual(fulfillment.processed_by_id, self.staff.pk)
        self.assertEqual(RedemptionEvent.objects.filter(event_type='processing').count(), 1)
        # Even the same superuser cannot use the normal change form to rewrite PII/identity/status.
        change = reverse('admin:customerpanel_rewardfulfillment_change', args=[fulfillment.pk])
        self.assertEqual(self.client.post(change, {'status': 'completed', 'recipient_name': 'Forged'}).status_code, 403)
        self.assertEqual(self.balance(), 160)


class FulfillmentRequestTests(FulfillmentFixtures, TestCase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.user)

    def confirm(self, client=None, reward=None):
        return (client or self.client).get(reverse('customer_reward_confirm', args=[(reward or self.reward).pk]))

    def post(self, quote=None, data=None, client=None):
        quote = quote or self.confirm(client).context['confirmation']
        return (client or self.client).post(reverse('customer_reward_redeem', args=[quote.reward_id]),
            {'confirmation_token': quote.token, **(DELIVERY if data is None else data)}, HTTP_ACCEPT='application/json')

    def test_physical_post_and_result_and_duplicate(self):
        quote = self.confirm().context['confirmation']
        first = self.post(quote)
        second = self.post(quote)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.json()['redirect_url'], second.json()['redirect_url'])
        result = self.client.get(first.json()['redirect_url'])
        for text in ('Request received — awaiting fulfillment.', '100 Points', DELIVERY['contact_email']):
            self.assertContains(result, text)
        self.assertIn('no-store', result['Cache-Control'])
        self.assertEqual(RewardFulfillment.objects.count(), 1)

    def test_manual_post_and_result(self):
        reward = make_reward(is_active=True, fulfillment_type='manual', requires_contact_details=True)
        quote = self.confirm(reward=reward).context['confirmation']
        response = self.post(quote, data={'recipient_name': 'Test', 'contact_email': 'test@example.test', 'request_details': 'Afternoon please'})
        self.assertEqual(response.status_code, 200)
        self.assertContains(self.client.get(response.json()['redirect_url']), 'Awaiting manual processing.')

    def test_invalid_fields_have_safe_errors_and_no_charge(self):
        response = self.post(data={'contact_email': 'PRIVATE-INVALID-EMAIL'})
        self.assertEqual(response.status_code, 422)
        self.assertIn('contact_email', response.json()['field_errors'])
        self.assertNotIn('PRIVATE-INVALID-EMAIL', response.content.decode())
        self.assert_unspent()

    def test_non_js_validation_renders_private_no_store_form(self):
        quote = self.confirm().context['confirmation']
        response = self.client.post(reverse('customer_reward_redeem', args=[self.reward.pk]), {'confirmation_token': quote.token})
        self.assertEqual(response.status_code, 422)
        self.assertContains(response, 'This field is required.', status_code=422)
        self.assertIn('no-store', response['Cache-Control'])

    def test_csrf_access_and_tampered_authoritative_fields(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.user)
        quote = self.confirm(client).context['confirmation']
        self.assertEqual(self.post(quote, client=client).status_code, 403)
        for field in ('user_id', 'points_required', 'stock_remaining', 'status'):
            self.assertEqual(self.post(data={**DELIVERY, field: '1'}).status_code, 400)
        response = self.post(quote, data={**DELIVERY, 'csrfmiddlewaretoken': client.cookies['csrftoken'].value}, client=client)
        self.assertEqual(response.status_code, 200)
        self.client.logout()
        self.assertEqual(self.post(quote).status_code, 403)

    def test_wrong_owner_and_public_pages_never_expose_delivery_data(self):
        response = self.post()
        other = Client(); other.force_login(self.other)
        self.assertEqual(other.get(response.json()['redirect_url']).status_code, 404)
        for url in (reverse('customer_rewards'), reverse('customer_reward_detail', args=[self.reward.pk])):
            with CaptureQueriesContext(connection) as queries:
                page = self.client.get(url)
            for key in ('contact_email', 'address_line_1'):
                self.assertNotContains(page, DELIVERY[key])
            self.assertTrue(all('customerpanel_rewardfulfillment' not in q['sql'] for q in queries))

    def test_existing_shell_and_assets_used_for_physical_flow(self):
        confirm = self.confirm()
        result = self.client.get(self.post(confirm.context['confirmation']).json()['redirect_url'])
        for page in (confirm, result):
            for text in ('customer_reward_actions.css', 'customer_reward_actions.js', 'class="customer-music-audio"'):
                self.assertContains(page, text)
        self.assertContains(result, 'data-fulfillment-private')


@skipUnlessDBFeature('has_select_for_update')
class FulfillmentConcurrencyTests(FulfillmentFixtures, TransactionTestCase):
    available_apps = digital.DigitalCommitAndConcurrencyTests.available_apps
    worker = digital.DigitalCommitAndConcurrencyTests.worker
    simultaneous = digital.DigitalCommitAndConcurrencyTests.simultaneous

    def setUp(self):
        self.setUpTestData()
        super().setUp()

    def test_last_physical_unit_only_charges_one_customer(self):
        Reward.objects.filter(pk=self.reward.pk).update(stock_remaining=1)
        first, second = self.quote(), self.quote(user=self.other)
        results = self.simultaneous([lambda: self.redeem(first), lambda: self.redeem(second, self.other)], reward_lock=True)
        self.assertEqual(results.count('out_of_stock'), 1)
        self.assertEqual(RewardFulfillment.objects.count(), 1)
        self.assertCountEqual([self.balance(), self.balance(self.other)], [160, 260])
        self.assertEqual(Reward.objects.get(pk=self.reward.pk).stock_remaining, 0)

    def test_duplicate_confirmation_reserves_and_creates_once(self):
        quote = self.quote()
        results = self.simultaneous([lambda: self.redeem(quote), lambda: self.redeem(quote)])
        self.assertCountEqual([r.replayed for r in results], [True, False])
        self.assertEqual(results[0].redemption_id, results[1].redemption_id)
        self.assertEqual(RewardFulfillment.objects.count(), 1)
        self.assertEqual(Reward.objects.get(pk=self.reward.pk).stock_remaining, 2)
        self.assertEqual(self.balance(), 160)

    def test_two_confirmations_cannot_share_one_balance(self):
        CustomerPoints.objects.filter(user=self.user).update(total_points=160)
        first, second = self.quote(), self.quote()
        results = self.simultaneous([lambda: self.redeem(first), lambda: self.redeem(second)])
        self.assertEqual(results.count('insufficient_points'), 1)
        self.assertEqual(RewardFulfillment.objects.count(), 1)
        self.assertEqual(self.balance(), 60)

    def test_lock_order_matches_points_services(self):
        quote = self.quote()
        with CaptureQueriesContext(connection) as queries:
            self.redeem(quote)
        locks = [q['sql'] for q in queries if 'FOR UPDATE' in q['sql']]
        self.assertEqual(len(locks), 4)
        for sql, model in zip(locks, (get_user_model(), CustomerPoints, Reward, Redemption)):
            self.assertIn('FROM "' + model._meta.db_table + '"', sql)

    def test_daily_or_bonus_winner_requires_confirmation_review(self):
        for operation in (claim_daily_points, claim_streak_bonus):
            CustomerPoints.objects.filter(user=self.user).update(total_points=260, streak_days=7,
                last_daily_claim_date=NOW.date() - timedelta(days=1), day_7_bonus_awarded=False)
            quote = self.quote()
            attempted = Event()
            with ThreadPoolExecutor(max_workers=1) as pool:
                with transaction.atomic():
                    get_user_model().objects.select_for_update().get(pk=self.user.pk)
                    future = pool.submit(self.worker, lambda: self.redeem(quote), attempted, get_user_model()._meta.db_table)
                    self.assertTrue(attempted.wait(10))
                    operation(self.user)
                self.assertEqual(future.result(timeout=20), 'balance_changed')
            self.assertFalse(RewardFulfillment.objects.exists())

    def test_bonus_after_redemption_reads_committed_deduction(self):
        attempted = Event()
        with ThreadPoolExecutor(max_workers=1) as pool:
            with transaction.atomic():
                self.redeem(self.quote())
                future = pool.submit(self.worker, lambda: claim_streak_bonus(self.user), attempted, get_user_model()._meta.db_table)
                self.assertTrue(attempted.wait(10))
            self.assertEqual(future.result(timeout=20).awarded_amount, 35)
        self.assertEqual(self.balance(), 195)
