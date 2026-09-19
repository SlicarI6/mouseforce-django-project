"""Customer transport, privacy and commit boundaries using test-only inventory."""
from datetime import timedelta
from unittest.mock import patch
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.db import IntegrityError, connection, transaction
from django.test import Client, TestCase, TransactionTestCase, override_settings, skipUnlessDBFeature
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from .models import CustomerPoints, Redemption, RedemptionEvent, Reward, RewardCode
from .reward_confirmation import material_fingerprint, read_confirmation_token
from .reward_redemption import PrivateCodeAllocation
from .test_reward_allocation import DigitalFixtures, NOW, TEST_KEYS
from . import test_reward_allocation as allocation_tests
from .test_reward_models import make_reward


class CustomerActionFixtures(DigitalFixtures):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.user)
        self.code, self.secret = self.inventory(expires_at=NOW + timedelta(days=10))

    def url(self, name, reward=None):
        return reverse('customer_reward_' + name, args=[(reward or self.reward).pk])

    def confirmation(self, reward=None, client=None):
        return (client or self.client).get(self.url('confirm', reward)).context['confirmation']

    def post(self, quote=None, client=None, **extra):
        quote = quote or self.confirmation()
        return (client or self.client).post(reverse('customer_reward_redeem', args=[quote.reward_id]),
            {'confirmation_token': quote.token, **extra}, HTTP_ACCEPT='application/json')

    def result_url(self):
        return reverse('customer_redemption_result', args=[Redemption.objects.get().pk])

    def reveal_url(self):
        return reverse('customer_redemption_reveal', args=[Redemption.objects.get().pk])


@override_settings(**TEST_KEYS)
class CustomerRewardActionTests(CustomerActionFixtures, TestCase):
    def test_confirmation_uses_exact_signed_public_snapshot_and_does_not_spend(self):
        response = self.client.get(self.url('confirm'), {'points': 1, 'user_id': self.other.pk})
        quote = response.context['confirmation']
        claims = read_confirmation_token(self.user, quote.token, session_key=self.client.session.session_key)
        self.assertEqual(claims.fingerprint, material_fingerprint(self.reward, quote.promised_expiry))
        self.assertEqual(quote.details['terms'], self.reward.terms)
        for text in (self.reward.title, '100 Points', '260 Points', '160 Points', self.reward.terms, 'normally cannot be refunded'):
            self.assertContains(response, text)
        self.assert_unspent()

    def test_quote_display_does_not_reread_an_edited_offer(self):
        from .reward_confirmation import create_reward_confirmation
        def create_and_edit(*args, **kwargs):
            quote = create_reward_confirmation(*args, **kwargs)
            Reward.objects.filter(pk=self.reward.pk).update(title='Changed after quote', terms='Other terms')
            return quote
        with patch('mouse_force_first_step.customerpanel.views.create_reward_confirmation', side_effect=create_and_edit):
            response = self.client.get(self.url('confirm'))
        self.assertContains(response, self.reward.title)
        self.assertNotContains(response, 'Changed after quote')
        self.assertEqual(self.post(response.context['confirmation']).status_code, 409)
        self.assert_unspent()

    def test_detail_cta_supports_all_four_fulfillment_types(self):
        self.assertContains(self.client.get(self.url('detail')), self.url('confirm'))
        for kind in ('physical', 'manual'):
            reward = make_reward(is_active=True, fulfillment_type=kind)
            self.assertContains(self.client.get(self.url('detail', reward)), self.url('confirm', reward))
            response = self.client.get(self.url('confirm', reward))
            self.assertContains(response, 'name="confirmation_token"')
        self.assert_unspent()

    def test_voucher_post_uses_service_once_and_returns_private_free_result(self):
        before = CustomerPoints.objects.values().get(user=self.user)
        response = self.post()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['redirect_url'], self.result_url())
        self.code.refresh_from_db()
        self.assertEqual(self.code.redemption_id, Redemption.objects.get().pk)
        self.assertEqual(CustomerPoints.objects.values().get(user=self.user), {**before, 'total_points': 160})
        self.assertNotContains(response, self.secret)
        result = self.client.get(self.result_url())
        for text in (self.reward.title, 'Your reward is ready', '100 Points', '160 Points', str(self.code.redemption_id)):
            self.assertContains(result, text)
        self.assertNotContains(result, self.secret)

    def test_external_public_information_url_is_not_the_entitlement(self):
        reward = make_reward(is_active=True, fulfillment_type='external', information_url='https://partner.example/public')
        response = self.client.get(self.url('confirm', reward))
        self.assertNotContains(response, 'name="confirmation_token"')
        code, secret = self.inventory(reward)
        response = self.post(self.confirmation(reward))
        self.assertEqual(response.status_code, 200)
        record = Redemption.objects.get()
        self.assertEqual(record.private_code.pk, code.pk)
        self.assertNotContains(response, secret)
        self.assertNotContains(self.client.get(self.result_url()), secret)

    def test_double_post_and_lost_response_retry_return_same_result(self):
        self.inventory()
        quote = self.confirmation()
        first = self.post(quote).json()
        for _ in range(3):
            replay = self.post(quote)
            self.assertEqual(replay.json()['redirect_url'], first['redirect_url'])
            self.assertTrue(replay.json()['replayed'])
        self.assertEqual(self.balance(), 160)
        self.assertEqual(Redemption.objects.count(), 1)
        self.assertEqual(RewardCode.objects.filter(redemption__isnull=False).count(), 1)
        self.assertEqual(RedemptionEvent.objects.count(), 1)

    def test_committed_retry_works_after_token_expiry_and_reward_withdrawal(self):
        with patch('django.core.signing.time.time', return_value=1000000):
            quote = self.confirmation()
            first = self.post(quote)
        Reward.objects.filter(pk=self.reward.pk).update(is_active=False)
        with patch('django.core.signing.time.time', return_value=1001000):
            replay = self.post(quote)
        self.assertEqual(first.json()['redirect_url'], replay.json()['redirect_url'])
        self.assertEqual(self.balance(), 160)

    def test_normal_form_uses_post_redirect_get(self):
        response = self.client.post(self.url('redeem'), {'confirmation_token': self.confirmation().token})
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response['Location'], self.result_url())
        self.assertIn('no-store', response['Cache-Control'])

    def test_get_cannot_purchase_or_reveal_and_post_cannot_render_confirmation(self):
        self.assertEqual(self.client.get(self.url('redeem')).status_code, 405)
        self.assertEqual(self.client.post(self.url('confirm')).status_code, 405)
        reveal = reverse('customer_redemption_reveal', args=[uuid4()])
        self.assertEqual(self.client.get(reveal).status_code, 405)
        self.assert_unspent()

    def test_csrf_protects_both_post_actions(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.user)
        quote = self.confirmation(client=client)
        data = {'confirmation_token': quote.token}
        self.assertEqual(client.post(self.url('redeem'), data).status_code, 403)
        data['csrfmiddlewaretoken'] = client.cookies['csrftoken'].value
        self.assertEqual(client.post(self.url('redeem'), data).status_code, 303)
        self.assertEqual(client.post(self.reveal_url()).status_code, 403)
        self.assertEqual(client.post(self.reveal_url(), {'csrfmiddlewaretoken': data['csrfmiddlewaretoken']}).status_code, 409)  # TestCase outer transaction

    def test_rejects_browser_supplied_amount_customer_stock_and_dates(self):
        quote = self.confirmation()
        for field in ('points', 'balance', 'user_id', 'stock', 'date', 'idempotency_key', 'amount'):
            self.assertEqual(self.post(quote, **{field: '1'}).status_code, 400)
        duplicate = {'confirmation_token': [quote.token, quote.token]}
        self.assertEqual(self.client.post(self.url('redeem'), duplicate).status_code, 400)
        self.assertEqual(self.client.post(self.url('redeem'), '{}', content_type='application/json').status_code, 400)
        self.assert_unspent()

    def test_tampered_and_expired_tokens_require_new_review(self):
        with patch('django.core.signing.time.time', return_value=1000000):
            quote = self.confirmation()
        response = self.client.post(self.url('redeem'), {'confirmation_token': quote.token + 'x'}, HTTP_ACCEPT='application/json')
        self.assertEqual(response.status_code, 409)
        self.assertIn('invalid_confirmation', response.json()['review_url'])
        with patch('django.core.signing.time.time', return_value=1001000):
            response = self.post(quote)
        self.assertEqual(response.status_code, 409)
        self.assertIn('expired_confirmation', response.json()['review_url'])
        self.assert_unspent()

    def test_changed_price_terms_and_balance_require_fresh_confirmation(self):
        for changes in ({'points_required': 120}, {'terms': 'Updated conditions'}):
            quote = self.confirmation()
            Reward.objects.filter(pk=self.reward.pk).update(**changes)
            response = self.post(quote)
            self.assertEqual(response.status_code, 409)
            self.assertIn('offer_changed', response.json()['review_url'])
            fresh = self.client.get(response.json()['review_url'])
            self.assertContains(fresh, 'Review the updated details')
        quote = self.confirmation()
        CustomerPoints.objects.filter(user=self.user).update(total_points=270)
        response = self.post(quote)
        self.assertEqual(response.status_code, 409)
        self.assertIn('balance_changed', response.json()['review_url'])
        self.assertFalse(Redemption.objects.exists())

    def test_insufficient_balance_no_charge(self):
        quote = self.confirmation()
        CustomerPoints.objects.filter(user=self.user).update(total_points=5)
        response = self.post(quote)
        self.assertEqual(response.status_code, 409)
        self.assertIn('insufficient_points', response.json()['review_url'])
        self.assertContains(self.client.get(response.json()['review_url']), 'not have enough Points')
        self.assertEqual(self.balance(), 5)
        self.assertFalse(Redemption.objects.exists())

    def test_revoked_publication_expiry_eligibility_or_inventory_do_not_charge(self):
        for changes in ({'is_active': False}, {'valid_until': NOW}, {'valid_from': NOW + timedelta(days=1)},
                        {'access_scope': 'selected_customers'}, {'benefit_valid_until': NOW}):
            with self.subTest(changes=changes):
                quote = self.confirmation()
                Reward.objects.filter(pk=self.reward.pk).update(**changes)
                self.assertEqual(self.post(quote).status_code, 409)
                self.assertNotContains(self.client.get(self.url('confirm')), 'name="confirmation_token"')
                Reward.objects.filter(pk=self.reward.pk).update(is_active=True, valid_until=None,
                    valid_from=None, access_scope='all_customers', benefit_valid_until=None)
        quote = self.confirmation()
        RewardCode.objects.filter(pk=self.code.pk).update(is_active=False)
        self.assertIn('out_of_stock', self.post(quote).json()['review_url'])
        self.assert_unspent()

    def test_wrong_customer_session_and_unknown_redemptions_stay_private(self):
        quote = self.confirmation()
        other = Client()
        other.force_login(self.other)
        self.assertEqual(self.post(quote, client=other).status_code, 409)
        second_session = Client()
        second_session.force_login(self.user)
        self.assertEqual(self.post(quote, client=second_session).status_code, 409)
        self.post(quote)
        self.assertEqual(other.get(self.result_url()).status_code, 404)
        # Under TestCase the precommit boundary intentionally denies all reveals.
        self.assertNotIn(self.secret, other.post(self.reveal_url()).content.decode())
        self.assertEqual(other.get(reverse('customer_redemption_result', args=[uuid4()])).status_code, 404)

    def test_active_customer_only(self):
        self.client.logout()
        self.assertEqual(self.client.get(self.url('confirm')).status_code, 302)
        self.assertEqual(self.client.post(self.url('redeem')).status_code, 403)
        for active, role in ((True, 'user'), (False, 'customer')):
            get_user_model().objects.filter(pk=self.user.pk).update(is_active=active, role=role)
            self.user.refresh_from_db()
            self.client.force_login(self.user)
            self.assertIn(self.client.get(self.url('confirm')).status_code, (302, 403))
            self.assertEqual(self.client.post(self.url('redeem')).status_code, 403)
        self.assert_unspent()

    def test_database_failure_rolls_back_and_same_token_can_retry(self):
        quote = self.confirmation()
        reserve = PrivateCodeAllocation.reserve
        def fail_after_assignment(plan, record):
            reserve(plan, record)
            raise IntegrityError('Test database failure')
        with patch.object(PrivateCodeAllocation, 'reserve', fail_after_assignment):
            response = self.post(quote)
        self.assertEqual(response.status_code, 503)
        self.assert_unspent()
        self.assertEqual(self.post(quote).status_code, 200)
        self.assertEqual(self.balance(), 160)

    def test_public_responses_and_result_never_load_private_code_columns(self):
        quote = self.confirmation()
        for url in (reverse('customer_rewards'), self.url('detail'), self.url('confirm')):
            with CaptureQueriesContext(connection) as queries:
                response = self.client.get(url)
            self.assertNotContains(response, self.secret)
            for sql in queries:
                self.assertNotIn('encrypted_payload', sql['sql'])
                self.assertNotIn('payload_fingerprint', sql['sql'])
        self.post(quote)
        with CaptureQueriesContext(connection) as queries:
            result = self.client.get(self.result_url())
        self.assertNotContains(result, self.secret)
        self.assertTrue(all('encrypted_payload' not in item['sql'] for item in queries))

    def test_persistent_shell_routes_assets_and_private_cache_headers(self):
        confirmation = self.client.get(self.url('confirm'), HTTP_X_CUSTOMER_NAVIGATION='1')
        self.post(confirmation.context['confirmation'])
        result = self.client.get(self.result_url(), HTTP_X_CUSTOMER_NAVIGATION='1')
        for response, page in ((confirmation, 'reward_confirm'), (result, 'redemption_result')):
            self.assertContains(response, f'data-page="{page}"')
            self.assertContains(response, 'customer_reward_actions.css')
            self.assertContains(response, 'customer_reward_actions.js')
            self.assertContains(response, 'class="customer-music-audio"')
            self.assertContains(response, 'href="/customer/rewards/" aria-current="page"')
            self.assertIn('no-store', response['Cache-Control'])
            self.assertEqual(response['Referrer-Policy'], 'no-referrer')
        self.assertEqual(confirmation.wsgi_request.customer_shell_session, result.wsgi_request.customer_shell_session)


@skipUnlessDBFeature('has_select_for_update')
@override_settings(**TEST_KEYS)
class CustomerPrivateRevealTests(CustomerActionFixtures, TransactionTestCase):
    available_apps = ['django.contrib.auth', 'django.contrib.contenttypes', 'django.contrib.sessions',
                      'mouse_force_first_step.accounts', 'mouse_force_first_step.customerpanel']

    def setUp(self):
        self.setUpTestData()
        super().setUp()

    def test_committed_voucher_and_external_reveal_repeat_without_spending(self):
        for kind in ('voucher', 'external'):
            reward = self.reward if kind == 'voucher' else make_reward(is_active=True, fulfillment_type='external')
            code, secret = (self.code, self.secret) if kind == 'voucher' else self.inventory(reward)
            result = self.post(self.confirmation(reward))
            self.assertEqual(result.status_code, 200)
            code.refresh_from_db()
            url = reverse('customer_redemption_reveal', args=[code.redemption_id])
            before = self.balance()
            for _ in range(2):
                response = self.client.post(url)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()['value'], secret)
                self.assertEqual(response.json()['kind'], 'code' if kind == 'voucher' else 'claim_link')
                self.assertIn('no-store', response['Cache-Control'])
                self.assertEqual(response['Referrer-Policy'], 'no-referrer')
                self.assertEqual(self.balance(), before)
            other = Client()
            other.force_login(self.other)
            self.assertEqual(other.post(url).status_code, 404)
            self.assertEqual(other.get(result.json()['redirect_url']).status_code, 404)
            self.assertEqual(self.client.get(url).status_code, 405)
            self.assertEqual(self.client.post(url, {'user_id': self.user.pk}).status_code, 400)

    def test_reveal_cannot_decrypt_before_outermost_commit(self):
        quote = self.confirmation()
        with transaction.atomic():
            self.post(quote)
            with patch('mouse_force_first_step.customerpanel.reward_private_access._decode_benefit') as decrypt:
                response = self.client.post(self.reveal_url())
                self.assertEqual(response.status_code, 409)
                decrypt.assert_not_called()
        self.assertEqual(self.client.post(self.reveal_url()).json()['value'], self.secret)

    def test_csrf_reveal_and_revoked_account(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.user)
        quote = self.confirmation(client=client)
        csrf = client.cookies['csrftoken'].value
        self.assertEqual(client.post(self.url('redeem'), {'confirmation_token': quote.token, 'csrfmiddlewaretoken': csrf}).status_code, 303)
        self.assertEqual(client.post(self.reveal_url()).status_code, 403)
        self.assertEqual(client.post(self.reveal_url(), {'csrfmiddlewaretoken': csrf}).status_code, 200)
        get_user_model().objects.filter(pk=self.user.pk).update(is_active=False)
        self.assertEqual(client.post(self.reveal_url(), {'csrfmiddlewaretoken': csrf}).status_code, 403)


@skipUnlessDBFeature('has_select_for_update')
@override_settings(**TEST_KEYS)
class CustomerPurchaseConcurrencyTests(CustomerActionFixtures, TransactionTestCase):
    # Reuse only the two synchronization helpers, not the service test suite.
    available_apps = CustomerPrivateRevealTests.available_apps
    worker = allocation_tests.DigitalCommitAndConcurrencyTests.worker
    simultaneous = allocation_tests.DigitalCommitAndConcurrencyTests.simultaneous

    def setUp(self):
        self.setUpTestData()
        super().setUp()

    def test_concurrent_http_duplicate_posts_share_one_redemption(self):
        quote = self.confirmation()
        clients = [Client(), Client()]
        for client in clients:
            client.cookies = self.client.cookies.copy()
        responses = self.simultaneous([lambda: self.post(quote, client=clients[0]), lambda: self.post(quote, client=clients[1])])
        self.assertEqual([r.status_code for r in responses], [200, 200])
        self.assertEqual(responses[0].json()['redirect_url'], responses[1].json()['redirect_url'])
        self.assertEqual(Redemption.objects.count(), 1)
        self.assertEqual(self.balance(), 160)
