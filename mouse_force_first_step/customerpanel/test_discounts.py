from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from decimal import Decimal
from threading import Event
from unittest.mock import patch
from uuid import uuid4

from django.contrib import admin
from django.contrib.auth import get_user_model
from django.core import signing
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, connection, connections, close_old_connections, transaction
from django.db.models.deletion import ProtectedError
from django.test import Client, RequestFactory, TestCase, TransactionTestCase, skipUnlessDBFeature
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from . import discounts
from .discount_admin import DiscountForm
from .models import Discount, DiscountVote, CustomerDiscountAccess, CustomerPoints
from .points import claim_daily_points, claim_streak_bonus
from .section_access import create_unlock_confirmation, unlock_section
from .section_test_support import seed_paid_access

SESSION = 'isolated-discount-test-session'


def make_deal(**changes):
    values = dict(brand='Test Bistro', title='A second main for £1', category='food-drink',
        deal_type='fixed', value_label='Second main for £1', short_description='Test-only offer.',
        details='Complete public details.', eligibility='Selected meals, weekdays only.',
        country='United Kingdom', region='London', usage_channels=['online', 'dine-in'],
        promo_code='TEST-ONLY-PROTECTED-CODE', official_url='https://example.test/paid-deal',
        valid_until=timezone.now() + timedelta(days=10), terms_summary='Public test terms.',
        points_to_unlock_deal=5, active=True)
    values.update(changes)
    return Discount.objects.create(**values)


class DiscountFixtures:
    def setUp(self):
        super().setUp()
        self.user = get_user_model().objects.create_user(username='discount-customer', role='customer')
        self.other = get_user_model().objects.create_user(username='discount-other', role='customer')
        seed_paid_access(self.user, self.other, sections=('discounts',))
        self.points = CustomerPoints.objects.create(user=self.user, total_points=50, streak_days=7,
            last_daily_claim_date=timezone.now().date(), day_7_bonus_awarded=False)
        self.deal = make_deal()

    def quote(self, deal=None, user=None, session=SESSION):
        return discounts.create_deal_confirmation(user or self.user, (deal or self.deal).pk, session_key=session)

    def buy(self, quote=None, deal=None, user=None, session=SESSION):
        return discounts.unlock_deal(user or self.user, (deal or self.deal).pk, (quote or self.quote(deal))['token'], session_key=session)

    def balance(self):
        return CustomerPoints.objects.get(user=self.user).total_points


class DiscountCatalogueTests(DiscountFixtures, TestCase):
    def test_search_matches_brand_title_category_country_keywords(self):
        self.deal.search_keywords = 'meal delivery'
        self.deal.save()
        for term in ('bistro', 'SECOND', 'Food & Drink', 'Kingdom', 'delivery', 'bistro meal'):
            with self.subTest(term=term):
                self.assertEqual([d['id'] for d in discounts.catalogue(self.user, {'q': term})['deals']], [self.deal.pk])
        self.assertFalse(discounts.catalogue(self.user, {'q': 'unmatched'})['deals'])

    def test_category_counts_follow_search_and_type_without_selected_category_bias(self):
        make_deal(brand='Test Rail', category='travel', deal_type='percent', value_label='23% off')
        make_deal(brand='Test Bistro', category='food-drink', deal_type='percent', value_label='30% off')
        result = discounts.catalogue(self.user, {'type': 'percent', 'category': 'food-drink'})
        counts = {c['key']: c['count'] for c in result['discount_categories']}
        self.assertEqual((counts['all'], counts['food-drink'], counts['travel']), (2, 1, 1))
        self.assertEqual(result['discount_count'], 1)
        result = discounts.catalogue(self.user, {'q': 'Rail'})
        self.assertEqual(result['discount_categories'][0]['count'], 1)

    def test_expired_inactive_future_hidden_and_ongoing_current(self):
        for change in ({'active': False}, {'valid_until': timezone.now() - timedelta(seconds=1)},
                       {'valid_from': timezone.now() + timedelta(days=1)}):
            make_deal(**change)
        ongoing = make_deal(ongoing=True, valid_until=None)
        result = discounts.catalogue(self.user, {})
        self.assertCountEqual([d['id'] for d in result['deals']], [self.deal.pk, ongoing.pk])

    def test_expiry_boundary_and_ongoing_does_not_override_future_start(self):
        instant = timezone.now()
        Discount.objects.filter(pk=self.deal.pk).update(valid_until=instant)
        make_deal(ongoing=True, valid_until=None, valid_from=instant + timedelta(days=1))
        with patch('django.utils.timezone.now', return_value=instant):
            self.assertFalse(discounts.catalogue(self.user, {})['deals'])

    def test_bounded_query_and_pagination(self):
        for i in range(13):
            make_deal(title=f'Test offer {i}')
        result = discounts.catalogue(self.user, {'page': '2'})
        self.assertEqual(len(result['deals']), 2)
        self.assertEqual(result['discount_count'], 14)
        self.assertTrue(result['discount_previous'])
        result = discounts.catalogue(self.user, {'q': 'x' * 500, 'category': 'invalid', 'type': 'invalid'})
        self.assertEqual(len(result['discount_query']), 100)
        self.assertEqual((result['discount_category'], result['discount_type']), ('all', 'all'))

    def test_catalogue_projection_omits_codes_and_links_even_for_owner(self):
        self.buy()
        result = discounts.catalogue(self.user, {})
        self.assertNotIn('promo_code', result['deals'][0])
        self.assertNotIn('official_url', result['deals'][0])
        self.assertNotIn(self.deal.promo_code, str(result))
        self.assertNotIn(self.deal.official_url, str(result))

    def test_purchased_expired_inactive_deal_retained_only_for_owner(self):
        self.buy()
        Discount.objects.filter(pk=self.deal.pk).update(active=False)
        self.assertFalse(discounts.catalogue(self.user, {})['deals'])
        saved = discounts.catalogue(self.user, {'access': 'unlocked'})['deals']
        self.assertEqual(saved[0]['id'], self.deal.pk)
        row = discounts.get_customer_deal(self.user, self.deal.pk)
        self.assertEqual(discounts.present_deal(row, detail=True)['promo_code'], self.deal.promo_code)
        from django.http import Http404
        with self.assertRaises(Http404):
            discounts.get_customer_deal(self.other, self.deal.pk)


class DiscountAccessTests(DiscountFixtures, TestCase):
    def test_confirmation_read_only_signed_and_no_protected_values(self):
        quote = self.quote()
        self.assertEqual((quote['balance'], quote['cost'], quote['balance_after']), (50, 5, 45))
        claims = signing.loads(quote['token'], salt=discounts.SALT)
        self.assertNotIn(self.deal.promo_code, str(claims))
        self.assertNotIn(self.deal.official_url, str(claims))
        self.assertFalse(CustomerDiscountAccess.objects.exists())
        self.assertEqual(self.balance(), 50)

    def test_paid_unlock_changes_only_total_and_retry_charges_zero(self):
        before = CustomerPoints.objects.values().get(user=self.user)
        quote = self.quote()
        first, retry = self.buy(quote), self.buy(quote)
        self.assertEqual((first['points_spent'], retry['points_spent']), (5, 0))
        self.assertEqual(first['access_id'], retry['access_id'])
        self.assertEqual(CustomerPoints.objects.values().get(user=self.user), {**before, 'total_points': 45})
        self.assertEqual(CustomerDiscountAccess.objects.count(), 1)
        self.assertNotIn(self.deal.promo_code, str(first))
        self.assertNotIn(self.deal.official_url, str(first))

    def test_free_access_needs_no_purchase_or_points_row(self):
        deal = make_deal(points_to_unlock_deal=0)
        row = discounts.get_customer_deal(self.other, deal.pk)
        self.assertEqual(discounts.present_deal(row, detail=True)['promo_code'], deal.promo_code)
        self.assertEqual(self.buy(self.quote(deal, self.other), deal, self.other)['points_spent'], 0)
        self.assertFalse(CustomerPoints.objects.filter(user=self.other).exists())
        self.assertFalse(CustomerDiscountAccess.objects.exists())

    def test_insufficient_balance_and_missing_row(self):
        for user in (self.user, self.other):
            CustomerPoints.objects.filter(user=user).update(total_points=4)
            quote = self.quote(user=user)
            self.assertIsNone(quote['balance_after'])
            with self.assertRaisesRegex(discounts.DealError, 'insufficient_points'):
                self.buy(quote, user=user)
        self.assertFalse(CustomerDiscountAccess.objects.exists())

    def test_tampered_expired_wrong_customer_and_session_tokens(self):
        quote = self.quote()
        for token, user, session in ((quote['token']+'x', self.user, SESSION), (quote['token'], self.other, SESSION),
                (quote['token'], self.user, 'new-session')):
            with self.assertRaisesRegex(discounts.DealError, 'invalid_confirmation'):
                self.buy({'token': token}, user=user, session=session)
        with patch('django.core.signing.time.time', return_value=timezone.now().timestamp() - discounts.MAX_AGE - 30):
            quote = self.quote()
        with self.assertRaisesRegex(discounts.DealError, 'expired_confirmation'):
            self.buy(quote)

    def test_offer_or_balance_change_requires_new_review(self):
        quote = self.quote()
        for field, value in (('points_to_unlock_deal', 10), ('terms_summary', 'Changed terms'),
                             ('official_url', 'https://example.test/changed'), ('eligibility', 'New customers only')):
            old = getattr(self.deal, field)
            Discount.objects.filter(pk=self.deal.pk).update(**{field: value})
            with self.assertRaisesRegex(discounts.DealError, 'offer_changed'):
                self.buy(quote)
            Discount.objects.filter(pk=self.deal.pk).update(**{field: old})
        CustomerPoints.objects.filter(user=self.user).update(total_points=55)
        with self.assertRaisesRegex(discounts.DealError, 'balance_changed'):
            self.buy(quote)
        self.assertFalse(CustomerDiscountAccess.objects.exists())

    def test_expired_or_inactive_cannot_be_purchased(self):
        quote = self.quote()
        for change in ({'active': False}, {'active': True, 'valid_until': timezone.now() - timedelta(days=1)}):
            Discount.objects.filter(pk=self.deal.pk).update(**change)
            with self.assertRaisesRegex(discounts.DealError, 'unavailable'):
                self.buy(quote)
        self.assertEqual(self.balance(), 50)

    def test_failure_after_deduction_and_outer_rollback(self):
        quote = self.quote()
        with patch.object(CustomerDiscountAccess.objects, 'create', side_effect=IntegrityError('isolated failure')):
            with self.assertRaises(IntegrityError):
                self.buy(quote)
        self.assertEqual(self.balance(), 50)
        with transaction.atomic():
            self.buy(quote)
            transaction.set_rollback(True)
        self.assertEqual(self.balance(), 50)
        self.assertFalse(CustomerDiscountAccess.objects.exists())

    def test_existing_access_survives_zero_balance_expiry_new_session_and_price_edit(self):
        first = self.buy()
        CustomerPoints.objects.filter(user=self.user).update(total_points=0)
        Discount.objects.filter(pk=self.deal.pk).update(active=False, points_to_unlock_deal=100)
        retry = self.buy({'token': ''}, session='different-device')
        self.assertEqual(retry['access_id'], first['access_id'])
        self.assertEqual(retry['points_spent'], 0)

    def test_unique_immutable_receipt_and_protected_discount(self):
        self.buy()
        for action in (lambda: CustomerDiscountAccess.objects.create(user=self.user, discount=self.deal, points_spent=5, balance_after=45),
                lambda: CustomerDiscountAccess.objects.update(points_spent=1), lambda: CustomerDiscountAccess.objects.all().delete()):
            with self.assertRaises(IntegrityError), transaction.atomic():
                action()
        with self.assertRaises(ProtectedError):
            self.deal.delete()

    def test_active_customer_and_section_required(self):
        outsider = get_user_model().objects.create_user(username='locked-discount-user', role='customer')
        with self.assertRaises(PermissionDenied):
            discounts.unlock_deal(outsider, self.deal.pk, '', session_key=SESSION)
        quote = self.quote()
        get_user_model().objects.filter(pk=self.user.pk).update(is_active=False)
        with self.assertRaises(PermissionDenied):
            self.buy(quote)


class DiscountVoteTests(DiscountFixtures, TestCase):
    def test_create_repeat_switch_remove_and_no_points_change(self):
        before = CustomerPoints.objects.values().get(user=self.user)
        for value, score, count in ((1,1,1), (1,1,1), (-1,-1,1), (0,0,0), (0,0,0)):
            result = discounts.set_vote(self.user, self.deal.pk, value)
            self.assertEqual((result['score'], result['vote']), (score, value))
            self.assertEqual((result['likes'], result['dislikes']), (int(value == 1), int(value == -1)))
            self.assertEqual(DiscountVote.objects.count(), count)
        self.assertEqual(CustomerPoints.objects.values().get(user=self.user), before)

    def test_separate_like_and_dislike_counts_in_response_catalogue_and_detail(self):
        empty = discounts.catalogue(self.user, {})['deals'][0]
        self.assertEqual((empty['likes'], empty['dislikes'], empty['vote']), (0, 0, 0))
        discounts.set_vote(self.user, self.deal.pk, 1)
        result = discounts.set_vote(self.other, self.deal.pk, -1)
        self.assertEqual((result['likes'], result['dislikes'], result['vote']), (1, 1, -1))
        for row in (discounts.catalogue(self.user, {})['deals'][0],
                    discounts.present_deal(discounts.get_customer_deal(self.user, self.deal.pk), detail=True)):
            self.assertEqual((row['likes'], row['dislikes'], row['vote']), (1, 1, 1))
        result = discounts.set_vote(self.other, self.deal.pk, 1)
        self.assertEqual((result['likes'], result['dislikes']), (2, 0))
        row = discounts.catalogue(self.user, {})['deals'][0]
        self.assertEqual((row['likes'], row['dislikes'], row['vote']), (2, 0, 1))

    def test_unique_and_value_constraints(self):
        discounts.set_vote(self.user, self.deal.pk, 1)
        with self.assertRaises(IntegrityError), transaction.atomic():
            DiscountVote.objects.create(user=self.user, discount=self.deal, value=-1)
        with self.assertRaises(IntegrityError), transaction.atomic():
            DiscountVote.objects.filter(user=self.user).update(value=0)
        for value in (True, 2, '1', None):
            with self.assertRaises(discounts.DealError):
                discounts.set_vote(self.user, self.deal.pk, value)

    def test_unpublished_or_locked_section_cannot_vote(self):
        Discount.objects.filter(pk=self.deal.pk).update(active=False)
        with self.assertRaisesRegex(discounts.DealError, 'unavailable'):
            discounts.set_vote(self.user, self.deal.pk, 1)
        self.assertFalse(DiscountVote.objects.exists())


class DiscountRequestTests(DiscountFixtures, TestCase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.user)

    def url(self, name='detail'):
        return reverse('customer_discount_'+name, args=[self.deal.pk])

    def test_feedback_counts_and_selected_state_render_on_both_pages(self):
        self.client.post(self.url('vote'), {'value': 1})
        discounts.set_vote(self.other, self.deal.pk, -1)
        for url in (reverse('customer_discounts'), self.url()):
            response = self.client.get(url)
            self.assertContains(response, 'Remove like, 1 likes')
            self.assertContains(response, 'Dislike this deal, 1 dislikes')
            self.assertContains(response, 'data-vote-count>1</span>', count=2)
            self.assertContains(response, 'aria-pressed="true"', count=1)
            self.assertContains(response, '👍')
            self.assertContains(response, '👎')
            self.assertNotContains(response, 'data-vote-score')
        removed = self.client.post(self.url('vote'), {'value': 0}, HTTP_ACCEPT='application/json').json()
        self.assertEqual((removed['likes'], removed['dislikes'], removed['vote']), (0, 1, 0))
        self.assertEqual(self.balance(), 50)

    def test_public_html_and_json_do_not_contain_paid_payload(self):
        for url, headers in ((reverse('customer_discounts'), {}), (reverse('customer_discounts'), {'HTTP_X_DISCOUNT_RESULTS': '1'}),
                             (self.url(), {}), (self.url('confirmation'), {})):
            response = self.client.get(url, **headers)
            self.assertEqual(response.status_code, 200)
            self.assertNotContains(response, self.deal.promo_code)
            self.assertNotContains(response, self.deal.official_url)
            self.assertIn('no-store', response['Cache-Control'])

    def test_purchase_then_refresh_and_new_login_reveals_only_for_owner(self):
        quote = self.client.get(self.url('confirmation')).json()
        result = self.client.post(self.url('unlock'), {'confirmation_token': quote['token']})
        self.assertEqual(result.status_code, 200)
        self.assertNotIn(self.deal.promo_code, str(result.json()))
        self.assertEqual(self.client.post(self.url('unlock'), {'confirmation_token': quote['token']}).json()['points_spent'], 0)
        for _ in range(2):
            self.assertContains(self.client.get(self.url()), self.deal.promo_code)
        self.client.logout(); self.client.force_login(self.user)
        self.assertContains(self.client.get(self.url()), self.deal.official_url)
        self.client.force_login(self.other)
        self.assertNotContains(self.client.get(self.url()), self.deal.promo_code)
        self.assertEqual(self.balance(), 45)

    def test_stale_detail_detected_before_confirmation(self):
        version = self.client.get(self.url()).context['deal']['offer_version']
        Discount.objects.filter(pk=self.deal.pk).update(terms_summary='New terms')
        response = self.client.get(self.url('confirmation'), {'offer': version})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()['error'], 'offer_changed')

    def test_direct_urls_actions_require_customer_and_section(self):
        locked = get_user_model().objects.create_user(username='locked-section', role='customer')
        self.client.force_login(locked)
        for name, method in [('detail','get'),('confirmation','get'),('unlock','post'),('vote','post')]:
            response = getattr(self.client,method)(self.url(name), HTTP_ACCEPT='application/json')
            self.assertEqual(response.status_code, 403)
            self.assertEqual(response.json()['error'], 'section_locked')
        self.client.logout()
        self.assertEqual(self.client.get(self.url()).status_code, 302)
        self.assertEqual(self.client.post(self.url('vote'), {'value': 1}).status_code, 401)
        self.client.force_login(self.other)
        get_user_model().objects.filter(pk=self.other.pk).update(role='user',is_staff=True)
        self.assertEqual(self.client.post(self.url('unlock')).status_code, 403)

    def test_csrf_forged_payload_method_and_get_never_spends(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.user)
        client.get(self.url())
        token = client.cookies['csrftoken'].value
        quote = client.get(self.url('confirmation')).json()
        body = {'confirmation_token': quote['token']}
        self.assertEqual(client.post(self.url('unlock'),body).status_code,403)
        self.assertEqual(client.post(self.url('vote'),{'value':1}).status_code,403)
        for key in ('user_id','cost','balance','points','date','discount_id'):
            self.assertEqual(client.post(self.url('unlock'),{**body,key:'1'},HTTP_X_CSRFTOKEN=token).status_code,400)
        self.assertEqual(client.get(self.url('unlock')).status_code,405)
        self.assertEqual(client.get(self.url('vote')).status_code,405)
        self.assertEqual(self.balance(),50)
        self.assertFalse(CustomerDiscountAccess.objects.exists())
        response=client.post(self.url('vote'),{'value':'1'},HTTP_X_CSRFTOKEN=token,HTTP_ACCEPT='application/json')
        self.assertEqual(response.json()['score'],1)

    def test_unknown_or_expired_uuid_cannot_reveal(self):
        self.assertEqual(self.client.get(reverse('customer_discount_detail',args=[uuid4()])).status_code,404)
        Discount.objects.filter(pk=self.deal.pk).update(valid_until=timezone.now()-timedelta(days=1))
        self.assertEqual(self.client.get(self.url()).status_code,404)

    def test_search_fragment_and_html_are_escaped(self):
        Discount.objects.filter(pk=self.deal.pk).update(brand='<script>unsafe</script>')
        response=self.client.get(reverse('customer_discounts'),{'q':'<script>'},HTTP_X_DISCOUNT_RESULTS='1')
        self.assertEqual(response.status_code,200)
        self.assertNotIn('<script>unsafe</script>',response.json()['html'])
        self.assertIn('&lt;script&gt;',response.json()['html'])


class DiscountAdminTests(DiscountFixtures, TestCase):
    def setUp(self):
        super().setUp()
        self.staff=get_user_model().objects.create_superuser(username='discount-staff',password='test-only',email='admin@example.test')
        self.client.force_login(self.staff)

    def test_registration_and_readonly_receipts_votes(self):
        request=RequestFactory().get('/admin/');request.user=self.staff
        for model in (Discount,CustomerDiscountAccess,DiscountVote):
            self.assertIn(model,admin.site._registry)
        for model in (CustomerDiscountAccess,DiscountVote):
            obj=admin.site._registry[model]
            self.assertFalse(obj.has_add_permission(request))
            self.assertFalse(obj.has_change_permission(request))
            self.assertFalse(obj.has_delete_permission(request))
            self.assertTrue(obj.has_view_permission(request))

    def test_search_filters_and_lists_omit_retailer_payload(self):
        make_deal(brand='Test Rail',category='travel',deal_type='percent')
        url=reverse('admin:customerpanel_discount_changelist')
        for query in ({'q':'Bistro'},{'category__exact':'food-drink'},{'deal_type__exact':'fixed'}):
            response=self.client.get(url,query)
            self.assertEqual([d.pk for d in response.context['cl'].result_list],[self.deal.pk])
            self.assertNotContains(response,self.deal.promo_code)
            self.assertNotContains(response,self.deal.official_url)

    def test_validation_handles_examples_without_unneeded_numeric_fields(self):
        for changes in ({'deal_type':'fixed','value_label':'Second main for £1'},
                        {'deal_type':'percent','percentage_value':Decimal('30'),'eligibility':'Students, Sunday–Thursday'},
                        {'deal_type':'money','money_off_value':Decimal('8')},
                        {'ongoing':True,'valid_until':None,'usage_channels':['online','in-store']}):
            deal=make_deal(**changes);deal.full_clean()
        self.assertEqual(DiscountForm().fields['usage_channels'].choices,Discount.CHANNELS)

    def test_constraints_bad_dates_values_and_invalid_urls(self):
        for change in ({'points_to_unlock_deal':-1},{'percentage_value':101}, {'money_off_value':-1},
                {'ongoing':True},{'valid_from':timezone.now()+timedelta(days=20)}):
            with self.assertRaises(IntegrityError),transaction.atomic():
                Discount.objects.filter(pk=self.deal.pk).update(**change)
        for url in ('javascript:alert(1)','https://user:secret@example.test/x'):
            self.deal.official_url=url
            with self.assertRaises(ValidationError):self.deal.full_clean()

    def test_published_free_deal_stays_free_even_after_deactivation(self):
        free=make_deal(points_to_unlock_deal=0)
        free.refresh_from_db();self.assertIsNotNone(free.free_published_at)
        Discount.objects.filter(pk=free.pk).update(active=False)
        for change in ({'points_to_unlock_deal':5},{'free_published_at':None}):
            with self.assertRaises(IntegrityError),transaction.atomic():
                Discount.objects.filter(pk=free.pk).update(**change)


@skipUnlessDBFeature('has_select_for_update')
class DiscountConcurrencyTests(DiscountFixtures,TransactionTestCase):
    available_apps=['django.contrib.auth','django.contrib.contenttypes','mouse_force_first_step.accounts','mouse_force_first_step.customerpanel']

    def worker(self,operation,event):
        close_old_connections()
        def observe(execute,sql,params,many,context):
            if 'FOR UPDATE' in sql and get_user_model()._meta.db_table in sql:event.set()
            return execute(sql,params,many,context)
        try:
            with connection.cursor() as cursor:cursor.execute("SET lock_timeout = '10s'")
            with connection.execute_wrapper(observe):
                try:return operation()
                except discounts.DealError as error:return error.code
        finally:connections.close_all()

    def race(self,operations):
        events=[Event() for _ in operations]
        with ThreadPoolExecutor(max_workers=len(operations)) as pool:
            with transaction.atomic():
                get_user_model().objects.select_for_update().get(pk=self.user.pk)
                futures=[pool.submit(self.worker,fn,event) for fn,event in zip(operations,events)]
                for event in events:self.assertTrue(event.wait(10))
            return [future.result(timeout=20) for future in futures]

    def test_same_deal_and_distinct_quotes_charge_once(self):
        first,second=self.quote(),self.quote()
        results=self.race([lambda:self.buy(first),lambda:self.buy(second)])
        self.assertCountEqual([r['points_spent'] for r in results],[5,0])
        self.assertEqual(results[0]['access_id'],results[1]['access_id'])
        self.assertEqual(self.balance(),45)

    def test_different_deals_cannot_spend_same_points(self):
        CustomerPoints.objects.filter(user=self.user).update(total_points=5)
        other=make_deal();first,second=self.quote(),self.quote(other)
        results=self.race([lambda:self.buy(first),lambda:self.buy(second,other)])
        self.assertEqual(results.count('insufficient_points'),1)
        self.assertEqual(CustomerDiscountAccess.objects.count(),1)
        self.assertEqual(self.balance(),0)

    def test_duplicate_votes_have_one_row_and_retries_are_idempotent(self):
        self.race([lambda:discounts.set_vote(self.user,self.deal.pk,1),lambda:discounts.set_vote(self.user,self.deal.pk,1)])
        self.assertEqual(DiscountVote.objects.count(),1)
        self.assertEqual(DiscountVote.objects.get().value,1)

    def test_lock_order_matches_existing_points_and_sections(self):
        quote=self.quote()
        with CaptureQueriesContext(connection) as queries:self.buy(quote)
        locks=[q['sql'] for q in queries if 'FOR UPDATE' in q['sql']]
        self.assertEqual(len(locks),4)
        for sql,model in zip(locks,(get_user_model(),CustomerPoints,Discount,CustomerDiscountAccess)):
            self.assertIn(model._meta.db_table,sql)

    def test_daily_bonus_and_section_winner_require_fresh_balance_review(self):
        for operation in (lambda:claim_daily_points(self.user),lambda:claim_streak_bonus(self.user),
                lambda:unlock_section(self.user,'news',create_unlock_confirmation(self.user,'news',session_key=SESSION)['token'],session_key=SESSION)):
            CustomerPoints.objects.filter(user=self.user).update(total_points=50,streak_days=7,
                last_daily_claim_date=timezone.now().date()-timedelta(days=1),day_7_bonus_awarded=False)
            quote=self.quote();event=Event()
            with ThreadPoolExecutor(max_workers=1) as pool:
                with transaction.atomic():
                    get_user_model().objects.select_for_update().get(pk=self.user.pk)
                    future=pool.submit(self.worker,lambda:self.buy(quote),event)
                    self.assertTrue(event.wait(10));operation()
                self.assertEqual(future.result(timeout=20),'balance_changed')
            self.assertFalse(CustomerDiscountAccess.objects.exists())

    def test_waiting_bonus_keeps_both_balance_changes(self):
        event=Event()
        with ThreadPoolExecutor(max_workers=1) as pool:
            with transaction.atomic():
                self.buy()
                future=pool.submit(self.worker,lambda:claim_streak_bonus(self.user),event)
                self.assertTrue(event.wait(10))
            self.assertEqual(future.result(timeout=20).awarded_amount,35)
        self.assertEqual(self.balance(),80)
