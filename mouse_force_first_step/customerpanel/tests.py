from concurrent.futures import ThreadPoolExecutor, TimeoutError
from datetime import datetime, timedelta, timezone as datetime_timezone
from threading import Event
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.core.exceptions import PermissionDenied
from django.db import close_old_connections, connection, connections, transaction
from django.middleware.csrf import get_token
from django.test import Client, RequestFactory, TestCase, TransactionTestCase, skipUnlessDBFeature
from django.urls import reverse
from django.utils import timezone

from .models import CustomerPoints
from .points import claim_daily_points, claim_streak_bonus, get_points_state


NOW = datetime(2026, 9, 8, 12, tzinfo=datetime_timezone.utc)


class DailyPointsTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username='points-customer', role='customer',
        )
        clock = patch('django.utils.timezone.now', return_value=NOW)
        clock.start()
        self.addCleanup(clock.stop)

    def state(self, streak, gap=1, total=100, day7=False, day14=False):
        return CustomerPoints.objects.create(
            user=self.user, total_points=total,
            last_daily_claim_date=NOW.date() - timedelta(days=gap),
            streak_days=streak, day_7_bonus_awarded=day7,
            day_14_bonus_awarded=day14,
        )

    def assert_claim(self, total, streak, bonus=0):
        result = claim_daily_points(self.user)
        self.assertEqual(result.status, 'claimed')
        self.assertEqual(result.daily_points_awarded, 10)
        self.assertEqual(result.bonus_points_awarded, bonus)
        self.assertEqual(result.total_points, total)
        self.assertEqual(result.streak_days, streak)
        points = CustomerPoints.objects.get(user=self.user)
        self.assertEqual(points.total_points, total)
        self.assertEqual(points.streak_days, streak)
        self.assertEqual(points.last_daily_claim_date, NOW.date())
        return points

    def test_first_claim(self):
        points = self.assert_claim(10, 1)
        self.assertFalse(points.day_7_bonus_awarded)
        self.assertFalse(points.day_14_bonus_awarded)

    def test_existing_empty_state(self):
        CustomerPoints.objects.create(user=self.user)
        self.assert_claim(10, 1)

    def test_same_day_duplicate_changes_nothing(self):
        claim_daily_points(self.user)
        before = CustomerPoints.objects.values().get(user=self.user)
        result = claim_daily_points(self.user)
        self.assertEqual(result.status, 'already_claimed')
        self.assertEqual(result.daily_points_awarded, 0)
        self.assertEqual(result.bonus_points_awarded, 0)
        self.assertEqual(CustomerPoints.objects.values().get(user=self.user), before)

    def test_consecutive_day(self):
        self.state(3)
        self.assert_claim(110, 4)

    def test_missed_day_resets_streak_but_preserves_points(self):
        self.state(10, gap=2, total=235, day7=True)
        points = self.assert_claim(245, 1)
        self.assertFalse(points.day_7_bonus_awarded)
        self.assertFalse(points.day_14_bonus_awarded)

    def test_reaching_day_7_does_not_pay_bonus(self):
        self.state(6, total=60)
        points = self.assert_claim(70, 7)
        self.assertFalse(points.day_7_bonus_awarded)

    def test_consecutive_day_8_auto_awards_day_7_bonus_once(self):
        self.state(7, total=70)
        points = self.assert_claim(115, 8, bonus=35)
        self.assertTrue(points.day_7_bonus_awarded)
        duplicate = claim_daily_points(self.user)
        self.assertEqual(duplicate.daily_points_awarded, 0)
        self.assertEqual(duplicate.bonus_points_awarded, 0)
        points.refresh_from_db()
        self.assertEqual(points.total_points, 115)

    def test_missed_day_8_loses_unclaimed_bonus(self):
        self.state(7, gap=2, total=70)
        points = self.assert_claim(80, 1)
        self.assertFalse(points.day_7_bonus_awarded)

    def test_previously_awarded_day_7_bonus_is_not_paid_again(self):
        self.state(7, total=105, day7=True)
        points = self.assert_claim(115, 8)
        self.assertTrue(points.day_7_bonus_awarded)

    def test_reaching_day_14_does_not_pay_bonus(self):
        self.state(13, total=165, day7=True)
        points = self.assert_claim(175, 14)
        self.assertFalse(points.day_14_bonus_awarded)

    def test_consecutive_after_day_14_pays_bonus_and_starts_new_cycle(self):
        self.state(14, total=175, day7=True)
        points = self.assert_claim(235, 1, bonus=50)
        self.assertFalse(points.day_7_bonus_awarded)
        self.assertFalse(points.day_14_bonus_awarded)
        duplicate = claim_daily_points(self.user)
        self.assertEqual(duplicate.daily_points_awarded, 0)
        self.assertEqual(duplicate.bonus_points_awarded, 0)
        points.refresh_from_db()
        self.assertEqual(points.total_points, 235)

    def test_missed_day_after_14_loses_unclaimed_bonus(self):
        self.state(14, gap=2, total=175, day7=True)
        points = self.assert_claim(185, 1)
        self.assertFalse(points.day_7_bonus_awarded)
        self.assertFalse(points.day_14_bonus_awarded)

    def test_previously_awarded_day_14_bonus_is_not_paid_again(self):
        self.state(14, total=225, day7=True, day14=True)
        points = self.assert_claim(235, 1)
        self.assertFalse(points.day_7_bonus_awarded)
        self.assertFalse(points.day_14_bonus_awarded)

    def test_two_full_cycles_can_earn_bonuses_again(self):
        for offset in range(29):
            with patch('django.utils.timezone.now', return_value=NOW + timedelta(days=offset)):
                result = claim_daily_points(self.user)
        self.assertEqual(result.total_points, 460)
        self.assertEqual(result.streak_days, 1)

    def test_utc_midnight_allows_next_calendar_day_without_24_hour_wait(self):
        midnight = NOW.replace(hour=0, minute=0, second=0)
        with timezone.override('Pacific/Honolulu'):
            with patch('django.utils.timezone.now', return_value=midnight - timedelta(seconds=1)):
                first = claim_daily_points(self.user)
            with patch('django.utils.timezone.now', return_value=midnight):
                second = claim_daily_points(self.user)
                duplicate = claim_daily_points(self.user)
        self.assertEqual(first.last_daily_claim_date, midnight.date() - timedelta(days=1))
        self.assertEqual(second.last_daily_claim_date, midnight.date())
        self.assertEqual(second.total_points, 20)
        self.assertEqual(second.streak_days, 2)
        self.assertEqual(duplicate.status, 'already_claimed')

    def test_future_claim_date_awards_nothing(self):
        self.state(3, gap=-1)
        before = CustomerPoints.objects.values().get(user=self.user)
        result = claim_daily_points(self.user)
        self.assertEqual(result.status, 'future_claim_date')
        self.assertEqual(result.daily_points_awarded, 0)
        self.assertEqual(result.bonus_points_awarded, 0)
        self.assertEqual(CustomerPoints.objects.values().get(user=self.user), before)

    def test_anonymous_user_is_rejected(self):
        with self.assertRaises(PermissionDenied):
            claim_daily_points(AnonymousUser())
        self.assertFalse(CustomerPoints.objects.exists())

    def test_non_customer_is_rejected_using_current_database_role(self):
        get_user_model().objects.filter(pk=self.user.pk).update(role='user')
        with self.assertRaises(PermissionDenied):
            claim_daily_points(self.user)
        self.assertFalse(CustomerPoints.objects.exists())

    def test_inactive_customer_is_rejected(self):
        get_user_model().objects.filter(pk=self.user.pk).update(is_active=False)
        with self.assertRaises(PermissionDenied):
            claim_daily_points(self.user)
        self.assertFalse(CustomerPoints.objects.exists())

    def test_failed_save_rolls_back_daily_and_bonus(self):
        self.state(7, total=70)
        before = CustomerPoints.objects.values().get(user=self.user)
        with patch.object(CustomerPoints, 'save', side_effect=RuntimeError('save failed')):
            with self.assertRaises(RuntimeError):
                claim_daily_points(self.user)
        self.assertEqual(CustomerPoints.objects.values().get(user=self.user), before)
        self.assert_claim(115, 8, bonus=35)


@skipUnlessDBFeature('has_select_for_update')
class ConcurrentDailyPointsTests(TransactionTestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username='concurrent-customer', role='customer',
        )

    def run_concurrent_claims(self, actions):
        """Hold the parent lock until both independent connections try to claim."""
        attempted = [Event(), Event()]
        user = self.user
        user_table = get_user_model()._meta.db_table

        def worker(index):
            close_old_connections()

            def track_lock(execute, sql, params, many, context):
                if 'FOR UPDATE' in sql and user_table in sql:
                    attempted[index].set()
                return execute(sql, params, many, context)

            try:
                with connection.cursor() as cursor:
                    cursor.execute("SET lock_timeout = '5s'")
                with connection.execute_wrapper(track_lock):
                    return actions[index](user)
            finally:
                connections.close_all()

        with patch('django.utils.timezone.now', return_value=NOW):
            with ThreadPoolExecutor(max_workers=2) as executor:
                with transaction.atomic():
                    get_user_model().objects.select_for_update().get(pk=user.pk)
                    futures = [executor.submit(worker, index) for index in range(2)]
                    for event in attempted:
                        self.assertTrue(event.wait(timeout=5), 'Claim did not attempt the user lock')
                    for future in futures:
                        with self.assertRaises(TimeoutError):
                            future.result(timeout=0.1)
                results = [future.result(timeout=10) for future in futures]
        return results

    def assert_concurrent_claims(self, expected_total, expected_streak, expected_bonus):
        results = self.run_concurrent_claims([claim_daily_points, claim_daily_points])
        self.assertCountEqual([result.status for result in results], ['claimed', 'already_claimed'])
        self.assertEqual(sum(result.daily_points_awarded for result in results), 10)
        self.assertEqual(sum(result.bonus_points_awarded for result in results), expected_bonus)
        self.assertEqual(CustomerPoints.objects.filter(user=self.user).count(), 1)
        points = CustomerPoints.objects.get(user=self.user)
        self.assertEqual(points.total_points, expected_total)
        self.assertEqual(points.streak_days, expected_streak)

    def test_simultaneous_first_claims_create_one_state_and_award_once(self):
        self.assert_concurrent_claims(10, 1, 0)

    def test_simultaneous_day_8_claims_award_bonus_once(self):
        CustomerPoints.objects.create(
            user=self.user, total_points=70, streak_days=7,
            last_daily_claim_date=NOW.date() - timedelta(days=1),
        )
        self.assert_concurrent_claims(115, 8, 35)

    def test_simultaneous_cycle_rollover_awards_bonus_once(self):
        CustomerPoints.objects.create(
            user=self.user, total_points=175, streak_days=14,
            last_daily_claim_date=NOW.date() - timedelta(days=1),
            day_7_bonus_awarded=True,
        )
        self.assert_concurrent_claims(235, 1, 50)

    def assert_manual_duplicates(self, streak, initial_total, amount):
        points = CustomerPoints.objects.create(
            user=self.user, total_points=initial_total, streak_days=streak,
            last_daily_claim_date=NOW.date(), day_7_bonus_awarded=streak == 14,
        )
        results = self.run_concurrent_claims([claim_streak_bonus, claim_streak_bonus])
        self.assertCountEqual([result.status for result in results], ['claimed', 'already_awarded'])
        self.assertEqual(sum(result.awarded_amount for result in results), amount)
        points.refresh_from_db()
        self.assertEqual(points.total_points, initial_total + amount)
        self.assertEqual(points.streak_days, streak)
        self.assertEqual(points.last_daily_claim_date, NOW.date())
        self.assertTrue(getattr(points, f'day_{streak}_bonus_awarded'))

    def test_simultaneous_day_7_manual_claims_award_once(self):
        self.assert_manual_duplicates(7, 70, 35)

    def test_simultaneous_day_14_manual_claims_award_once(self):
        self.assert_manual_duplicates(14, 175, 50)

    def assert_manual_daily_race(self, streak, initial_total, amount, next_streak):
        points = CustomerPoints.objects.create(
            user=self.user, total_points=initial_total, streak_days=streak,
            last_daily_claim_date=NOW.date() - timedelta(days=1),
            day_7_bonus_awarded=streak == 14,
        )
        manual, daily = self.run_concurrent_claims([claim_streak_bonus, claim_daily_points])
        self.assertEqual(manual.awarded_amount + daily.bonus_points_awarded, amount)
        self.assertEqual(daily.daily_points_awarded, 10)
        points.refresh_from_db()
        self.assertEqual(points.total_points, initial_total + amount + 10)
        self.assertEqual(points.streak_days, next_streak)
        self.assertEqual(points.last_daily_claim_date, NOW.date())

    def test_manual_day_7_racing_daily_day_8_awards_bonus_once(self):
        self.assert_manual_daily_race(7, 70, 35, 8)

    def test_manual_day_14_racing_daily_rollover_awards_bonus_once(self):
        self.assert_manual_daily_race(14, 175, 50, 1)


class BonusFixtures:
    def setUp(self):
        super().setUp()
        self.user = get_user_model().objects.create_user(username='bonus-customer', role='customer')
        clock = patch('django.utils.timezone.now', return_value=NOW)
        clock.start()
        self.addCleanup(clock.stop)

    def state(self, streak=7, gap=0):
        return CustomerPoints.objects.create(
            user=self.user, total_points=70 if streak == 7 else 175,
            streak_days=streak, last_daily_claim_date=NOW.date() - timedelta(days=gap),
            day_7_bonus_awarded=streak == 14,
        )


class ManualBonusTests(BonusFixtures, TestCase):
    def assert_manual_claim(self, streak, amount):
        points = self.state(streak)
        original_total = points.total_points
        result = claim_streak_bonus(self.user)
        self.assertTrue(result.bonus_awarded)
        self.assertEqual(result.awarded_amount, amount)
        self.assertEqual(result.status, 'claimed')
        points.refresh_from_db()
        self.assertEqual(points.total_points, original_total + amount)
        self.assertEqual(result.total_points, points.total_points)
        self.assertEqual(result.streak_days, streak)
        self.assertEqual(points.streak_days, streak)
        self.assertEqual(points.last_daily_claim_date, NOW.date())
        self.assertEqual(result.last_daily_claim_date, NOW.date())
        self.assertTrue(getattr(points, f'day_{streak}_bonus_awarded'))
        self.assertEqual(result.day_7_bonus_awarded, points.day_7_bonus_awarded)
        self.assertEqual(result.day_14_bonus_awarded, points.day_14_bonus_awarded)

    def test_valid_day_7_manual_claim(self):
        self.assert_manual_claim(7, 35)

    def test_valid_day_14_manual_claim(self):
        self.assert_manual_claim(14, 50)

    def assert_duplicate(self, streak):
        self.state(streak)
        claim_streak_bonus(self.user)
        before = CustomerPoints.objects.values().get(user=self.user)
        result = claim_streak_bonus(self.user)
        self.assertEqual(result.status, 'already_awarded')
        self.assertFalse(result.bonus_awarded)
        self.assertEqual(result.awarded_amount, 0)
        self.assertEqual(CustomerPoints.objects.values().get(user=self.user), before)

    def test_duplicate_day_7_claim(self):
        self.assert_duplicate(7)

    def test_duplicate_day_14_claim(self):
        self.assert_duplicate(14)

    def assert_ineligible(self, streak):
        self.state(streak)
        before = CustomerPoints.objects.values().get(user=self.user)
        result = claim_streak_bonus(self.user)
        self.assertEqual(result.status, 'not_eligible')
        self.assertFalse(result.bonus_awarded)
        self.assertEqual(result.awarded_amount, 0)
        self.assertEqual(CustomerPoints.objects.values().get(user=self.user), before)

    def test_day_7_before_eligibility(self):
        self.assert_ineligible(6)

    def test_day_14_before_eligibility(self):
        self.assert_ineligible(13)

    def test_no_state_does_not_create_state_or_award_points(self):
        result = claim_streak_bonus(self.user)
        self.assertEqual(result.status, 'not_eligible')
        self.assertEqual(result.total_points, 0)
        self.assertFalse(result.bonus_awarded)
        self.assertFalse(CustomerPoints.objects.exists())

    def test_empty_state_is_ineligible(self):
        CustomerPoints.objects.create(user=self.user)
        result = claim_streak_bonus(self.user)
        self.assertEqual(result.status, 'not_eligible')
        self.assertEqual(result.awarded_amount, 0)

    def test_unpaid_milestones_expire_after_missed_day(self):
        for streak in (7, 14):
            with self.subTest(streak=streak):
                points = self.state(streak, gap=2)
                before = CustomerPoints.objects.values().get(user=self.user)
                result = claim_streak_bonus(self.user)
                self.assertEqual(result.status, 'expired')
                self.assertFalse(result.bonus_awarded)
                self.assertEqual(result.awarded_amount, 0)
                self.assertEqual(CustomerPoints.objects.values().get(user=self.user), before)
                points.delete()

    def test_next_consecutive_day_still_allows_manual_claim_before_daily(self):
        for streak, amount in ((7, 35), (14, 50)):
            with self.subTest(streak=streak):
                points = self.state(streak, gap=1)
                result = claim_streak_bonus(self.user)
                self.assertEqual(result.awarded_amount, amount)
                self.assertEqual(result.last_daily_claim_date, NOW.date() - timedelta(days=1))
                points.delete()

    def test_future_claim_date_cannot_unlock_bonus(self):
        self.state(7, gap=-1)
        result = claim_streak_bonus(self.user)
        self.assertEqual(result.status, 'future_claim_date')
        self.assertEqual(result.awarded_amount, 0)

    def test_active_request_timezone_does_not_extend_expired_utc_bonus(self):
        self.state(7, gap=2)
        with timezone.override('Pacific/Honolulu'):
            with patch('django.utils.timezone.now', return_value=NOW.replace(hour=0)):
                result = claim_streak_bonus(self.user)
        self.assertEqual(result.status, 'expired')
        self.assertEqual(result.awarded_amount, 0)

    def test_non_customer_and_inactive_customer_rejected_from_database_state(self):
        points = self.state()
        for changes in ({'role': 'user'}, {'role': 'customer', 'is_active': False}):
            with self.subTest(changes=changes):
                get_user_model().objects.filter(pk=self.user.pk).update(**changes)
                with self.assertRaises(PermissionDenied):
                    claim_streak_bonus(self.user)
                points.refresh_from_db()
                self.assertEqual(points.total_points, 70)
                self.assertFalse(points.day_7_bonus_awarded)

    def test_anonymous_user_rejected(self):
        with self.assertRaises(PermissionDenied):
            claim_streak_bonus(AnonymousUser())

    def assert_manual_then_daily(self, streak, final_total, next_streak):
        self.state(streak)
        claim_streak_bonus(self.user)
        same_day = claim_daily_points(self.user)
        self.assertEqual(same_day.status, 'already_claimed')
        with patch('django.utils.timezone.now', return_value=NOW + timedelta(days=1)):
            daily = claim_daily_points(self.user)
        self.assertEqual(daily.bonus_points_awarded, 0)
        self.assertEqual(daily.daily_points_awarded, 10)
        self.assertEqual(daily.total_points, final_total)
        self.assertEqual(daily.streak_days, next_streak)

    def test_manual_day_7_then_day_8_does_not_pay_bonus_again(self):
        self.assert_manual_then_daily(7, 115, 8)

    def test_manual_day_14_then_next_daily_does_not_pay_bonus_again(self):
        self.assert_manual_then_daily(14, 235, 1)

    def test_failed_save_rolls_back_bonus_and_flag(self):
        self.state()
        before = CustomerPoints.objects.values().get(user=self.user)
        with patch.object(CustomerPoints, 'save', side_effect=RuntimeError('save failed')):
            with self.assertRaises(RuntimeError):
                claim_streak_bonus(self.user)
        self.assertEqual(CustomerPoints.objects.values().get(user=self.user), before)
        self.assertEqual(claim_streak_bonus(self.user).awarded_amount, 35)


class BonusEndpointTests(BonusFixtures, TestCase):
    def setUp(self):
        super().setUp()
        self.state()
        self.url = reverse('claim_streak_bonus')
        self.client = Client(enforce_csrf_checks=True)
        self.client.force_login(self.user)
        csrf_request = RequestFactory().get('/')
        self.csrf_token = get_token(csrf_request)
        self.client.cookies[settings.CSRF_COOKIE_NAME] = csrf_request.META['CSRF_COOKIE']

    def post(self, data=None, **kwargs):
        return self.client.post(
            self.url, data={} if data is None else data,
            HTTP_X_CSRFTOKEN=self.csrf_token, **kwargs,
        )

    def test_post_returns_authoritative_state(self):
        self.assertEqual(self.url, '/customer/points/claim-bonus/')
        response = self.post(content_type='application/json')
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        state = payload.pop('state')
        self.assertEqual(state, get_points_state(self.user))
        self.assertFalse(state['day_7_bonus_claimable'])
        self.assertEqual(payload, {
            'status': 'claimed', 'bonus_awarded': True, 'awarded_amount': 35,
            'total_points': 105, 'streak_days': 7,
            'day_7_bonus_awarded': True, 'day_14_bonus_awarded': False,
            'last_daily_claim_date': NOW.date().isoformat(),
        })

    def test_duplicate_post_returns_zero_and_current_state(self):
        self.post()
        response = self.post()
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()['bonus_awarded'])
        self.assertEqual(response.json()['awarded_amount'], 0)
        self.assertEqual(response.json()['total_points'], 105)

    def test_day_14_amount_is_selected_by_server(self):
        CustomerPoints.objects.filter(user=self.user).update(
            streak_days=14, total_points=175, day_7_bonus_awarded=True,
        )
        response = self.post()
        self.assertEqual(response.json()['awarded_amount'], 50)
        self.assertEqual(response.json()['total_points'], 225)
        self.assertEqual(response.json()['streak_days'], 14)
        self.assertTrue(response.json()['day_14_bonus_awarded'])

    def test_get_is_rejected(self):
        self.assertEqual(self.client.get(self.url).status_code, 405)
        self.assertEqual(CustomerPoints.objects.get(user=self.user).total_points, 70)

    def test_missing_csrf_token_is_rejected(self):
        self.assertEqual(self.client.post(self.url, {}).status_code, 403)
        self.assertEqual(CustomerPoints.objects.get(user=self.user).total_points, 70)

    def test_anonymous_request_is_rejected(self):
        self.client.logout()
        csrf_request = RequestFactory().get('/')
        self.csrf_token = get_token(csrf_request)
        self.client.cookies[settings.CSRF_COOKIE_NAME] = csrf_request.META['CSRF_COOKIE']
        self.assertEqual(self.post().status_code, 401)
        self.assertEqual(CustomerPoints.objects.get(user=self.user).total_points, 70)

    def test_non_customer_request_is_rejected(self):
        get_user_model().objects.filter(pk=self.user.pk).update(role='user')
        self.assertEqual(self.post().status_code, 403)
        self.assertEqual(CustomerPoints.objects.get(user=self.user).total_points, 70)

    def test_inactive_customer_request_is_rejected(self):
        get_user_model().objects.filter(pk=self.user.pk).update(is_active=False)
        self.assertIn(self.post().status_code, (401, 403))
        self.assertEqual(CustomerPoints.objects.get(user=self.user).total_points, 70)

    def test_forged_form_json_and_query_values_award_nothing(self):
        fields = ('amount', 'bonus_amount', 'user_id', 'user', 'points', 'total_points',
                  'streak', 'streak_days', 'date', 'last_daily_claim_date',
                  'day_7_bonus_awarded', 'day_14_bonus_awarded')
        before = CustomerPoints.objects.values().get(user=self.user)
        for field in fields:
            for content_type in ('application/json', 'application/x-www-form-urlencoded'):
                with self.subTest(field=field, content_type=content_type):
                    payload = {field: 999} if content_type == 'application/json' else f'{field}=999'
                    self.assertEqual(self.post(payload, content_type=content_type).status_code, 400)
            with self.subTest(field=field, query=True):
                response = self.client.post(
                    self.url + f'?{field}=999', {}, HTTP_X_CSRFTOKEN=self.csrf_token,
                )
                self.assertEqual(response.status_code, 400)
        self.assertEqual(CustomerPoints.objects.values().get(user=self.user), before)

    def test_malformed_or_non_object_json_is_rejected(self):
        for payload in ('{', '[]', 'null', '35', '"claim"'):
            with self.subTest(payload=payload):
                self.assertEqual(self.post(payload, content_type='application/json').status_code, 400)
        self.assertEqual(CustomerPoints.objects.get(user=self.user).total_points, 70)

    def test_form_csrf_token_is_allowed(self):
        response = self.client.post(self.url, {'csrfmiddlewaretoken': self.csrf_token})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['awarded_amount'], 35)


class DashboardPointsTests(BonusFixtures, TestCase):
    def setUp(self):
        super().setUp()
        self.client = Client(enforce_csrf_checks=True)
        self.client.force_login(self.user)
        self.dashboard_url = reverse('customer_dashboard')
        self.daily_url = reverse('claim_daily_points')
        self.bonus_url = reverse('claim_streak_bonus')
        csrf_request = RequestFactory().get('/')
        self.csrf_token = get_token(csrf_request)
        self.client.cookies[settings.CSRF_COOKIE_NAME] = csrf_request.META['CSRF_COOKIE']

    def post_claim(self, url=None, **kwargs):
        return self.client.post(
            url or self.daily_url, HTTP_X_CSRFTOKEN=self.csrf_token, **kwargs,
        )

    def test_dashboard_defaults_do_not_create_points_row(self):
        response = self.client.get(self.dashboard_url)
        self.assertEqual(response.status_code, 200)
        state = response.context['points_state']
        self.assertEqual(state['total_points'], 0)
        self.assertEqual(state['displayed_streak'], 0)
        self.assertTrue(state['daily_claimable'])
        self.assertFalse(state['day_7_bonus_claimable'])
        self.assertFalse(state['day_14_bonus_claimable'])
        self.assertFalse(state['streak_broken'])
        self.assertContains(response, '0 Points')
        self.assertContains(response, '0 Days in a Row')
        self.assertContains(response, '+10 Daily')
        self.assertContains(response, 'Next: Day 7 (+35 Points)')
        self.assertContains(response, 'data-daily-url="/customer/points/claim-daily/"')
        self.assertContains(response, 'data-bonus-url="/customer/points/claim-bonus/"')
        self.assertContains(response, 'id="points-initial-state"')
        self.assertFalse(CustomerPoints.objects.filter(user=self.user).exists())

    def test_daily_endpoint_claims_once_and_returns_renderable_state(self):
        self.assertEqual(self.daily_url, '/customer/points/claim-daily/')
        response = self.post_claim()
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload['status'], 'claimed')
        self.assertEqual(payload['daily_points_awarded'], 10)
        self.assertEqual(payload['state']['total_points'], 10)
        self.assertEqual(payload['state']['displayed_streak'], 1)
        self.assertFalse(payload['state']['daily_claimable'])
        self.assertEqual(payload['state']['daily_label'], 'Claimed today')
        duplicate = self.post_claim().json()
        self.assertEqual(duplicate['status'], 'already_claimed')
        self.assertEqual(duplicate['daily_points_awarded'], 0)
        self.assertEqual(duplicate['state'], payload['state'])
        self.assertEqual(CustomerPoints.objects.get(user=self.user).total_points, 10)

    def test_dashboard_reflects_claim_and_refresh_does_not_award(self):
        self.post_claim()
        before = CustomerPoints.objects.values().get(user=self.user)
        for _ in range(2):
            response = self.client.get(self.dashboard_url)
            self.assertContains(response, '10 Points')
            self.assertContains(response, '1 Day in a Row')
            self.assertContains(response, 'Claimed today')
            self.assertContains(response, 'id="points-daily" disabled')
            self.assertEqual(response.context['points_state']['displayed_streak'], 1)
        self.assertEqual(CustomerPoints.objects.values().get(user=self.user), before)

    def test_dashboard_milestones_are_read_only_and_bonus_remains_connected(self):
        for streak, amount in ((7, 35), (14, 50)):
            with self.subTest(streak=streak):
                points = self.state(streak)
                before = CustomerPoints.objects.values().get(user=self.user)
                response = self.client.get(self.dashboard_url)
                self.assertContains(response, f'Claim +{amount} Points')
                self.assertTrue(response.context['points_state'][f'day_{streak}_bonus_claimable'])
                self.assertEqual(CustomerPoints.objects.values().get(user=self.user), before)
                claimed = self.post_claim(self.bonus_url).json()
                self.assertEqual(claimed['awarded_amount'], amount)
                self.assertFalse(claimed['state'][f'day_{streak}_bonus_claimable'])
                self.assertTrue(claimed['state'][f'day_{streak}_bonus_awarded'])
                self.assertEqual(claimed['state']['displayed_streak'], streak)
                self.assertEqual(claimed['state']['total_points'], before['total_points'] + amount)
                response = self.client.get(self.dashboard_url)
                self.assertContains(response, 'id="points-bonus" disabled')
                self.assertNotContains(response, f'Claim +{amount} Points')
                points.delete()

    def test_broken_streak_displays_zero_without_resetting_database(self):
        for streak in (7, 14):
            with self.subTest(streak=streak):
                points = self.state(streak, gap=2)
                before = CustomerPoints.objects.values().get(user=self.user)
                response = self.client.get(self.dashboard_url)
                state = response.context['points_state']
                self.assertTrue(state['streak_broken'])
                self.assertEqual(state['displayed_streak'], 0)
                self.assertEqual(state['total_points'], before['total_points'])
                self.assertTrue(state['daily_claimable'])
                self.assertFalse(state['day_7_bonus_claimable'])
                self.assertFalse(state['day_14_bonus_claimable'])
                self.assertContains(response, 'Streak broken')
                self.assertEqual(CustomerPoints.objects.values().get(user=self.user), before)
                expired = self.post_claim(self.bonus_url).json()
                self.assertEqual(expired['status'], 'expired')
                self.assertEqual(expired['state'], state)
                reset = self.post_claim().json()['state']
                self.assertEqual(reset['displayed_streak'], 1)
                self.assertFalse(reset['streak_broken'])
                self.assertEqual(reset['total_points'], before['total_points'] + 10)
                points.delete()

    def test_yesterday_milestone_can_be_claimed_and_daily_state_matches(self):
        for streak, total, next_streak in ((7, 115, 8), (14, 235, 1)):
            with self.subTest(streak=streak):
                points = self.state(streak, gap=1)
                state = self.client.get(self.dashboard_url).context['points_state']
                self.assertFalse(state['streak_broken'])
                self.assertTrue(state['daily_claimable'])
                self.assertTrue(state[f'day_{streak}_bonus_claimable'])
                claimed = self.post_claim().json()['state']
                self.assertEqual(claimed['total_points'], total)
                self.assertEqual(claimed['displayed_streak'], next_streak)
                self.assertFalse(claimed['daily_claimable'])
                self.assertFalse(claimed['day_7_bonus_claimable'])
                self.assertFalse(claimed['day_14_bonus_claimable'])
                points.delete()

    def test_daily_reaching_milestone_enables_bonus_control(self):
        for streak, label in ((6, 'Claim +35 Points'), (13, 'Claim +50 Points')):
            with self.subTest(streak=streak):
                points = self.state(streak, gap=1)
                payload = self.post_claim().json()
                self.assertEqual(payload['bonus_points_awarded'], 0)
                self.assertEqual(payload['state']['bonus_label'], label)
                self.assertTrue(payload['state'][f'day_{streak + 1}_bonus_claimable'])
                points.delete()

    def test_daily_endpoint_requires_post_and_csrf(self):
        self.assertEqual(self.client.get(self.daily_url).status_code, 405)
        self.assertEqual(self.client.post(self.daily_url).status_code, 403)
        self.assertFalse(CustomerPoints.objects.filter(user=self.user).exists())

    def test_daily_endpoint_rejects_client_reward_values(self):
        for field in ('amount', 'user_id', 'points', 'total_points', 'streak', 'streak_days', 'date'):
            with self.subTest(field=field):
                response = self.post_claim(data={field: 999}, content_type='application/json')
                self.assertEqual(response.status_code, 400)
        self.assertFalse(CustomerPoints.objects.filter(user=self.user).exists())

    def test_non_customer_has_no_controls_and_cannot_claim(self):
        get_user_model().objects.filter(pk=self.user.pk).update(role='user')
        response = self.client.get(self.dashboard_url)
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.context['points_state'])
        self.assertNotContains(response, 'id="points-controls"')
        self.assertNotContains(response, 'id="points-script"')
        for url in (self.daily_url, self.bonus_url):
            self.assertEqual(self.post_claim(url).status_code, 403)
        self.assertFalse(CustomerPoints.objects.filter(user=self.user).exists())

    def test_inactive_customer_cannot_claim_daily(self):
        get_user_model().objects.filter(pk=self.user.pk).update(is_active=False)
        self.assertIn(self.post_claim().status_code, (401, 403))
        self.assertFalse(CustomerPoints.objects.filter(user=self.user).exists())

    def test_anonymous_customer_cannot_claim_daily(self):
        self.client.logout()
        self.client.cookies[settings.CSRF_COOKIE_NAME] = self.csrf_token
        self.assertEqual(self.post_claim().status_code, 401)
        self.assertFalse(CustomerPoints.objects.exists())

    def test_future_date_disables_claims_without_writes(self):
        self.state(7, gap=-1)
        before = CustomerPoints.objects.values().get(user=self.user)
        state = self.client.get(self.dashboard_url).context['points_state']
        self.assertFalse(state['daily_claimable'])
        self.assertFalse(state['day_7_bonus_claimable'])
        self.assertEqual(state['daily_label'], 'Unavailable')
        self.assertEqual(CustomerPoints.objects.values().get(user=self.user), before)

    def test_read_state_uses_utc_calendar_day(self):
        self.state(7, gap=1)
        with timezone.override('Pacific/Honolulu'):
            with patch('django.utils.timezone.now', return_value=NOW.replace(hour=0)):
                state = get_points_state(self.user)
        self.assertTrue(state['daily_claimable'])
        self.assertTrue(state['day_7_bonus_claimable'])
        self.assertFalse(state['streak_broken'])
