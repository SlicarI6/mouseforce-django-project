from datetime import timedelta
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.core.exceptions import PermissionDenied
from django.db import connection
from django.test import RequestFactory, TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import resolve, reverse
from django.utils import timezone

from .models import CustomerPoints, Reward, RewardCode, Redemption, RewardRequest, RedemptionEvent
from .reward_catalogue import REWARD_CATEGORIES, published_rewards
from .test_reward_models import make_reward, make_code, make_redemption
from .views import customer_rewards, customer_reward_detail


class CustomerRewardsCatalogueTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_user(username='catalogue-customer', role='customer')
        cls.other = get_user_model().objects.create_user(username='other-customer', role='customer')
        cls.item = make_reward(title='Published reward', is_active=True, fulfillment_type='physical',
            stock_remaining=3, points_required=500, partner_name='MouseForce Partner',
            image_url='https://example.com/product.jpg', image_alt='Product selection',
            information_url='https://example.com/product', country_code='GB', region='London', city='London',
            location_details='Partner collection desk', fulfillment_instructions='Collect at the desk.')
        cls.affordable = make_reward(title='Affordable reward', is_active=True, fulfillment_type='manual', points_required=120)
        CustomerPoints.objects.create(user=cls.user, total_points=260, streak_days=7, last_daily_claim_date=timezone.now().date())

    def setUp(self):
        self.client.force_login(self.user)
        self.url = reverse('customer_rewards')

    def detail_url(self, reward=None):
        return reverse('customer_reward_detail', args=[(reward or self.item).pk])

    def cards(self, response=None):
        response = response if response is not None else self.client.get(self.url)
        return {row['id']: row for row in response.context['rewards']}

    def test_catalogue_route_template_and_real_cards(self):
        response = self.client.get(self.url)
        self.assertEqual(self.url, '/customer/rewards/')
        self.assertIs(resolve(self.url).func, customer_rewards)
        self.assertTemplateUsed(response, 'customer_rewards.html')
        self.assertTemplateUsed(response, 'customer_base.html')
        for value in [self.item.title, self.item.image_url, self.item.partner_name, self.detail_url()]:
            self.assertContains(response, value)
        self.assertNotContains(response, 'Demo rewards')
        self.assertNotContains(response, 'Self-care selection')
        self.assertEqual(set(self.cards(response)), {self.item.pk, self.affordable.pk})

    def test_empty_catalogue_does_not_fall_back_to_demos(self):
        Reward.objects.update(is_active=False)
        response = self.client.get(self.url)
        self.assertContains(response, 'New rewards are on the way. Keep collecting your points and check back soon.')
        self.assertNotContains(response, 'data-reward-card data-category=')
        self.assertNotContains(response, 'Demo rewards')

    def test_inactive_expired_future_and_ineligible_rewards_hidden_in_both_views(self):
        at = timezone.now()
        hidden = [make_reward(title='Inactive'), make_reward(title='Expired', is_active=True, valid_until=at),
            make_reward(title='Future', is_active=True, valid_from=at + timedelta(days=1)),
            make_reward(title='Private selection', is_active=True, access_scope='selected_customers', is_exclusive=True)]
        hidden[-1].eligible_users.add(self.other)
        response = self.client.get(self.url)
        for item in hidden:
            self.assertNotContains(response, item.title)
            self.assertEqual(self.client.get(self.detail_url(item)).status_code, 404)

    def test_window_start_inclusive_end_exclusive(self):
        at = timezone.now()
        Reward.objects.filter(pk=self.item.pk).update(valid_from=at, valid_until=at + timedelta(hours=1))
        self.assertIn(self.item.pk, {row['id'] for row in published_rewards(self.user, at=at)})
        self.assertNotIn(self.item.pk, {row['id'] for row in published_rewards(self.user, at=at + timedelta(hours=1))})

    def test_selected_customer_allowed_and_uuid_guessing_by_other_customer_denied(self):
        Reward.objects.filter(pk=self.item.pk).update(access_scope='selected_customers', is_exclusive=True)
        self.item.eligible_users.add(self.user)
        self.assertIn(self.item.pk, self.cards())
        self.assertContains(self.client.get(self.detail_url()), 'Exclusive')
        self.client.force_login(self.other)
        self.assertNotIn(self.item.pk, self.cards())
        self.assertEqual(self.client.get(self.detail_url(), {'user_id': self.user.pk, 'eligible': 1}).status_code, 404)

    def test_eligibility_revocation_applies_on_next_navigation(self):
        Reward.objects.filter(pk=self.item.pk).update(access_scope='selected_customers')
        self.item.eligible_users.add(self.user)
        self.assertEqual(self.client.get(self.detail_url()).status_code, 200)
        self.item.eligible_users.clear()
        self.assertEqual(self.client.get(self.detail_url(), HTTP_X_CUSTOMER_NAVIGATION='1').status_code, 404)

    def test_unknown_and_invalid_uuid(self):
        for value in (str(uuid4()), 'invalid', 'redeem'):
            self.assertEqual(self.client.get(self.url + value + '/').status_code, 404)

    def test_authenticated_balance_determines_state_ignoring_browser_values(self):
        response = self.client.get(self.url, {'points': 99999, 'user_id': self.other.pk, 'available': 1})
        cards = self.cards(response)
        self.assertEqual(cards[self.item.pk]['points_needed'], 240)
        self.assertFalse(cards[self.item.pk]['available'])
        self.assertTrue(cards[self.affordable.pk]['available'])
        self.assertContains(response, 'You need 240 more points')
        self.assertContains(response, '260 Points')

    def test_exact_balance_is_available(self):
        CustomerPoints.objects.filter(user=self.user).update(total_points=500)
        self.assertTrue(self.cards()[self.item.pk]['available'])
        self.assertNotContains(self.client.get(self.detail_url()), 'You need 0 more points')

    def test_missing_points_row_shows_zero_without_creating_row(self):
        CustomerPoints.objects.filter(user=self.user).delete()
        self.assertContains(self.client.get(self.url), '0 Points')
        self.assertContains(self.client.get(self.detail_url()), 'You need 500 more points')
        self.assertFalse(CustomerPoints.objects.filter(user=self.user).exists())

    def test_zero_stock_unavailable_unlimited_stock_available(self):
        CustomerPoints.objects.filter(user=self.user).update(total_points=500)
        Reward.objects.filter(pk=self.item.pk).update(stock_remaining=0)
        self.assertFalse(self.cards()[self.item.pk]['available'])
        self.assertContains(self.client.get(self.detail_url()), 'Currently unavailable')
        Reward.objects.filter(pk=self.item.pk).update(stock_remaining=None)
        self.assertTrue(self.cards()[self.item.pk]['available'])

    def test_voucher_requires_active_unassigned_unexpired_inventory(self):
        voucher = make_reward(title='Voucher stock', is_active=True)
        self.assertFalse(self.cards()[voucher.pk]['available'])
        make_code(voucher, is_active=False)
        make_code(voucher, expires_at=timezone.now() - timedelta(seconds=1))
        allocated = make_code(voucher)
        RewardCode.objects.filter(pk=allocated.pk).update(redemption=make_redemption(self.other, voucher), assigned_at=timezone.now())
        self.assertFalse(self.cards()[voucher.pk]['available'])
        make_code(voucher)
        self.assertTrue(self.cards()[voucher.pk]['available'])

    def test_external_requires_inventory_and_uses_friendly_label(self):
        reward = make_reward(is_active=True, fulfillment_type='external')
        self.assertFalse(self.cards()[reward.pk]['available'])
        make_code(reward, payload_kind='claim_link')
        self.assertTrue(self.cards()[reward.pk]['available'])
        self.assertContains(self.client.get(self.detail_url(reward)), 'Partner offer')

    def test_recorded_customer_limit_affects_display_only(self):
        make_redemption(self.user, self.affordable)
        before = CustomerPoints.objects.filter(user=self.user).values().get()
        self.assertEqual(self.cards()[self.affordable.pk]['status'], 'Reward limit reached')
        Reward.objects.filter(pk=self.affordable.pk).update(max_redemptions_per_customer=None)
        self.assertTrue(self.cards()[self.affordable.pk]['available'])
        self.assertEqual(CustomerPoints.objects.filter(user=self.user).values().get(), before)

    def test_detail_contains_public_information(self):
        at = timezone.now()
        Reward.objects.filter(pk=self.item.pk).update(valid_from=at - timedelta(days=1), valid_until=at + timedelta(days=2), benefit_valid_until=at + timedelta(days=5), is_limited_time=True)
        response = self.client.get(self.detail_url())
        self.assertTemplateUsed(response, 'customer_reward_detail.html')
        for value in [self.item.title, self.item.short_description, self.item.full_description, self.item.image_alt,
            self.item.partner_name, 'Physical product', self.item.location_details, self.item.terms,
            self.item.fulfillment_instructions, 'Available from', 'Available until', 'Benefit expires',
            '3 remaining', '260 Points', '500 Points', 'You need 240 more points', 'Limited time']:
            self.assertContains(response, value)
        self.assertContains(response, f'href="{self.item.information_url}" target="_blank" rel="noopener noreferrer"')

    def test_available_reward_links_to_confirmation_without_spending(self):
        response = self.client.get(self.detail_url(self.affordable))
        self.assertContains(response, reverse('customer_reward_confirm', args=[self.affordable.pk]))
        self.assertNotContains(self.client.get(self.detail_url()), 'class="reward-confirm-link"')

    def test_html_escaped_and_unsafe_stored_urls_omitted(self):
        Reward.objects.filter(pk=self.item.pk).update(title='<script>bad()</script>', full_description='<img src=x onerror=bad()>', information_url='javascript:bad()', image_url='https://user:password@example.com/image')
        for url in (self.url, self.detail_url()):
            response = self.client.get(url)
            for value in ['<script>bad()</script>', '<img src=x onerror=bad()>', 'javascript:bad()', 'user:password@']:
                self.assertNotContains(response, value)

    def test_no_private_values_notes_or_audit_in_queries_or_context(self):
        voucher = make_reward(is_active=True)
        code = make_code(voucher)
        redemption = make_redemption(self.other, voucher)
        event = RedemptionEvent.objects.create(redemption=redemption, event_type='processing', internal_note='STAFF-AUDIT-SECRET')
        RewardRequest.objects.create(user=self.user, title='Private request', description='Request text', internal_notes='STAFF-REQUEST-SECRET')
        for url in (self.url, self.detail_url(voucher)):
            with CaptureQueriesContext(connection) as queries:
                response = self.client.get(url)
            for secret in [code.payload_fingerprint, bytes(code.encrypted_payload).decode(), code.encryption_key_id, event.internal_note, 'STAFF-REQUEST-SECRET']:
                self.assertNotContains(response, secret)
            sql = ' '.join(query['sql'].lower() for query in queries)
            for name in ['encrypted_payload', 'payload_fingerprint', 'encryption_key_id', 'internal_note', 'snapshot_']:
                self.assertNotIn(name, sql)
            rows = response.context['rewards'] if url == self.url else [response.context['reward']]
            self.assertTrue(all(type(row) is dict for row in rows))
            self.assertTrue(all('private_codes' not in row and 'customer_usage' not in row for row in rows))

    def test_reads_preserve_points_inventory_and_redemptions(self):
        before = CustomerPoints.objects.filter(user=self.user).values().get()
        request = RequestFactory().get(self.url)
        request.user = self.user
        with CaptureQueriesContext(connection) as queries:
            for _ in range(2):
                customer_rewards(request)
                customer_reward_detail(request, self.item.pk)
        self.assertTrue(all(q['sql'].lstrip().upper().startswith('SELECT') for q in queries))
        self.assertEqual(CustomerPoints.objects.filter(user=self.user).values().get(), before)
        self.item.refresh_from_db()
        self.assertEqual(self.item.stock_remaining, 3)
        self.assertFalse(Redemption.objects.exists())

    def test_next_navigation_refreshes_balance_and_stock(self):
        self.assertFalse(self.cards()[self.item.pk]['available'])
        CustomerPoints.objects.filter(user=self.user).update(total_points=500)
        self.assertTrue(self.cards()[self.item.pk]['available'])
        Reward.objects.filter(pk=self.item.pk).update(stock_remaining=0)
        self.assertFalse(self.cards()[self.item.pk]['available'])

    def test_customer_only_access_to_both_views(self):
        for view, args in [(customer_rewards, ()), (customer_reward_detail, (self.item.pk,))]:
            request = RequestFactory().get(self.url)
            request.user = AnonymousUser()
            self.assertEqual(view(request, *args).status_code, 302)
            for role, active, staff in [('customer', False, False), ('user', True, False), ('user', True, True)]:
                request.user = get_user_model()(pk=self.user.pk, role=role, is_active=active, is_staff=staff)
                with self.assertRaises(PermissionDenied):
                    view(request, *args)
                navigation = RequestFactory().get(self.url, HTTP_X_CUSTOMER_NAVIGATION='1')
                navigation.user = request.user
                self.assertEqual(view(navigation, *args).status_code, 403)

    def test_no_post_redemption_action(self):
        before = CustomerPoints.objects.filter(user=self.user).values().get()
        for url in (self.url, self.detail_url()):
            self.assertEqual(self.client.post(url, {'redeem': 1, 'points': 0}).status_code, 405)
        self.assertEqual(CustomerPoints.objects.filter(user=self.user).values().get(), before)
        self.assertFalse(Redemption.objects.exists())

    def test_accessible_category_and_availability_controls(self):
        response = self.client.get(self.url)
        for slug, _ in REWARD_CATEGORIES:
            self.assertContains(response, f'data-category="{slug}" aria-pressed=')
        self.assertContains(response, 'aria-controls="rewards-grid"', count=10)
        self.assertContains(response, 'All rewards')
        self.assertContains(response, 'Available for me')

    def test_persistent_identity_active_navigation_and_cache_headers(self):
        identities = []
        for url, page in [(self.url, 'rewards'), (self.detail_url(), 'reward_detail')]:
            response = self.client.get(url, HTTP_X_CUSTOMER_NAVIGATION='1')
            for marker in [f'data-page="{page}"', 'id="customer-page-main"', 'data-customer-page-style', 'class="customer-music-audio"']:
                self.assertContains(response, marker)
            self.assertContains(response, f'<a href="{self.url}" aria-current="page">Rewards</a>', html=True)
            self.assertIn('no-store', response['Cache-Control'])
            self.assertIn('Cookie', response['Vary'])
            identities.append(response.wsgi_request.customer_shell_session)
        self.assertEqual(identities[0], identities[1])
