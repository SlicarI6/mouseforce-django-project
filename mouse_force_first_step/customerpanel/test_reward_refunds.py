from .section_test_support import seed_paid_access
"""Full refunds, finite inventory safety and private customer history."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Event
from unittest.mock import patch
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import connection, transaction
from django.test import Client, TestCase, TransactionTestCase, override_settings, skipUnlessDBFeature
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from . import test_reward_allocation as digital
from . import test_reward_requests as requests
from .models import CustomerPoints, Reward, RewardCode, RewardFulfillment, RewardRequest, Redemption, RedemptionEvent
from .points import claim_daily_points, claim_streak_bonus
from .reward_confirmation import create_reward_confirmation
from .reward_fulfillment import process_fulfillment
from .reward_private_access import get_private_reward_benefit, PrivateBenefitUnavailable
from .reward_redemption import redeem_reward
from .reward_refunds import refund_confirmation, refund_redemption
from .test_reward_fulfillment import DELIVERY

NOW, SESSION = digital.NOW, digital.SESSION


class RefundFixtures(requests.RequestFixtures):
    def purchase(self, kind='manual', stock=3):
        Reward.objects.filter(pk=self.reward.pk).update(fulfillment_type=kind,
            stock_remaining=stock if kind in ('physical', 'manual') else None)
        self.reward.refresh_from_db()
        if kind in ('voucher', 'external'):
            self.inventory()
        quote = self.quote()
        result = redeem_reward(self.user, self.reward.pk, quote.token, session_key=SESSION,
            fulfillment_data=DELIVERY if kind == 'physical' else None)
        return Redemption.objects.get(pk=result.redemption_id)

    def refund_args(self, record, **changes):
        record.refresh_from_db()
        values = {'reason': 'unusable', 'stock_action': 'none', 'internal_note': 'Verified isolated test case; no private inventory values.',
            'approved': True, 'confirmation_token': refund_confirmation(self.staff, record,
                RewardFulfillment.objects.filter(redemption=record).first())}
        values.update(changes)
        return values

    def refund(self, record, **changes):
        return refund_redemption(self.staff, record.pk, **self.refund_args(record, **changes))


@override_settings(**digital.TEST_KEYS)
class RefundServiceTests(RefundFixtures, TestCase):
    def test_full_refund_uses_original_price_and_only_changes_total_points(self):
        record = self.purchase()
        before = CustomerPoints.objects.values().get(user=self.user)
        snapshot = Redemption.objects.values().get(pk=record.pk)
        Reward.objects.filter(pk=self.reward.pk).update(points_required=999, revision=2)
        result = self.refund(record)
        self.assertEqual(result.refunded_points, 100)
        self.assertEqual(CustomerPoints.objects.values().get(user=self.user), {**before, 'total_points': 260})
        record.refresh_from_db()
        for key, value in snapshot.items():
            if key.startswith('snapshot_') or key in ('balance_after', 'points_spent', 'user_id', 'reward_id'):
                self.assertEqual(getattr(record, key), value)
        self.assertEqual((record.status, record.refunded_points, record.refunded_at), ('cancelled', 100, NOW))
        self.assertEqual(record.fulfillment.status, 'cancelled')
        event = record.events.get(event_type='refunded')
        self.assertEqual((event.points_delta, event.balance_after, event.actor), (100, 260, self.staff))

    def test_refund_adds_to_current_balance_not_original_balance(self):
        record = self.purchase()
        CustomerPoints.objects.filter(user=self.user).update(total_points=19)
        self.refund(record)
        self.assertEqual(self.balance(), 119)
        self.assertEqual(Redemption.objects.get(pk=record.pk).balance_after, 160)

    def test_duplicate_refund_with_original_token_does_nothing(self):
        record = self.purchase('physical')
        args = self.refund_args(record, reason='cancellation', stock_action='cancelled', stock_verified=True)
        first = refund_redemption(self.staff, record.pk, **args)
        second = refund_redemption(self.staff, record.pk, **args)
        self.assertFalse(first.replayed); self.assertTrue(second.replayed)
        self.assertEqual(self.balance(), 260)
        self.assertEqual(Reward.objects.get(pk=self.reward.pk).stock_remaining, 3)
        self.assertEqual(record.events.filter(event_type='refunded').count(), 1)

    def test_permissions_approval_reason_note_and_forged_token_fail_closed(self):
        record = self.purchase()
        with self.assertRaises(PermissionDenied):
            refund_redemption(self.user, record.pk, **self.refund_args(record))
        for bad in ({'approved': False}, {'reason': 'timeout'}, {'internal_note': ''}, {'confirmation_token': 'forged'}):
            with self.assertRaises(ValidationError):
                self.refund(record, **bad)
        self.assertEqual(self.balance(), 160)
        self.assertFalse(record.events.filter(event_type='refunded').exists())

    def test_staff_without_change_redemption_cannot_refund(self):
        record = self.purchase()
        staff = get_user_model().objects.create_user(username='unprivileged-refund-staff', is_staff=True)
        with self.assertRaises(PermissionDenied):
            refund_redemption(staff, record.pk, **self.refund_args(record))

    def test_restore_requires_additional_inventory_permission(self):
        record = self.purchase()
        staff = get_user_model().objects.create_user(username='refund-only-staff', is_staff=True)
        staff.user_permissions.add(Permission.objects.get(codename='change_redemption', content_type__app_label='customerpanel'))
        args = self.refund_args(record, stock_action='cancelled', stock_verified=True)
        args['confirmation_token'] = refund_confirmation(staff, record, record.fulfillment)
        with self.assertRaises(PermissionDenied):
            refund_redemption(staff, record.pk, **args)
        args['stock_action'] = 'none'
        refund_redemption(staff, record.pk, **args)
        self.assertEqual(self.balance(), 260)

    def test_missing_charge_event_missing_balance_and_overflow_require_review(self):
        record = self.purchase()
        with patch('mouse_force_first_step.customerpanel.reward_refunds.RedemptionEvent.objects.filter') as events:
            events.return_value.exists.return_value = False
            with self.assertRaises(ValidationError):
                self.refund(record)
        CustomerPoints.objects.filter(user=self.user).update(total_points=2 ** 63 - 1)
        with self.assertRaises(ValidationError):
            self.refund(record)
        CustomerPoints.objects.filter(user=self.user).delete()
        with self.assertRaises(ValidationError):
            self.refund(record)
        record.refresh_from_db(); self.assertIsNone(record.refunded_at)

    def test_inactive_customer_can_receive_staff_refund(self):
        record = self.purchase()
        get_user_model().objects.filter(pk=self.user.pk).update(is_active=False)
        self.refund(record)
        self.assertEqual(self.balance(), 260)

    def test_cancellation_restores_one_finite_physical_unit_and_timestamp(self):
        record = self.purchase('physical')
        self.refund(record, reason='cancellation', stock_action='cancelled', stock_verified=True)
        record.refresh_from_db()
        self.assertEqual(record.stock_reserved_quantity, 1)
        self.assertEqual(record.stock_restored_at, record.refunded_at)
        self.assertEqual(Reward.objects.get(pk=self.reward.pk).stock_remaining, 3)

    def test_refund_without_restock_leaves_stock_and_timestamp_unchanged(self):
        record = self.purchase('physical')
        self.refund(record, reason='cannot_fulfill')
        record.refresh_from_db()
        self.assertIsNone(record.stock_restored_at)
        self.assertEqual(Reward.objects.get(pk=self.reward.pk).stock_remaining, 2)
        retry = self.refund(record, stock_action='cancelled', stock_verified=True)
        self.assertTrue(retry.replayed)
        self.assertFalse(retry.stock_restored)

    def test_unlimited_reservation_zero_cannot_manufacture_stock(self):
        record = self.purchase('physical', stock=None)
        self.assertEqual(record.stock_reserved_quantity, 0)
        with self.assertRaises(ValidationError):
            self.refund(record, stock_action='cancelled', stock_verified=True)
        self.assertEqual(self.balance(), 160)
        self.refund(record)
        self.assertIsNone(Reward.objects.get(pk=self.reward.pk).stock_remaining)
        self.assertIsNone(Redemption.objects.get(pk=record.pk).stock_restored_at)

    def test_changed_revision_type_or_unlimited_stock_blocks_restore_not_refund_only(self):
        record = self.purchase('physical')
        for changes in ({'revision': 2}, {'stock_remaining': None}, {'fulfillment_type': 'manual'}):
            with transaction.atomic():
                Reward.objects.filter(pk=self.reward.pk).update(**changes)
                with self.assertRaises(ValidationError):
                    self.refund(record, stock_action='cancelled', stock_verified=True)
                self.assertEqual(self.balance(), 160)
                transaction.set_rollback(True)
        self.refund(record)

    def test_dispatched_item_never_restored_without_verified_return(self):
        record = self.purchase('physical')
        process_fulfillment(self.staff, record.fulfillment.pk, 'processing')
        process_fulfillment(self.staff, record.fulfillment.pk, 'dispatched')
        for changes in ({'reason': 'cancellation'}, {'stock_action': 'cancelled', 'stock_verified': True},
                        {'reason': 'returned', 'stock_action': 'returned', 'stock_verified': False}):
            with self.assertRaises(ValidationError):
                self.refund(record, **changes)
        self.refund(record, reason='unusable')  # Lost/unusable: refund only, never restock.
        self.assertEqual(Reward.objects.get(pk=self.reward.pk).stock_remaining, 2)
        self.assertIsNone(Redemption.objects.get(pk=record.pk).stock_restored_at)

    def test_verified_sellable_physical_return_can_restore_once(self):
        record = self.purchase('physical')
        process_fulfillment(self.staff, record.fulfillment.pk, 'processing')
        process_fulfillment(self.staff, record.fulfillment.pk, 'dispatched')
        process_fulfillment(self.staff, record.fulfillment.pk, 'completed')
        self.refund(record, reason='returned', stock_action='returned', stock_verified=True)
        self.refund(record, reason='returned', stock_action='returned', stock_verified=True)
        self.assertEqual(Reward.objects.get(pk=self.reward.pk).stock_remaining, 3)
        self.assertEqual(record.events.filter(event_type='refunded').count(), 1)

    def test_stale_staff_form_cannot_cancel_newly_dispatched_fulfillment(self):
        record = self.purchase('physical')
        args = self.refund_args(record, reason='cancellation', stock_action='cancelled', stock_verified=True)
        with patch('django.utils.timezone.now', return_value=NOW + timedelta(seconds=1)):
            process_fulfillment(self.staff, record.fulfillment.pk, 'processing')
        with self.assertRaises(ValidationError):
            refund_redemption(self.staff, record.pk, **args)
        self.assertEqual(self.balance(), 160)

    def test_manual_finite_capacity_restores_only_verified_release(self):
        record = self.purchase()
        with self.assertRaises(ValidationError):
            self.refund(record, reason='cannot_fulfill', stock_action='cancelled')
        self.refund(record, reason='cannot_fulfill', stock_action='cancelled', stock_verified=True)
        self.assertEqual(Reward.objects.get(pk=self.reward.pk).stock_remaining, 3)

    def test_manual_unlimited_and_consumed_capacity_not_restored(self):
        record = self.purchase(stock=None)
        process_fulfillment(self.staff, record.fulfillment.pk, 'processing')
        process_fulfillment(self.staff, record.fulfillment.pk, 'completed')
        with self.assertRaises(ValidationError):
            self.refund(record, stock_action='returned', stock_verified=True, reason='returned')
        self.refund(record)
        self.assertIsNone(Reward.objects.get(pk=self.reward.pk).stock_remaining)

    def test_digital_code_and_external_link_remain_assigned_and_encrypted(self):
        for kind in ('voucher', 'external'):
            with transaction.atomic():
                record = self.purchase(kind)
                code = record.private_code
                before = RewardCode.objects.values().get(pk=code.pk)
                self.refund(record)
                self.assertEqual(RewardCode.objects.values().get(pk=code.pk), before)
                self.assertEqual(code.redemption_id, record.pk)
                self.assertFalse(RewardCode.objects.filter(pk=code.pk, redemption__isnull=True).exists())
                self.assertIsNone(Redemption.objects.get(pk=record.pk).stock_restored_at)
                transaction.set_rollback(True)

    def test_digital_inventory_cannot_be_restocked(self):
        record = self.purchase('voucher')
        with self.assertRaises(ValidationError):
            self.refund(record, stock_action='cancelled', stock_verified=True)
        self.assertEqual(self.balance(), 160)

    def test_audit_failure_rolls_back_points_stock_status_fulfillment(self):
        record = self.purchase('physical')
        before = Redemption.objects.values().get(pk=record.pk)
        fulfillment = RewardFulfillment.objects.values().get(redemption=record)
        with patch('mouse_force_first_step.customerpanel.reward_refunds.RedemptionEvent.objects.create', side_effect=RuntimeError('Isolated audit failure')):
            with self.assertRaises(RuntimeError):
                self.refund(record, stock_action='cancelled', stock_verified=True)
        self.assertEqual(self.balance(), 160)
        self.assertEqual(Reward.objects.get(pk=self.reward.pk).stock_remaining, 2)
        self.assertEqual(Redemption.objects.values().get(pk=record.pk), before)
        self.assertEqual(RewardFulfillment.objects.values().get(redemption=record), fulfillment)
        self.assertFalse(record.events.filter(event_type='refunded').exists())

    def test_request_link_and_customer_limit_survive_refund(self):
        record_request = self.approve(self.submit())
        result = self.redeem(self.request_quote(record_request))
        record = Redemption.objects.get(pk=result.redemption_id)
        before = RewardRequest.objects.values().get(pk=record_request.pk)
        self.refund(record)
        self.assertEqual(RewardRequest.objects.values().get(pk=record_request.pk), before)
        Reward.objects.filter(pk=self.reward.pk).update(max_redemptions_per_customer=1)
        self.assert_error('limit_reached', lambda: self.quote())

    def test_refunded_fulfillment_cannot_resume(self):
        record = self.purchase()
        self.refund(record)
        with self.assertRaises(ValidationError):
            process_fulfillment(self.staff, record.fulfillment.pk, 'processing')


@override_settings(**digital.TEST_KEYS)
class RefundHistoryTests(RefundFixtures, TestCase):
    def setUp(self):
        super().setUp()
        seed_paid_access(self.user, self.other)
        self.client.force_login(self.user)

    def test_history_is_customer_only_get_only_and_empty(self):
        url = reverse('customer_redemption_history')
        self.assertContains(self.client.get(url), 'Your redeemed rewards will appear here.')
        self.assertEqual(self.client.post(url).status_code, 405)
        self.client.logout(); self.assertEqual(self.client.get(url).status_code, 302)
        get_user_model().objects.filter(pk=self.user.pk).update(role='simple')
        self.client.force_login(self.user)
        self.assertEqual(self.client.get(url).status_code, 403)

    def test_history_excludes_other_customers_even_when_uuid_is_known(self):
        record = self.purchase()
        self.client.force_login(self.other)
        self.assertNotContains(self.client.get(reverse('customer_redemption_history')), str(record.pk))
        self.assertEqual(self.client.get(reverse('customer_redemption_result', args=[record.pk])).status_code, 404)

    def test_history_contains_only_public_snapshot_status_fields(self):
        record = self.purchase('physical')
        RewardFulfillment.objects.filter(redemption=record).update(internal_notes='PRIVATE STAFF NOTE', request_details='PRIVATE DELIVERY NOTE')
        with CaptureQueriesContext(connection) as queries:
            response = self.client.get(reverse('customer_redemption_history'))
        for secret in ('PRIVATE STAFF NOTE', 'PRIVATE DELIVERY NOTE', DELIVERY['address_line_1'], DELIVERY['contact_email']):
            self.assertNotContains(response, secret)
        for sql in (item['sql'] for item in queries):
            for field in ('internal_notes', 'encrypted_payload', 'payload_fingerprint', 'address_line_1', 'contact_email'):
                self.assertNotIn(field, sql)
        self.assertContains(response, str(record.pk))
        self.assertIn('no-store', response['Cache-Control'])

    def test_digital_history_never_contains_private_values_or_inventory_queries(self):
        for kind in ('voucher', 'external'):
            with transaction.atomic():
                record = self.purchase(kind)
                with CaptureQueriesContext(connection) as queries:
                    response = self.client.get(reverse('customer_redemption_history'))
                self.assertNotContains(response, 'PRIVATE-')
                self.assertNotContains(response, 'https://partner.example/claim/')
                self.assertTrue(all('customerpanel_rewardcode' not in q['sql'] for q in queries))
                transaction.set_rollback(True)

    def test_history_newest_first_refund_and_original_result_balance(self):
        first = self.purchase()
        second = self.purchase()
        self.refund(first)
        response = self.client.get(reverse('customer_redemption_history'))
        self.assertEqual([r['id'] for r in response.context['history_page']], [second.pk, first.pk])
        self.assertContains(response, 'Refunded · +100 Points')
        page = self.client.get(reverse('customer_redemption_result', args=[first.pk]))
        self.assertContains(page, 'Refunded — 100 Points were returned')
        self.assertEqual(page.context['redemption']['balance_after'], 160)
        self.assertEqual(page.context['current_balance'], 160)
        self.assertNotContains(page, 'data-reward-reveal')

    def test_navigation_and_safe_page_metadata(self):
        url = reverse('customer_redemption_history')
        self.assertContains(self.client.get(reverse('customer_rewards')), url)
        page = self.client.get(url, HTTP_X_CUSTOMER_NAVIGATION='1')
        self.assertContains(page, 'data-page="redemption_history"')
        self.assertContains(page, 'customer-music-audio')
        self.assertContains(page, 'data-fulfillment-private')

    def test_admin_refund_csrf_and_readonly_protections(self):
        record = self.purchase()
        url = reverse('admin:customerpanel_redemption_refund', args=[record.pk])
        self.assertEqual(self.client.get(url).status_code, 302)
        client = Client(enforce_csrf_checks=True); client.force_login(self.staff)
        form = client.get(url).context['form']
        data = {**self.refund_args(record), 'confirmation_token': form.initial['confirmation_token']}
        self.assertEqual(client.post(url, data).status_code, 403)
        data['csrfmiddlewaretoken'] = client.cookies['csrftoken'].value
        self.assertEqual(client.post(url, data).status_code, 302)
        self.assertEqual(client.post(url, data).status_code, 302)
        self.assertContains(client.get(url), 'Refunded: 100 Points')
        self.assertEqual(self.balance(), 260)
        self.client.force_login(self.staff)
        self.assertEqual(self.client.post(reverse('admin:customerpanel_redemption_change', args=[record.pk]), {'refunded_points': '99999'}).status_code, 403)

    def test_refund_amount_owner_and_snapshot_post_fields_cannot_override_service(self):
        record = self.purchase()
        self.client.force_login(self.staff)
        url = reverse('admin:customerpanel_redemption_refund', args=[record.pk])
        response = self.client.post(url, {**self.refund_args(record), 'refunded_points': '9999', 'user': self.other.pk, 'points_spent': '1'})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.balance(), 260)
        self.assertEqual(self.balance(self.other), 260)
        self.assertEqual(Redemption.objects.get(pk=record.pk).points_spent, 100)


@override_settings(**digital.TEST_KEYS)
@skipUnlessDBFeature('has_select_for_update')
class RefundConcurrencyTests(RefundFixtures, TransactionTestCase):
    available_apps = digital.DigitalCommitAndConcurrencyTests.available_apps
    worker = digital.DigitalCommitAndConcurrencyTests.worker
    simultaneous = digital.DigitalCommitAndConcurrencyTests.simultaneous

    def setUp(self):
        self.setUpTestData()
        super().setUp()

    def test_two_different_staff_refunds_credit_and_restock_once(self):
        record = self.purchase('physical')
        other_staff = get_user_model().objects.create_user(username='other-refund-staff', is_staff=True, is_superuser=True)
        args = self.refund_args(record, stock_action='cancelled', stock_verified=True)
        other_args = {**args, 'confirmation_token': refund_confirmation(other_staff, record, record.fulfillment)}
        results = self.simultaneous([lambda: refund_redemption(self.staff, record.pk, **args),
                                    lambda: refund_redemption(other_staff, record.pk, **other_args)])
        self.assertCountEqual([r.replayed for r in results], [False, True])
        self.assertEqual(self.balance(), 260)
        self.assertEqual(Reward.objects.get(pk=self.reward.pk).stock_remaining, 3)
        self.assertEqual(record.events.filter(event_type='refunded').count(), 1)

    def test_refund_lock_order_matches_redemption_and_claims(self):
        record = self.purchase()
        args = self.refund_args(record)
        with CaptureQueriesContext(connection) as queries:
            refund_redemption(self.staff, record.pk, **args)
        locks = [q['sql'] for q in queries if 'FOR UPDATE' in q['sql']]
        self.assertEqual(len(locks), 5)
        for sql, model in zip(locks, (get_user_model(), CustomerPoints, Reward, Redemption, RewardFulfillment)):
            self.assertIn('FROM "' + model._meta.db_table + '"', sql)

    def test_daily_and_bonus_racing_refund_preserve_both_balance_changes(self):
        for operation, amount in ((claim_daily_points, 10), (claim_streak_bonus, 35)):
            CustomerPoints.objects.filter(user=self.user).update(total_points=260, streak_days=7,
                last_daily_claim_date=NOW.date() - timedelta(days=1), day_7_bonus_awarded=operation is claim_daily_points)
            record = self.purchase()
            args = self.refund_args(record)
            self.simultaneous([lambda: refund_redemption(self.staff, record.pk, **args), lambda: operation(self.user)])
            self.assertEqual(self.balance(), 260 + amount)

    def test_refund_then_waiting_purchase_requires_new_balance_confirmation(self):
        record = self.purchase()
        quote = self.quote()
        attempted = Event()
        with ThreadPoolExecutor(max_workers=1) as pool:
            with transaction.atomic():
                self.refund(record)
                future = pool.submit(self.worker, lambda: self.redeem(quote), attempted, get_user_model()._meta.db_table)
                self.assertTrue(attempted.wait(10))
            self.assertEqual(future.result(timeout=20), 'balance_changed')
        self.assertEqual(self.balance(), 260)

    def test_refunded_digital_benefits_cannot_be_revealed_and_stay_assigned(self):
        for kind in ('voucher', 'external'):
            record = self.purchase(kind)
            self.assertEqual(get_private_reward_benefit(self.user, record.pk).redemption_id, record.pk)
            self.refund(record)
            with self.assertRaises(PrivateBenefitUnavailable):
                get_private_reward_benefit(self.user, record.pk)
            with self.assertRaises(PermissionDenied):
                get_private_reward_benefit(self.other, record.pk)
            self.assertEqual(record.private_code.redemption_id, record.pk)

    def test_second_connection_never_observes_failed_refund(self):
        record = self.purchase()
        args = self.refund_args(record)
        with patch('mouse_force_first_step.customerpanel.reward_refunds.RedemptionEvent.objects.create', side_effect=RuntimeError('Isolated failure')):
            with self.assertRaises(RuntimeError):
                refund_redemption(self.staff, record.pk, **args)
        with ThreadPoolExecutor(max_workers=1) as pool:
            result = pool.submit(self.worker, lambda: (self.balance(), Redemption.objects.get(pk=record.pk).refunded_at), Event(), 'unused').result(timeout=20)
        self.assertEqual(result, (160, None))
