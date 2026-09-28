from .section_test_support import seed_paid_access
"""Request submission/review never spend; only explicit bound acceptance does."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Event
from unittest.mock import patch
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.core import signing
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import connection, transaction
from django.test import Client, TestCase, TransactionTestCase, override_settings, skipUnlessDBFeature
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from . import test_reward_allocation as digital
from .models import CustomerPoints, Reward, RewardRequest, Redemption, RedemptionEvent, RewardFulfillment
from .points import claim_daily_points, claim_streak_bonus
from .reward_confirmation import create_reward_confirmation, ConfirmationError, TOKEN_SALT
from .reward_redemption import redeem_reward, FulfillmentAllocation
from .reward_requests import submission_token, submit_reward_request, review_token, review_reward_request

NOW, SESSION = digital.NOW, digital.SESSION


class RequestFixtures(digital.DigitalFixtures):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        Reward.objects.filter(pk=cls.reward.pk).update(fulfillment_type='manual', stock_remaining=3, access_scope='selected_customers')
        cls.reward.refresh_from_db()
        cls.reward.eligible_users.add(cls.user)

    def submit(self, user=None):
        user = user or self.user
        record, form = submit_reward_request(user, {'title': 'A custom experience', 'category': 'experiences',
            'description': 'A private request description', 'submission_token': submission_token(user, SESSION)}, session_key=SESSION)
        self.assertFalse(form.errors)
        return record

    def approve(self, record, **changes):
        return review_reward_request(self.staff, record.pk, status=changes.pop('status', 'approved'),
            approved_reward=changes.pop('approved_reward', self.reward), review_token=review_token(record), **changes)

    def request_quote(self, record, user=None):
        return create_reward_confirmation(user or self.user, self.reward.pk, session_key=SESSION, request_id=record.pk)


class RequestServiceTests(RequestFixtures, TestCase):
    def test_submission_and_approval_have_zero_spending_or_inventory_effect(self):
        before = CustomerPoints.objects.values().get(user=self.user)
        record = self.submit()
        self.assertEqual((record.user, record.status), (self.user, 'submitted'))
        self.approve(record)
        record.refresh_from_db()
        self.assertEqual(record.approved_reward_id, self.reward.pk)
        self.assertEqual(CustomerPoints.objects.values().get(user=self.user), before)
        self.assertEqual(Reward.objects.get(pk=self.reward.pk).stock_remaining, 3)
        self.assertFalse(Redemption.objects.exists())
        self.assertFalse(RewardFulfillment.objects.exists())
        self.assertFalse(RedemptionEvent.objects.exists())

    def test_submission_retry_creates_one_request(self):
        data = {'title': 'Retry', 'description': 'Details', 'submission_token': submission_token(self.user, SESSION)}
        first, _ = submit_reward_request(self.user, data, session_key=SESSION)
        second, _ = submit_reward_request(self.user, data, session_key=SESSION)
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(RewardRequest.objects.count(), 1)

    def test_submission_token_owner_session_and_signature_are_checked(self):
        token = submission_token(self.user, SESSION)
        for user, session, value in ((self.other, SESSION, token), (self.user, 'wrong', token), (self.user, SESSION, token + 'x')):
            record, form = submit_reward_request(user, {'title': 'Test', 'description': 'Details', 'submission_token': value}, session_key=session)
            self.assertIsNone(record)
            self.assertTrue(form.errors)
        self.assertFalse(RewardRequest.objects.exists())

    def test_review_permission_and_restricted_offer_guard(self):
        record = self.submit()
        with self.assertRaises(PermissionDenied):
            review_reward_request(self.user, record.pk, status='approved', approved_reward=self.reward, review_token=review_token(record))
        Reward.objects.filter(pk=self.reward.pk).update(access_scope='all_customers')
        with self.assertRaises(ValidationError):
            self.approve(record)
        self.approve(record, allow_shared_offer=True)
        self.assert_unspent()

    def test_review_stale_form_and_accepted_edits_rejected(self):
        record = self.submit()
        token = review_token(record)
        with patch('django.utils.timezone.now', return_value=NOW + timedelta(seconds=1)):
            self.approve(record)
        with self.assertRaises(ValidationError):
            review_reward_request(self.staff, record.pk, status='closed', review_token=token)
        record.refresh_from_db()
        self.redeem(self.request_quote(record))
        record.refresh_from_db()
        with self.assertRaises(ValidationError):
            self.approve(record, status='closed')

    def test_approval_and_confirmation_do_not_accept(self):
        record = self.approve(self.submit())
        quote = self.request_quote(record)
        self.assert_unspent()
        record.refresh_from_db()
        self.assertIsNone(record.redemption_id)
        payload = signing.loads(quote.token, salt=TOKEN_SALT)
        self.assertEqual(payload['request'], str(record.pk))
        self.assertNotIn(record.description, repr(payload))

    def test_acceptance_links_once_even_with_two_confirmations(self):
        record = self.approve(self.submit())
        one, two = self.request_quote(record), self.request_quote(record)
        result = self.redeem(one)
        for quote in (one, two):
            retry = self.redeem(quote)
            self.assertTrue(retry.replayed)
            self.assertEqual(retry.redemption_id, result.redemption_id)
        record.refresh_from_db()
        self.assertEqual(record.redemption_id, result.redemption_id)
        self.assertEqual(Redemption.objects.count(), 1)
        self.assertEqual(RewardFulfillment.objects.count(), 1)
        self.assertEqual(self.balance(), 160)
        self.assertEqual(Reward.objects.get(pk=self.reward.pk).stock_remaining, 2)

    def test_wrong_customer_cannot_quote_or_accept(self):
        record = self.approve(self.submit())
        with self.assertRaises(PermissionDenied):
            self.request_quote(record, self.other)
        quote = self.request_quote(record)
        self.assert_error('wrong_confirmation_owner', lambda: self.redeem(quote, self.other))
        self.assert_unspent()

    def test_unapproved_declined_closed_and_wrong_reward_rejected(self):
        record = self.submit()
        for status in ('submitted', 'in_review', 'declined', 'closed'):
            RewardRequest.objects.filter(pk=record.pk).update(status=status, approved_reward=self.reward)
            self.assert_error('request_unavailable', lambda: self.request_quote(record))
        RewardRequest.objects.filter(pk=record.pk).update(status='approved', approved_reward_id=self.reward.pk)
        from .test_reward_models import make_reward
        wrong = make_reward(is_active=True, fulfillment_type='manual')
        self.assert_error('request_unavailable', lambda: create_reward_confirmation(self.user, wrong.pk, session_key=SESSION, request_id=record.pk))
        self.assert_unspent()

    def test_revoked_or_changed_offer_requires_fresh_confirmation(self):
        record = self.approve(self.submit())
        quote = self.request_quote(record)
        with patch('django.utils.timezone.now', return_value=NOW + timedelta(seconds=1)):
            record = self.approve(record, staff_response='Updated review')
        self.assert_error('request_changed', lambda: self.redeem(quote))
        quote = self.request_quote(record)
        self.approve(record, status='declined')
        self.assert_error('request_unavailable', lambda: self.redeem(quote))
        self.assert_unspent()

    def test_current_reward_state_checked_at_acceptance(self):
        record = self.approve(self.submit())
        quote = self.request_quote(record)
        for changes in ({'is_active': False}, {'valid_until': NOW - timedelta(seconds=1)}, {'valid_from': NOW + timedelta(days=1)}):
            with transaction.atomic():
                Reward.objects.filter(pk=self.reward.pk).update(**changes)
                self.assert_error('reward_unavailable', lambda: self.redeem(quote))
                transaction.set_rollback(True)
        self.reward.eligible_users.clear()
        self.assert_error('reward_unavailable', lambda: self.redeem(quote))
        self.assert_unspent()

    def test_insufficient_balance_stock_and_limits_leave_request_unaccepted(self):
        record = self.approve(self.submit())
        quote = self.request_quote(record)
        CustomerPoints.objects.filter(user=self.user).update(total_points=50)
        self.assert_error('insufficient_points', lambda: self.redeem(quote))
        CustomerPoints.objects.filter(user=self.user).update(total_points=260)
        Reward.objects.filter(pk=self.reward.pk).update(stock_remaining=0)
        self.assert_error('out_of_stock', lambda: self.redeem(quote))
        record.refresh_from_db()
        self.assertIsNone(record.redemption_id)
        self.assertFalse(Redemption.objects.exists())

    def test_full_rollback_if_linking_request_fails_after_allocation(self):
        record = self.approve(self.submit())
        quote = self.request_quote(record)
        with patch.object(RewardRequest, 'save', side_effect=RuntimeError('isolated failure')):
            with self.assertRaises(RuntimeError):
                self.redeem(quote)
        self.assert_unspent()
        self.assertEqual(Reward.objects.get(pk=self.reward.pk).stock_remaining, 3)
        self.assertFalse(RewardFulfillment.objects.exists())
        record.refresh_from_db()
        self.assertIsNone(record.redemption_id)

    @override_settings(**digital.TEST_KEYS)
    def test_voucher_and_external_request_acceptance_reuses_private_allocation(self):
        for kind in ('voucher', 'external'):
            with transaction.atomic():
                Reward.objects.filter(pk=self.reward.pk).update(fulfillment_type=kind, stock_remaining=None)
                self.reward.refresh_from_db()
                code, secret = self.inventory()
                record = self.approve(self.submit())
                result = self.redeem(self.request_quote(record))
                self.assertEqual(Redemption.objects.get(pk=result.redemption_id).private_code.pk, code.pk)
                self.assertNotIn(secret, repr(result))
                transaction.set_rollback(True)
        self.reward.refresh_from_db()


class RequestPageTests(RequestFixtures, TestCase):
    def setUp(self):
        super().setUp()
        seed_paid_access(self.user, self.other)
        self.client.force_login(self.user)

    def new(self, client=None):
        return (client or self.client).get(reverse('customer_reward_request_new'))

    def test_submit_csrf_and_successful_persistent_response(self):
        client = Client(enforce_csrf_checks=True); client.force_login(self.user)
        page = self.new(client)
        data = {'title': 'My request', 'description': 'Private description', 'submission_token': page.context['form'].initial['submission_token']}
        url = reverse('customer_reward_request_new')
        self.assertEqual(client.post(url, data).status_code, 403)
        data['csrfmiddlewaretoken'] = client.cookies['csrftoken'].value
        response = client.post(url, data, HTTP_ACCEPT='application/json')
        self.assertEqual(response.status_code, 200)
        result = client.get(response.json()['redirect_url'])
        self.assertContains(result, 'Your request has been submitted for review.')
        self.assertIn('no-store', result['Cache-Control'])
        self.assertEqual(result['Referrer-Policy'], 'no-referrer')
        self.assert_unspent()

    def test_extra_authoritative_fields_are_rejected(self):
        token = self.new().context['form'].initial['submission_token']
        for key in ('user', 'status', 'approved_reward', 'points_required', 'redemption', 'internal_notes'):
            response = self.client.post(reverse('customer_reward_request_new'), {'title': 'Title', 'description': 'Details', 'submission_token': token, key: '1'})
            self.assertEqual(response.status_code, 400)
        self.assertFalse(RewardRequest.objects.exists())

    def test_invalid_input_and_anonymous_inactive_noncustomer(self):
        token = self.new().context['form'].initial['submission_token']
        response = self.client.post(reverse('customer_reward_request_new'), {'title': '', 'description': '', 'submission_token': token}, HTTP_ACCEPT='application/json')
        self.assertEqual(response.status_code, 422)
        self.client.logout()
        self.assertEqual(self.new().status_code, 302)
        for changes in ({'role': 'simple'}, {'role': 'customer', 'is_active': False}):
            get_user_model().objects.filter(pk=self.user.pk).update(**changes)
            self.client.force_login(self.user)
            self.assertIn(self.new().status_code, (302, 403))

    def test_owned_list_detail_private_notes_and_guessing_protection(self):
        record = self.approve(self.submit(), internal_notes='STAFF-ONLY-SECRET', staff_response='Customer response')
        other_record = self.submit(self.other)
        detail = reverse('customer_reward_request_detail', args=[record.pk])
        for url in (reverse('customer_reward_requests'), detail):
            with CaptureQueriesContext(connection) as queries:
                response = self.client.get(url)
            self.assertNotContains(response, 'STAFF-ONLY-SECRET')
            self.assertTrue(all('internal_notes' not in q['sql'] for q in queries))
        self.assertContains(self.client.get(detail), 'Customer response')
        self.assertEqual(self.client.get(reverse('customer_reward_request_detail', args=[other_record.pk])).status_code, 404)
        self.assertEqual(self.client.get(reverse('customer_reward_request_confirm', args=[other_record.pk])).status_code, 404)

    def test_offer_confirmation_existing_post_result_and_retry(self):
        record = self.approve(self.submit())
        page = self.client.get(reverse('customer_reward_request_confirm', args=[record.pk]))
        self.assertEqual(page.status_code, 200)
        self.assert_unspent()
        quote = page.context['confirmation']
        url = reverse('customer_reward_redeem', args=[self.reward.pk])
        data = {'confirmation_token': quote.token}
        first = self.client.post(url, data, HTTP_ACCEPT='application/json')
        second = self.client.post(url, data, HTTP_ACCEPT='application/json')
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.json()['redirect_url'], second.json()['redirect_url'])
        self.assertTrue(second.json()['replayed'])
        self.assertEqual(self.balance(), 160)
        self.assertContains(self.client.get(reverse('customer_reward_request_detail', args=[record.pk])), 'Reward accepted')

    def test_changed_confirmation_review_preserves_request_binding(self):
        record = self.approve(self.submit())
        quote = self.client.get(reverse('customer_reward_request_confirm', args=[record.pk])).context['confirmation']
        CustomerPoints.objects.filter(user=self.user).update(total_points=300)
        response = self.client.post(reverse('customer_reward_redeem', args=[self.reward.pk]), {'confirmation_token': quote.token}, HTTP_ACCEPT='application/json')
        self.assertEqual(response.status_code, 409)
        self.assertIn(reverse('customer_reward_request_confirm', args=[record.pk]), response.json()['review_url'])

    def test_admin_review_permissions_and_approval(self):
        record = self.submit()
        url = reverse('admin:customerpanel_rewardrequest_review', args=[record.pk])
        self.assertEqual(self.client.get(url).status_code, 302)
        self.client.force_login(self.staff)
        form = self.client.get(url).context['form']
        response = self.client.post(url, {'review_token': form.initial['review_token'], 'status': 'approved',
            'approved_reward': self.reward.pk, 'internal_notes': 'Staff private', 'staff_response': 'An offer for you'})
        self.assertEqual(response.status_code, 302)
        record.refresh_from_db()
        self.assertEqual((record.status, record.approved_reward_id), ('approved', self.reward.pk))
        self.assert_unspent()

    def test_navigation_and_scoped_assets_present(self):
        self.assertContains(self.client.get(reverse('customer_rewards')), reverse('customer_reward_request_new'))
        for page in (self.new(), self.client.get(reverse('customer_reward_requests'))):
            self.assertContains(page, 'customer_reward_actions.css')
            self.assertContains(page, 'customer_reward_actions.js')
            self.assertContains(page, 'customer-music-audio')


@skipUnlessDBFeature('has_select_for_update')
class RequestConcurrencyTests(RequestFixtures, TransactionTestCase):
    available_apps = digital.DigitalCommitAndConcurrencyTests.available_apps
    worker = digital.DigitalCommitAndConcurrencyTests.worker
    simultaneous = digital.DigitalCommitAndConcurrencyTests.simultaneous

    def setUp(self):
        self.setUpTestData()
        super().setUp()

    def test_two_distinct_confirmation_intents_accept_request_once(self):
        record = self.approve(self.submit())
        one, two = self.request_quote(record), self.request_quote(record)
        results = self.simultaneous([lambda: self.redeem(one), lambda: self.redeem(two)])
        self.assertCountEqual([r.replayed for r in results], [False, True])
        self.assertEqual(results[0].redemption_id, results[1].redemption_id)
        self.assertEqual(self.balance(), 160)
        self.assertEqual(RewardFulfillment.objects.count(), 1)

    def test_request_lock_is_between_reward_and_redemption(self):
        record = self.approve(self.submit())
        quote = self.request_quote(record)
        with CaptureQueriesContext(connection) as queries:
            self.redeem(quote)
        locks = [q['sql'] for q in queries if 'FOR UPDATE' in q['sql']]
        self.assertEqual(len(locks), 5)
        for sql, model in zip(locks, (get_user_model(), CustomerPoints, Reward, RewardRequest, Redemption)):
            self.assertIn('FROM "' + model._meta.db_table + '"', sql)

    def test_daily_and_bonus_races_require_fresh_quote_without_lost_points(self):
        record = self.approve(self.submit())
        for operation in (claim_daily_points, claim_streak_bonus):
            CustomerPoints.objects.filter(user=self.user).update(total_points=260, streak_days=7,
                last_daily_claim_date=NOW.date() - timedelta(days=1), day_7_bonus_awarded=False)
            quote = self.request_quote(record)
            attempted = Event()
            with ThreadPoolExecutor(max_workers=1) as pool:
                with transaction.atomic():
                    get_user_model().objects.select_for_update().get(pk=self.user.pk)
                    future = pool.submit(self.worker, lambda: self.redeem(quote), attempted, get_user_model()._meta.db_table)
                    self.assertTrue(attempted.wait(10))
                    operation(self.user)
                self.assertEqual(future.result(timeout=20), 'balance_changed')
            self.assertFalse(Redemption.objects.exists())

    def test_review_revocation_serializes_with_acceptance(self):
        record = self.approve(self.submit())
        quote = self.request_quote(record)
        attempted = Event()
        with ThreadPoolExecutor(max_workers=1) as pool:
            with transaction.atomic():
                get_user_model().objects.select_for_update().get(pk=self.user.pk)
                future = pool.submit(self.worker, lambda: self.redeem(quote), attempted, get_user_model()._meta.db_table)
                self.assertTrue(attempted.wait(10))
                self.approve(record, status='declined')
            self.assertEqual(future.result(timeout=20), 'request_unavailable')
        self.assert_unspent()
