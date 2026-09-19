import base64
import json
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Event

from cryptography.fernet import Fernet
from django.contrib import admin
from django.contrib.admin.models import LogEntry
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import close_old_connections, connection, connections, transaction
from django.test import Client, RequestFactory, TestCase, TransactionTestCase, override_settings, skipUnlessDBFeature
from django.urls import reverse
from django.utils import timezone

from .models import CustomerPoints, Reward, RewardCode, Redemption, RewardFulfillment, RewardRequest, RedemptionEvent
from .reward_admin_forms import RewardAdminForm
from .reward_inventory import import_private_inventory
from .test_reward_models import make_reward, make_redemption, make_code

TEST_KEYS = {
    'REWARDS_CODE_ENCRYPTION_KEY': Fernet.generate_key().decode(),
    'REWARDS_CODE_FINGERPRINT_KEY': base64.urlsafe_b64encode(os.urandom(32)).decode(),
    'REWARDS_CODE_KEY_ID': 'test-admin-v1',
}
MODELS = (Reward, RewardCode, Redemption, RewardFulfillment, RewardRequest, RedemptionEvent)


def admin_url(model, action='changelist', obj=None):
    return reverse(f'admin:customerpanel_{model._meta.model_name}_{action}', args=[obj.pk] if obj else [])


def reward_data(reward, **overrides):
    data = {
        'title': reward.title, 'short_description': reward.short_description,
        'full_description': reward.full_description, 'category': reward.category,
        'points_required': str(reward.points_required), 'fulfillment_type': reward.fulfillment_type,
        'terms': reward.terms, 'access_scope': reward.access_scope,
        'stock_remaining': '' if reward.stock_remaining is None else str(reward.stock_remaining),
        'max_redemptions_per_customer': str(reward.max_redemptions_per_customer or ''),
        'edit_token': RewardAdminForm(instance=reward).initial.get('edit_token', ''),
    }
    if reward.is_active:
        data['is_active'] = 'on'
    data.update(overrides)
    return data


@override_settings(**TEST_KEYS)
class RewardsAdminTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.owner = User.objects.create_superuser(username='rewards-owner', password='test-password')
        cls.viewer = User.objects.create_user(username='rewards-viewer', is_staff=True)
        cls.editor = User.objects.create_user(username='rewards-editor', is_staff=True)
        cls.customer = User.objects.create_user(username='rewards-customer', role='customer')
        cls.viewer.user_permissions.set(Permission.objects.filter(content_type__app_label='customerpanel', codename__in=['view_' + model._meta.model_name for model in MODELS]))
        cls.editor.user_permissions.set(Permission.objects.filter(content_type__app_label='customerpanel', codename__in=['view_reward', 'add_reward', 'change_reward', 'view_rewardcode', 'add_rewardcode', 'change_rewardcode']))
        cls.reward = make_reward()
        cls.physical = make_reward(title='Physical catalogue item', category='shopping', fulfillment_type='physical', stock_remaining=3, partner_name='Test Partner')
        cls.redemption = make_redemption(cls.customer, cls.reward)
        cls.code = make_code(cls.reward)
        cls.assigned = make_code(cls.reward)
        RewardCode.objects.filter(pk=cls.assigned.pk).update(redemption=cls.redemption, assigned_at=timezone.now())
        physical_redemption = make_redemption(cls.customer, cls.physical)
        cls.fulfillment = RewardFulfillment.objects.create(redemption=physical_redemption)
        cls.request_record = RewardRequest.objects.create(user=cls.customer, title='Customer idea', description='Please review this idea.')
        cls.audit = RedemptionEvent.objects.create(redemption=cls.redemption, event_type='processing')
        CustomerPoints.objects.create(user=cls.customer, total_points=260, streak_days=7, last_daily_claim_date=timezone.now().date())

    def setUp(self):
        self.client.force_login(self.owner)

    def import_values(self, values, **overrides):
        return self.client.post(admin_url(RewardCode, 'import'), {
            'reward': str(self.reward.pk), 'private_values': values, 'is_active': 'on', **overrides,
        })

    def test_all_models_registered_and_list_pages_render(self):
        for model in MODELS:
            with self.subTest(model=model.__name__):
                self.assertIn(model, admin.site._registry)
                self.assertEqual(self.client.get(admin_url(model)).status_code, 200)

    def test_anonymous_and_non_staff_customer_cannot_access_admin(self):
        for user in (None, self.customer):
            self.client.logout()
            if user:
                self.client.force_login(user)
            for model in MODELS:
                self.assertEqual(self.client.get(admin_url(model)).status_code, 302)
            self.assertEqual(self.client.post(admin_url(RewardCode, 'import'), {'private_values': 'PRIVATE'}).status_code, 302)

    def test_staff_without_model_permissions_is_denied(self):
        staff = get_user_model().objects.create_user(username='no-model-permissions', is_staff=True)
        self.client.force_login(staff)
        for model in MODELS:
            self.assertEqual(self.client.get(admin_url(model)).status_code, 403)
        self.assertEqual(self.import_values('PRIVATE').status_code, 403)

    def test_view_permission_allows_reading_but_not_editing_or_importing(self):
        self.client.force_login(self.viewer)
        for model in MODELS:
            self.assertEqual(self.client.get(admin_url(model)).status_code, 200)
        self.assertEqual(self.client.post(admin_url(Reward, 'change', self.reward), reward_data(self.reward, title='Forged')).status_code, 403)
        self.assertEqual(self.import_values('PRIVATE').status_code, 403)

    def test_import_requires_both_code_add_and_reward_change_permissions(self):
        self.client.force_login(self.editor)
        self.assertEqual(self.import_values('EDITOR-PRIVATE').status_code, 302)
        self.editor.user_permissions.remove(Permission.objects.get(content_type__app_label='customerpanel', codename='change_reward'))
        self.assertEqual(self.import_values('BLOCKED-PRIVATE').status_code, 403)

    def test_owner_can_create_reward_with_requested_fields(self):
        data = reward_data(self.physical, title='New reward', image_url='https://example.com/reward.jpg',
            image_alt='Reward package', partner_name='Partner Company', information_url='https://example.com/info',
            country_code='GB', region='London', city='London', location_details='Collection point',
            fulfillment_instructions='Collect at the desk.', is_exclusive='on', is_active='on')
        response = self.client.post(admin_url(Reward, 'add'), data)
        self.assertEqual(response.status_code, 302)
        saved = Reward.objects.get(title='New reward')
        self.assertEqual(saved.stock_remaining, 3)
        self.assertEqual(saved.image_alt, 'Reward package')
        self.assertEqual(saved.partner_name, 'Partner Company')
        self.assertTrue(saved.is_active)
        self.assertEqual(saved.revision, 1)

    def test_locked_stock_edit_increments_revision_and_preserves_points(self):
        before = CustomerPoints.objects.filter(user=self.customer).values().get()
        response = self.client.post(admin_url(Reward, 'change', self.physical), reward_data(self.physical, stock_remaining='8'))
        self.assertEqual(response.status_code, 302)
        self.physical.refresh_from_db()
        self.assertEqual(self.physical.stock_remaining, 8)
        self.assertEqual(self.physical.revision, 2)
        self.assertEqual(CustomerPoints.objects.filter(user=self.customer).values().get(), before)

    def test_editing_manual_phone_requirement_saves_implied_contact_requirement(self):
        reward = make_reward(fulfillment_type='manual')
        before = CustomerPoints.objects.filter(user=self.customer).values().get()
        response = self.client.post(admin_url(Reward, 'change', reward),
            reward_data(reward, requires_phone='on'))
        self.assertEqual(response.status_code, 302)
        reward.refresh_from_db()
        self.assertTrue(reward.requires_phone)
        self.assertTrue(reward.requires_contact_details)
        self.assertEqual(reward.revision, 2)
        self.assertEqual(CustomerPoints.objects.filter(user=self.customer).values().get(), before)

    def test_stale_form_cannot_overwrite_concurrently_changed_stock(self):
        data = reward_data(self.physical, title='Stale edit')
        Reward.objects.filter(pk=self.physical.pk).update(stock_remaining=1)
        response = self.client.post(admin_url(Reward, 'change', self.physical), data)
        self.assertContains(response, 'Reload the page before saving')
        self.physical.refresh_from_db()
        self.assertEqual(self.physical.stock_remaining, 1)
        self.assertEqual(self.physical.title, 'Physical catalogue item')

    def test_missing_or_forged_edit_token_cannot_save(self):
        for token in ('', 'forged'):
            response = self.client.post(admin_url(Reward, 'change', self.physical), reward_data(self.physical, stock_remaining='100', edit_token=token))
            self.assertContains(response, 'Reload the page before saving')
        self.physical.refresh_from_db()
        self.assertEqual(self.physical.stock_remaining, 3)

    def test_readonly_revision_and_dates_cannot_be_forged(self):
        response = self.client.post(admin_url(Reward, 'change', self.physical), reward_data(self.physical, title='Updated title', revision='500', created_at='2000-01-01'))
        self.assertEqual(response.status_code, 302)
        before = self.physical.created_at
        self.physical.refresh_from_db()
        self.assertEqual(self.physical.revision, 2)
        self.assertEqual(self.physical.created_at, before)

    def test_reward_form_validates_image_access_and_limited_time(self):
        cases = [({'image_url': 'https://example.com/image.jpg'}, 'Describe the image'),
                 ({'access_scope': 'selected_customers'}, 'Select at least one active customer'),
                 ({'is_limited_time': 'on'}, 'Set an end date')]
        for values, message in cases:
            response = self.client.post(admin_url(Reward, 'change', self.physical), reward_data(self.physical, **values))
            self.assertContains(response, message)

    def test_eligible_customer_selection_excludes_inactive_and_non_customer(self):
        inactive = get_user_model().objects.create_user(username='inactive-rewards', role='customer', is_active=False)
        for user in (self.owner, inactive):
            response = self.client.post(admin_url(Reward, 'change', self.physical), reward_data(self.physical, access_scope='selected_customers', eligible_users=[str(user.pk)]))
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.context['adminform'].form.errors)
        response = self.client.post(admin_url(Reward, 'change', self.physical), reward_data(self.physical, access_scope='selected_customers', eligible_users=[str(self.customer.pk)]))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(list(self.physical.eligible_users.all()), [self.customer])

    def test_type_cannot_change_after_inventory_or_redemption_exists(self):
        response = self.client.post(admin_url(Reward, 'change', self.reward), reward_data(self.reward, fulfillment_type='external'))
        self.assertContains(response, 'Create a new reward to change fulfillment type')

    def test_activation_requires_inventory_and_unpublished_rewards_stay_hidden(self):
        reward = make_reward(title='Unpublished voucher')
        response = self.client.post(admin_url(Reward, 'change', reward), reward_data(reward, is_active='on'))
        self.assertContains(response, 'import available private inventory')
        self.client.force_login(self.customer)
        response = self.client.get(reverse('customer_rewards'))
        self.assertContains(response, 'New rewards are on the way.')
        self.assertNotContains(response, reward.title)

    def test_reward_category_partner_search_and_stock_filters(self):
        for query in ({'category__exact': 'shopping'}, {'q': 'Test Partner'}, {'stock': 'available', 'fulfillment_type__exact': 'physical'}):
            response = self.client.get(admin_url(Reward), query)
            self.assertEqual([row.pk for row in response.context['cl'].result_list], [self.physical.pk])
        empty = make_reward(title='Empty voucher')
        response = self.client.get(admin_url(Reward), {'stock': 'empty'})
        self.assertEqual([row.pk for row in response.context['cl'].result_list], [empty.pk])

    def test_code_status_filters_counts_and_safe_search(self):
        expired = make_code(self.reward, expires_at=timezone.now() - timedelta(days=1))
        disabled = make_code(self.reward, is_active=False)
        for state, expected in [('available', self.code), ('assigned', self.assigned), ('expired', expired), ('disabled', disabled)]:
            response = self.client.get(admin_url(RewardCode), {'inventory_status': state})
            self.assertEqual([row.pk for row in response.context['cl'].result_list], [expected.pk])
        response = self.client.get(admin_url(RewardCode), {'q': str(self.code.pk)})
        self.assertEqual([row.pk for row in response.context['cl'].result_list], [self.code.pk])
        response = self.client.get(admin_url(Reward))
        self.assertContains(response, '1 available / 1 assigned / 4 total')

    def test_unassigned_code_can_be_disabled_without_changing_private_data(self):
        url = admin_url(RewardCode, 'change', self.code)
        before = bytes(self.code.encrypted_payload)
        response = self.client.get(url)
        token = response.context['adminform'].form.initial['edit_token']
        response = self.client.post(url, {'edit_token': token, 'expires_at_0': '', 'expires_at_1': '', 'encrypted_payload': 'forged'})
        self.assertEqual(response.status_code, 302)
        self.code.refresh_from_db()
        self.assertFalse(self.code.is_active)
        self.assertEqual(bytes(self.code.encrypted_payload), before)

    def test_assigned_code_is_readonly_and_cannot_be_changed_or_deleted(self):
        url = admin_url(RewardCode, 'change', self.assigned)
        self.assertEqual(self.client.get(url).status_code, 200)
        self.assertEqual(self.client.post(url, {'is_active': '', 'redemption': ''}).status_code, 403)
        self.assertEqual(self.client.post(admin_url(RewardCode, 'delete', self.assigned), {'post': 'yes'}).status_code, 403)

    def test_raw_code_add_is_disabled(self):
        self.assertEqual(self.client.get(admin_url(RewardCode, 'add')).status_code, 403)
        self.assertEqual(self.client.post(admin_url(RewardCode, 'add'), {'encrypted_payload': 'RAW'}).status_code, 403)

    def test_other_models_are_readonly_even_for_superuser(self):
        for obj in (self.redemption, self.fulfillment, self.request_record, self.audit):
            model = type(obj)
            url = admin_url(model, 'change', obj)
            self.assertEqual(self.client.get(url).status_code, 200)
            self.assertEqual(self.client.post(url, {'status': 'fulfilled', 'points_spent': '0', 'balance_after': '9999'}).status_code, 403)
            self.assertEqual(self.client.post(admin_url(model, 'add'), {}).status_code, 403)
            self.assertEqual(self.client.post(admin_url(model, 'delete', obj), {'post': 'yes'}).status_code, 403)
        self.redemption.refresh_from_db()
        self.assertEqual(self.redemption.points_spent, 100)

    def test_bulk_deletion_actions_are_disabled(self):
        request = RequestFactory().get('/admin/')
        request.user = self.owner
        for model in MODELS:
            self.assertNotIn('delete_selected', admin.site._registry[model].get_actions(request))

    def test_private_import_encrypts_and_never_echoes_or_logs_values(self):
        secret = 'PRIVATE-IMPORT-VOUCHER-123'
        response = self.import_values(secret)
        self.assertEqual(response.status_code, 302)
        code = RewardCode.objects.exclude(pk__in=[self.code.pk, self.assigned.pk]).get()
        decoded = json.loads(Fernet(TEST_KEYS['REWARDS_CODE_ENCRYPTION_KEY'].encode()).decrypt(bytes(code.encrypted_payload)))
        self.assertEqual(decoded, {'reward': str(self.reward.pk), 'kind': 'code', 'value': secret})
        self.assertNotIn(secret.encode(), bytes(code.encrypted_payload))
        for url in (admin_url(RewardCode), admin_url(RewardCode, 'change', code), admin_url(RewardCode, 'history', code), admin_url(Reward, 'change', self.reward)):
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200)
            for value in (secret, code.payload_fingerprint, bytes(code.encrypted_payload).decode()):
                self.assertNotContains(response, value)
        for entry in LogEntry.objects.all():
            self.assertNotIn(secret, entry.object_repr + entry.change_message)

    def test_invalid_import_does_not_redisplay_private_values(self):
        secret = 'DO-NOT-ECHO-THIS-CODE'
        response = self.import_values(secret + '\n' + secret)
        self.assertContains(response, 'duplicate private values')
        self.assertNotContains(response, secret)
        self.assertEqual(RewardCode.objects.count(), 2)

    def test_duplicate_against_existing_inventory_rejects_entire_batch(self):
        self.assertEqual(self.import_values('EXISTING-CODE').status_code, 302)
        response = self.import_values('NEW-CODE\nEXISTING-CODE')
        self.assertContains(response, 'Nothing was imported')
        self.assertNotContains(response, 'NEW-CODE')
        self.assertNotContains(response, 'EXISTING-CODE')
        self.assertEqual(RewardCode.objects.count(), 3)

    def test_private_https_links_are_encrypted_and_unsafe_links_rejected(self):
        external = make_reward(fulfillment_type='external')
        private_url = 'https://partner.example/claim?token=PRIVATE-LINK-123'
        response = self.import_values(private_url, reward=str(external.pk))
        self.assertEqual(response.status_code, 302)
        code = external.private_codes.get()
        self.assertEqual(code.payload_kind, 'claim_link')
        for bad in ('http://partner.example/claim', 'javascript:alert(1)', 'https://user:password@partner.example/claim'):
            response = self.import_values(bad, reward=str(external.pk))
            self.assertContains(response, 'valid HTTPS URLs')
            self.assertNotContains(response, bad)
        self.assertEqual(external.private_codes.count(), 1)

    def test_import_is_csrf_protected_and_get_does_not_import(self):
        protected = Client(enforce_csrf_checks=True)
        protected.force_login(self.owner)
        url = admin_url(RewardCode, 'import')
        self.assertContains(protected.get(url), 'admin/css/forms.css')
        self.assertContains(protected.get(admin_url(RewardCode)), url)
        protected.force_login(self.viewer)
        self.assertNotContains(protected.get(admin_url(RewardCode)), url)
        protected.force_login(self.owner)
        self.assertEqual(protected.post(url, {'private_values': 'CSRF-TEST', 'reward': str(self.reward.pk)}).status_code, 403)
        self.assertEqual(RewardCode.objects.count(), 2)

    def test_missing_keys_fail_closed_without_plaintext_fallback(self):
        with override_settings(REWARDS_CODE_ENCRYPTION_KEY='', REWARDS_CODE_FINGERPRINT_KEY=''):
            response = self.import_values('SECRET-NO-KEY')
        self.assertContains(response, 'not configured')
        self.assertNotContains(response, 'SECRET-NO-KEY')
        self.assertEqual(RewardCode.objects.count(), 2)

    def test_import_caps_batch_size_and_rejects_expired_inventory(self):
        response = self.import_values('\n'.join(f'VALUE-{i}' for i in range(201)))
        self.assertContains(response, 'between 1 and 200')
        response = self.import_values('EXPIRED-PRIVATE', expires_at='2000-01-01 12:00')
        self.assertContains(response, 'future expiry')
        self.assertNotContains(response, 'EXPIRED-PRIVATE')

    def test_code_service_checks_permissions_without_http_wrapper(self):
        with self.assertRaises(PermissionDenied):
            import_private_inventory(self.customer, self.reward.pk, ['PRIVATE'])
        with self.assertRaises(PermissionDenied):
            import_private_inventory(self.viewer, self.reward.pk, ['PRIVATE'])

    def test_successful_import_and_admin_edit_do_not_change_points(self):
        before = CustomerPoints.objects.filter(user=self.customer).values().get()
        self.assertEqual(self.import_values('NO-POINTS-CHANGE').status_code, 302)
        self.assertEqual(CustomerPoints.objects.filter(user=self.customer).values().get(), before)

    def test_private_fields_cannot_be_used_as_admin_filters(self):
        response = self.client.get(admin_url(RewardCode), {'payload_fingerprint__exact': self.code.payload_fingerprint})
        self.assertEqual(response.status_code, 400)


@override_settings(**TEST_KEYS)
@skipUnlessDBFeature('has_select_for_update')
class ConcurrentRewardsAdminTests(TransactionTestCase):
    def setUp(self):
        self.owner = get_user_model().objects.create_superuser(username='inventory-owner', password='test')
        self.reward = make_reward()

    def test_import_waits_for_reward_lock_and_rechecks_type(self):
        started = Event()
        def contender():
            close_old_connections()
            def track(execute, sql, params, many, context):
                if 'customerpanel_reward' in sql and 'FOR UPDATE' in sql:
                    started.set()
                return execute(sql, params, many, context)
            try:
                with connection.execute_wrapper(track):
                    import_private_inventory(self.owner, self.reward.pk, ['CONCURRENT-PRIVATE'])
                return 'imported'
            except ValidationError:
                return 'rejected'
            finally:
                connections.close_all()
        with ThreadPoolExecutor(max_workers=1) as pool:
            with transaction.atomic():
                reward = Reward.objects.select_for_update().get(pk=self.reward.pk)
                future = pool.submit(contender)
                self.assertTrue(started.wait(10))
                self.assertFalse(future.done())
                reward.fulfillment_type = 'physical'
                reward.save(update_fields=['fulfillment_type'])
            self.assertEqual(future.result(timeout=15), 'rejected')
        self.assertFalse(RewardCode.objects.exists())

    def test_concurrent_duplicate_import_creates_one_code(self):
        def contender(_):
            close_old_connections()
            try:
                import_private_inventory(self.owner, self.reward.pk, ['SAME-PRIVATE-CODE'])
                return 'imported'
            except ValidationError:
                return 'rejected'
            finally:
                connections.close_all()
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertCountEqual(list(pool.map(contender, range(2))), ['imported', 'rejected'])
        self.assertEqual(RewardCode.objects.count(), 1)
        self.assertFalse(CustomerPoints.objects.exists())
