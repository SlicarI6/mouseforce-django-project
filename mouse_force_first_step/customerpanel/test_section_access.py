from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Event
from unittest.mock import patch
from uuid import uuid4

from django.contrib import admin
from django.contrib.auth import get_user_model
from django.core import signing
from django.core.exceptions import PermissionDenied
from django.db import IntegrityError, close_old_connections, connection, connections, transaction
from django.test import Client, RequestFactory, TestCase, TransactionTestCase, skipUnlessDBFeature
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from .models import CustomerPoints, CustomerSectionUnlock, Reward, Redemption
from .points import claim_daily_points, claim_streak_bonus
from .section_access import (CONFIRMATION_SECONDS, SECTIONS, TOKEN_SALT, UnlockError,
    create_unlock_confirmation, get_section_state, unlock_section)
from .section_test_support import seed_paid_access
from .test_reward_models import make_reward
from .reward_confirmation import create_reward_confirmation, ConfirmationError
from .reward_redemption import redeem_reward

SESSION = 'isolated-unlock-session'


class UnlockFixtures:
    def setUp(self):
        super().setUp()
        self.user = get_user_model().objects.create_user(username='unlock-customer', role='customer')
        self.other = get_user_model().objects.create_user(username='other-unlock-customer', role='customer')
        self.points = CustomerPoints.objects.create(user=self.user, total_points=50, streak_days=7,
            last_daily_claim_date=timezone.now().date(), day_7_bonus_awarded=False)

    def quote(self, section='rewards', user=None, session=SESSION):
        return create_unlock_confirmation(user or self.user, section, session_key=session)

    def unlock(self, quote, user=None, session=SESSION):
        return unlock_section(user or self.user, quote['section'], quote['token'], session_key=session)

    def balance(self):
        return CustomerPoints.objects.get(user=self.user).total_points


class SectionUnlockServiceTests(UnlockFixtures, TestCase):
    def test_quote_is_read_only_and_token_has_only_public_confirmation(self):
        before = CustomerPoints.objects.values().get(user=self.user)
        quote = self.quote()
        self.assertEqual((quote['balance'], quote['cost'], quote['balance_after']), (50, 10, 40))
        self.assertEqual(CustomerPoints.objects.values().get(user=self.user), before)
        self.assertFalse(CustomerSectionUnlock.objects.exists())
        claims = signing.loads(quote['token'], salt=TOKEN_SALT)
        self.assertEqual(set(claims), {'v', 'u', 's', 'section', 'cost', 'balance', 'intent'})
        self.assertNotIn(SESSION, quote['token'])

    def test_unlock_once_changes_only_balance_and_records_receipt(self):
        before = CustomerPoints.objects.values().get(user=self.user)
        result = self.unlock(self.quote())
        after = CustomerPoints.objects.values().get(user=self.user)
        self.assertEqual(after, {**before, 'total_points': 40})
        record = CustomerSectionUnlock.objects.get(user=self.user, section='rewards')
        self.assertEqual((record.points_spent, record.balance_after, record.source), (10, 40, 'points'))
        self.assertEqual(result['points_spent'], 10)
        self.assertFalse(Redemption.objects.exists())

    def test_retries_and_new_quotes_do_not_charge_existing_access(self):
        quote = self.quote()
        first = self.unlock(quote)
        CustomerPoints.objects.filter(user=self.user).update(total_points=0)
        for retry in (quote, self.quote()):
            result = self.unlock(retry)
            self.assertTrue(result['already_unlocked'])
            self.assertEqual(result['unlock_id'], first['unlock_id'])
            self.assertEqual(result['points_spent'], 0)
        self.assertEqual(self.balance(), 0)
        self.assertEqual(CustomerSectionUnlock.objects.count(), 1)

    def test_each_section_costs_ten_and_how_points_work_is_not_purchasable(self):
        for section in SECTIONS:
            self.unlock(self.quote(section))
        self.assertEqual(self.balance(), 0)
        self.assertEqual(CustomerSectionUnlock.objects.count(), 5)
        from django.http import Http404
        with self.assertRaises(Http404):
            self.quote('how_points_work')

    def test_insufficient_balance_and_missing_points_do_not_create_access(self):
        CustomerPoints.objects.filter(user=self.user).update(total_points=9)
        quote = self.quote()
        self.assertIsNone(quote['balance_after'])
        self.assertFalse(quote['can_unlock'])
        with self.assertRaisesRegex(UnlockError, 'insufficient_points'):
            self.unlock(quote)
        quote = self.quote(user=self.other)
        with self.assertRaisesRegex(UnlockError, 'insufficient_points'):
            self.unlock(quote, user=self.other)
        self.assertFalse(CustomerPoints.objects.filter(user=self.other).exists())
        self.assertFalse(CustomerSectionUnlock.objects.exists())

    def test_tampered_expired_wrong_customer_and_wrong_session_tokens(self):
        quote = self.quote()
        for candidate, user, session in [({**quote, 'token': quote['token'] + 'x'}, self.user, SESSION),
                (quote, self.other, SESSION), (quote, self.user, 'different-session'),
                ({**quote, 'section': 'news'}, self.user, SESSION)]:
            with self.subTest(user=user.pk, session=session, section=candidate['section']):
                with self.assertRaisesRegex(UnlockError, 'invalid_confirmation'):
                    self.unlock(candidate, user=user, session=session)
        with patch('django.core.signing.time.time', return_value=timezone.now().timestamp() - CONFIRMATION_SECONDS - 20):
            expired = self.quote()
        with self.assertRaisesRegex(UnlockError, 'expired_confirmation'):
            self.unlock(expired)
        self.assertEqual(self.balance(), 50)

    def test_inactive_non_customer_and_changed_balance_rejected(self):
        quote = self.quote()
        CustomerPoints.objects.filter(user=self.user).update(total_points=60)
        with self.assertRaisesRegex(UnlockError, 'balance_changed'):
            self.unlock(quote)
        for changes in ({'is_active': False}, {'is_active': True, 'role': 'user'}):
            get_user_model().objects.filter(pk=self.user.pk).update(**changes)
            with self.assertRaises(PermissionDenied):
                self.unlock(quote)
        self.assertFalse(CustomerSectionUnlock.objects.exists())

    def test_failure_after_deduction_rolls_everything_back(self):
        quote = self.quote()
        with patch.object(CustomerSectionUnlock.objects, 'create', side_effect=IntegrityError('test rollback')):
            with self.assertRaises(IntegrityError):
                self.unlock(quote)
        self.assertEqual(self.balance(), 50)
        self.assertFalse(CustomerSectionUnlock.objects.exists())

    def test_outer_transaction_rollback_undoes_unlock(self):
        with transaction.atomic():
            self.unlock(self.quote())
            transaction.set_rollback(True)
        self.assertEqual(self.balance(), 50)
        self.assertFalse(CustomerSectionUnlock.objects.exists())

    def test_constraints_and_permanent_record_protection(self):
        self.unlock(self.quote())
        attempts = [lambda: CustomerSectionUnlock.objects.create(user=self.user, section='rewards', balance_after=40),
            lambda: CustomerSectionUnlock.objects.create(user=self.user, section='news', balance_after=40, points_spent=0),
            lambda: CustomerSectionUnlock.objects.create(user=self.user, section='invalid', balance_after=40),
            lambda: CustomerSectionUnlock.objects.filter(user=self.user).update(section='news'),
            lambda: CustomerSectionUnlock.objects.filter(user=self.user).delete()]
        for attempt in attempts:
            with self.subTest(attempt=attempt), self.assertRaises(IntegrityError), transaction.atomic():
                attempt()
        self.assertEqual(CustomerSectionUnlock.objects.get().section, 'rewards')

    def test_customer_access_is_separate_between_accounts_and_sessions(self):
        self.unlock(self.quote())
        self.assertIn('rewards', get_section_state(self.user)['unlocked'])
        self.assertNotIn('rewards', get_section_state(self.other)['unlocked'])
        result = self.unlock(self.quote(session='new-device'), session='new-device')
        self.assertTrue(result['already_unlocked'])

    def test_admin_is_read_only(self):
        staff = get_user_model().objects.create_superuser(username='unlock-admin', email='admin@example.test', password='test-only')
        request = RequestFactory().get('/admin/customerpanel/customersectionunlock/')
        request.user = staff
        model_admin = admin.site._registry[CustomerSectionUnlock]
        self.assertTrue(model_admin.has_view_permission(request))
        self.assertFalse(model_admin.has_add_permission(request))
        self.assertFalse(model_admin.has_change_permission(request))
        self.assertFalse(model_admin.has_delete_permission(request))
        self.assertIsNone(model_admin.actions)
        self.assertEqual(set(model_admin.readonly_fields), {f.name for f in CustomerSectionUnlock._meta.fields})


class SectionAccessRequestTests(UnlockFixtures, TestCase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.user)

    def url(self, section='rewards'):
        return reverse('customer_section_unlock', args=[section])

    def buy(self, section='rewards', client=None):
        client = client or self.client
        quote = client.get(self.url(section)).json()
        return client.post(self.url(section), {'confirmation_token': quote['token']}, HTTP_ACCEPT='application/json')

    def test_all_section_gets_blocked_without_provider_calls_or_points_changes(self):
        with patch('mouse_force_first_step.customerpanel.views.get_customer_news') as news, patch('mouse_force_first_step.customerpanel.views.get_customer_weather') as weather:
            for section in SECTIONS:
                for headers in ({}, {'HTTP_X_CUSTOMER_NAVIGATION': '1'}):
                    response = self.client.get(reverse('customer_' + section), {'city': 'London'}, **headers)
                    self.assertEqual(response.status_code, 403)
                    self.assertEqual(response['X-Customer-Section-Locked'], section)
                    self.assertIn('no-store', response['Cache-Control'])
                    if headers:
                        self.assertEqual(response.json()['error'], 'section_locked')
                    else:
                        self.assertTemplateUsed(response, 'customer_section_locked.html')
            news.assert_not_called(); weather.assert_not_called()
        self.assertEqual(self.balance(), 50)
        self.assertFalse(CustomerSectionUnlock.objects.exists())

    def test_all_rewards_subroutes_and_actions_require_the_same_unlock(self):
        record_id = uuid4()
        routes = [('customer_reward_detail', [record_id]), ('customer_reward_confirm', [record_id]),
            ('customer_reward_redeem', [record_id]), ('customer_redemption_history', []),
            ('customer_redemption_result', [record_id]), ('customer_redemption_reveal', [record_id]),
            ('customer_reward_requests', []), ('customer_reward_request_new', []),
            ('customer_reward_request_detail', [record_id]), ('customer_reward_request_confirm', [record_id])]
        for name, args in routes:
            with self.subTest(route=name):
                method = self.client.post if name in ('customer_reward_redeem', 'customer_redemption_reveal', 'customer_reward_request_new') else self.client.get
                response = method(reverse(name, args=args), HTTP_ACCEPT='application/json')
                self.assertEqual(response.status_code, 403)
                self.assertEqual(response.json()['error'], 'section_locked')
        self.assertEqual(self.balance(), 50)

    def test_free_pages_claims_and_session_do_not_require_unlock(self):
        for name in ('customer_dashboard', 'customer_how_points_work', 'customer_session'):
            self.assertEqual(self.client.get(reverse(name)).status_code, 200)
        self.assertEqual(self.client.post(reverse('claim_daily_points')).status_code, 200)
        self.assertEqual(self.client.post(reverse('claim_streak_bonus')).status_code, 200)
        with patch('mouse_force_first_step.customerpanel.views.get_random_music_track', return_value={'id': 'test'}):
            self.assertEqual(self.client.get(reverse('customer_music_track')).status_code, 200)
        self.assertFalse(CustomerSectionUnlock.objects.exists())

    def test_success_updates_balance_unlocks_all_reward_pages_and_retries_free(self):
        reward = make_reward(is_active=True, fulfillment_type='manual', points_required=20)
        result = self.buy()
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()['state']['points']['total_points'], 40)
        for name, args in [('customer_rewards', []), ('customer_reward_detail', [reward.pk]),
            ('customer_reward_confirm', [reward.pk]), ('customer_redemption_history', []),
            ('customer_reward_requests', []), ('customer_reward_request_new', [])]:
            self.assertEqual(self.client.get(reverse(name, args=args)).status_code, 200)
        self.assertEqual(self.buy().json()['points_spent'], 0)
        self.assertEqual(self.balance(), 40)

    def test_rewards_unlock_does_not_replace_reward_cost_or_reveal_ownership(self):
        reward = make_reward(is_active=True, fulfillment_type='manual', points_required=20)
        self.buy()
        page = self.client.get(reverse('customer_reward_confirm', args=[reward.pk]))
        quote = page.context['confirmation']
        result = self.client.post(reverse('customer_reward_redeem', args=[reward.pk]),
            {'confirmation_token': quote.token, 'request_details': ''}, HTTP_ACCEPT='application/json')
        self.assertEqual(result.status_code, 200)
        self.assertEqual(self.balance(), 20)
        record = Redemption.objects.get(user=self.user)
        self.assertEqual(self.client.get(reverse('customer_redemption_result', args=[record.pk])).status_code, 200)
        self.client.force_login(self.other)
        seed_paid_access(self.other)
        self.assertEqual(self.client.get(reverse('customer_redemption_result', args=[record.pk])).status_code, 404)

    def test_csrf_and_payload_tampering(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.user)
        client.get(reverse('customer_rewards'))
        quote = client.get(self.url()).json()
        body = {'confirmation_token': quote['token']}
        self.assertEqual(client.post(self.url(), body).status_code, 403)
        csrf = client.cookies['csrftoken'].value
        for name in ('user_id', 'cost', 'balance', 'total_points', 'section'):
            response = client.post(self.url(), {**body, name: '1'}, HTTP_X_CSRFTOKEN=csrf, HTTP_ACCEPT='application/json')
            self.assertEqual(response.status_code, 400)
        self.assertEqual(client.post(self.url(), body, HTTP_X_CSRFTOKEN=csrf, HTTP_ACCEPT='application/json').status_code, 200)
        self.assertEqual(self.balance(), 40)

    def test_anonymous_non_customer_inactive_and_unknown_section(self):
        self.client.logout()
        self.assertEqual(self.client.get(self.url()).status_code, 401)
        self.client.force_login(self.other)
        get_user_model().objects.filter(pk=self.other.pk).update(role='user')
        self.assertEqual(self.client.post(self.url()).status_code, 403)
        self.client.force_login(self.user)
        self.assertEqual(self.client.get(self.url('how_points_work')).status_code, 404)
        get_user_model().objects.filter(pk=self.user.pk).update(is_active=False)
        self.assertIn(self.client.post(self.url()).status_code, (401, 403))
        self.assertFalse(CustomerSectionUnlock.objects.exists())

    def test_normal_form_post_redirect_get_and_safe_destination(self):
        quote = self.client.get(self.url('weather')).json()
        result = self.client.post(self.url('weather'), {'confirmation_token': quote['token'], 'next': '/customer/weather/?city=London'})
        self.assertEqual(result.status_code, 303)
        self.assertEqual(result.url, '/customer/weather/?city=London')
        quote = self.client.get(self.url('news')).json()
        result = self.client.post(self.url('news'), {'confirmation_token': quote['token'], 'next': 'https://attacker.example/'})
        self.assertEqual(result.url, '/customer/news/')

    def test_state_and_quotes_are_not_cacheable_and_get_never_unlocks(self):
        for name in (reverse('customer_section_access'), self.url()):
            for _ in range(2):
                response = self.client.get(name)
                self.assertEqual(response.status_code, 200)
                self.assertIn('no-store', response['Cache-Control'])
        self.assertFalse(CustomerSectionUnlock.objects.exists())
        self.assertEqual(self.balance(), 50)


@skipUnlessDBFeature('has_select_for_update')
class SectionUnlockConcurrencyTests(UnlockFixtures, TransactionTestCase):
    available_apps = ['django.contrib.auth', 'django.contrib.contenttypes',
                      'mouse_force_first_step.accounts', 'mouse_force_first_step.customerpanel']

    def worker(self, operation, attempted):
        close_old_connections()
        def observe(execute, sql, params, many, context):
            if 'FOR UPDATE' in sql and get_user_model()._meta.db_table in sql:
                attempted.set()
            return execute(sql, params, many, context)
        try:
            with connection.cursor() as cursor:
                cursor.execute("SET lock_timeout = '10s'")
            with connection.execute_wrapper(observe):
                try:
                    return operation()
                except (UnlockError, ConfirmationError) as error:
                    return error.code
        finally:
            connections.close_all()

    def race(self, operations):
        events = [Event() for _ in operations]
        with ThreadPoolExecutor(max_workers=len(operations)) as pool:
            with transaction.atomic():
                get_user_model().objects.select_for_update().get(pk=self.user.pk)
                futures = [pool.submit(self.worker, fn, event) for fn, event in zip(operations, events)]
                for event in events:
                    self.assertTrue(event.wait(10))
                self.assertTrue(all(not future.done() for future in futures))
            return [future.result(timeout=20) for future in futures]

    def test_same_section_concurrent_double_click_charges_once(self):
        quote = self.quote()
        results = self.race([lambda: self.unlock(quote), lambda: self.unlock(quote)])
        self.assertCountEqual([r['points_spent'] for r in results], [10, 0])
        self.assertEqual(results[0]['unlock_id'], results[1]['unlock_id'])
        self.assertEqual(self.balance(), 40)
        self.assertEqual(CustomerSectionUnlock.objects.count(), 1)

    def test_different_confirmations_for_same_section_charge_once(self):
        first, second = self.quote(), self.quote()
        results = self.race([lambda: self.unlock(first), lambda: self.unlock(second)])
        self.assertCountEqual([r['already_unlocked'] for r in results], [False, True])
        self.assertEqual(self.balance(), 40)

    def test_different_sections_cannot_spend_same_ten_points(self):
        CustomerPoints.objects.filter(user=self.user).update(total_points=10)
        first, second = self.quote('news'), self.quote('weather')
        results = self.race([lambda: self.unlock(first), lambda: self.unlock(second)])
        self.assertEqual(results.count('insufficient_points'), 1)
        self.assertEqual(self.balance(), 0)
        self.assertEqual(CustomerSectionUnlock.objects.count(), 1)

    def test_lock_order_is_user_then_points_then_unlock(self):
        quote = self.quote()
        with CaptureQueriesContext(connection) as queries:
            self.unlock(quote)
        locks = [q['sql'] for q in queries if 'FOR UPDATE' in q['sql']]
        self.assertEqual(len(locks), 3)
        for query, model in zip(locks, (get_user_model(), CustomerPoints, CustomerSectionUnlock)):
            self.assertIn(model._meta.db_table, query)

    def test_waiting_unlock_reviews_after_daily_or_bonus_changes(self):
        for operation in (claim_daily_points, claim_streak_bonus):
            CustomerPoints.objects.filter(user=self.user).update(total_points=50, streak_days=7,
                last_daily_claim_date=timezone.now().date() - timedelta(days=1), day_7_bonus_awarded=False)
            quote = self.quote()
            event = Event()
            with ThreadPoolExecutor(max_workers=1) as pool:
                with transaction.atomic():
                    get_user_model().objects.select_for_update().get(pk=self.user.pk)
                    future = pool.submit(self.worker, lambda: self.unlock(quote), event)
                    self.assertTrue(event.wait(10))
                    operation(self.user)
                self.assertEqual(future.result(timeout=20), 'balance_changed')
            self.assertEqual(self.balance(), 95 if operation == claim_daily_points else 85)
            self.assertFalse(CustomerSectionUnlock.objects.exists())

    def test_bonus_waiting_for_unlock_preserves_both_changes(self):
        event = Event()
        with ThreadPoolExecutor(max_workers=1) as pool:
            with transaction.atomic():
                self.unlock(self.quote())
                future = pool.submit(self.worker, lambda: claim_streak_bonus(self.user), event)
                self.assertTrue(event.wait(10))
            self.assertEqual(future.result(timeout=20).awarded_amount, 35)
        self.assertEqual(self.balance(), 75)

    def test_waiting_redemption_reviews_after_another_section_unlock(self):
        reward = make_reward(is_active=True, fulfillment_type='manual', points_required=20)
        confirmation = create_reward_confirmation(self.user, reward.pk, session_key=SESSION)
        event = Event()
        with ThreadPoolExecutor(max_workers=1) as pool:
            with transaction.atomic():
                self.unlock(self.quote('news'))
                future = pool.submit(self.worker, lambda: redeem_reward(self.user, reward.pk, confirmation.token, session_key=SESSION), event)
                self.assertTrue(event.wait(10))
            self.assertEqual(future.result(timeout=20), 'balance_changed')
        self.assertEqual(self.balance(), 40)
        self.assertFalse(Redemption.objects.exists())
