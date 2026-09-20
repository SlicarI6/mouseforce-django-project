"""Fake digital inventory and in-memory email; no SMTP or real customer writes."""
from smtplib import SMTPException
from unittest.mock import patch

from cryptography.fernet import Fernet
from django.contrib.sites.models import Site
from django.core import mail
from django.db import connection, transaction
from django.test import TransactionTestCase, override_settings, skipUnlessDBFeature
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from .models import Redemption, RewardCode, RewardRequest
from .reward_confirmation import create_reward_confirmation
from .reward_redemption import redeem_reward
from .test_reward_allocation import DigitalFixtures, SESSION, TEST_KEYS
from .test_reward_models import make_reward

EMAIL_MODULE = 'mouse_force_first_step.customerpanel.reward_emails'


@override_settings(**TEST_KEYS, EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend',
                   DEFAULT_FROM_EMAIL='rewards@example.test', SITE_ID=1,
                   ALLOWED_HOSTS=['rewards.example.test'])
@skipUnlessDBFeature('has_select_for_update')
class DigitalRewardEmailTests(DigitalFixtures, TransactionTestCase):
    # Real commits use PostgreSQL: SQLite's append-only audit triggers forbid
    # the DELETE-based flush performed by TransactionTestCase between tests.
    available_apps = ['django.contrib.auth', 'django.contrib.contenttypes', 'django.contrib.sessions',
                      'django.contrib.sites', 'mouse_force_first_step.accounts',
                      'mouse_force_first_step.customerpanel']

    def setUp(self):
        self.setUpTestData()
        super().setUp()
        self.user.email = 'customer@example.test'
        self.user.save(update_fields=['email'])
        Site.objects.update_or_create(pk=1, defaults={'domain': 'rewards.example.test', 'name': 'Test rewards'})
        Site.objects.clear_cache()
        self.addCleanup(Site.objects.clear_cache)

    def assert_digital_notice(self, reward):
        code, private_value = self.inventory(reward)
        quote = self.quote(reward)
        with CaptureQueriesContext(connection) as queries, patch.object(Fernet, 'decrypt', side_effect=AssertionError('No email decryption')):
            with transaction.atomic():
                result = self.redeem(quote)
                self.assertEqual(len(mail.outbox), 0)
            self.assertEqual(len(mail.outbox), 1)
        message = mail.outbox[0]
        self.assertEqual(message.subject, 'Your MouseForce reward is ready')
        self.assertEqual(message.to, [self.user.email])
        self.assertEqual(message.from_email, 'rewards@example.test')
        self.assertIn('Your MouseForce reward is ready. Sign in to your account to view your voucher or private reward securely.', message.body)
        self.assertIn(reward.title, message.body)
        self.assertIn(str(result.redemption_id), message.body)
        self.assertIn('https://rewards.example.test' + reverse('customer_redemption_result', args=[result.redemption_id]), message.body)
        serialized = message.message().as_string()
        for secret in (private_value, bytes(code.encrypted_payload).decode(), code.payload_fingerprint):
            self.assertNotIn(secret, serialized)
        self.assertTrue(all('encrypted_payload' not in row['sql'] and 'payload_fingerprint' not in row['sql'] for row in queries))

    def test_voucher_sends_one_private_free_notification_after_commit(self):
        self.assert_digital_notice(self.reward)

    def test_external_sends_one_private_free_notification_after_commit(self):
        reward = make_reward(is_active=True, fulfillment_type='external', information_url='https://partner.example/public')
        self.assert_digital_notice(reward)
        self.assertNotIn(reward.information_url, mail.outbox[0].body)

    def test_idempotent_retry_does_not_send_another_email(self):
        self.inventory()
        quote = self.quote()
        first = self.redeem(quote)
        replay = self.redeem(quote)
        self.assertTrue(replay.replayed)
        self.assertEqual(first.redemption_id, replay.redemption_id)
        self.assertEqual(len(mail.outbox), 1)

    def test_request_acceptance_with_another_confirmation_does_not_resend(self):
        self.inventory()
        request = RewardRequest.objects.create(user=self.user, title='Test request', description='Test only',
            status='approved', approved_reward=self.reward)
        quotes = [create_reward_confirmation(self.user, self.reward.pk, session_key=SESSION, request_id=request.pk) for _ in range(2)]
        first = self.redeem(quotes[0])
        replay = self.redeem(quotes[1])
        self.assertTrue(replay.replayed)
        self.assertEqual(first.redemption_id, replay.redemption_id)
        self.assertEqual(len(mail.outbox), 1)

    def test_mail_failure_keeps_committed_redemption_and_retry_does_not_resend(self):
        code, private_value = self.inventory()
        quote = self.quote()
        with patch(EMAIL_MODULE + '.send_mail', side_effect=SMTPException(private_value)) as sending:
            with self.assertLogs(EMAIL_MODULE, level='WARNING') as logs:
                result = self.redeem(quote)
            self.assertTrue(Redemption.objects.filter(pk=result.redemption_id).exists())
            code.refresh_from_db()
            self.assertEqual(code.redemption_id, result.redemption_id)
            self.assertEqual(self.balance(), 160)
            self.assertEqual(self.redeem(quote).redemption_id, result.redemption_id)
            sending.assert_called_once()
        self.assertNotIn(private_value, '\n'.join(logs.output))
        self.assertNotIn(self.user.email, '\n'.join(logs.output))

    def test_outer_rollback_discards_email_and_all_redemption_writes(self):
        self.inventory()
        quote = self.quote()
        with self.assertRaises(RuntimeError):
            with transaction.atomic():
                self.redeem(quote)
                self.assertEqual(len(mail.outbox), 0)
                raise RuntimeError('Test outer rollback')
        self.assertEqual(len(mail.outbox), 0)
        self.assert_unspent()
        self.redeem(quote)
        self.assertEqual(len(mail.outbox), 1)

    def test_failed_allocation_does_not_send_email(self):
        self.inventory()
        with patch.object(RewardCode, 'save', side_effect=RuntimeError('Test allocation failure')):
            with self.assertRaises(RuntimeError):
                self.redeem(self.quote())
        self.assertEqual(len(mail.outbox), 0)
        self.assert_unspent()

    def test_physical_and_manual_redemptions_do_not_send_digital_notices(self):
        from .test_reward_fulfillment import DELIVERY
        for kind in ('physical', 'manual'):
            reward = make_reward(is_active=True, fulfillment_type=kind)
            quote = self.quote(reward)
            redeem_reward(self.user, reward.pk, quote.token, session_key=SESSION,
                fulfillment_data=DELIVERY if kind == 'physical' else {})
        self.assertEqual(len(mail.outbox), 0)

    def test_untrusted_site_domain_is_omitted_without_blocking_notification(self):
        Site.objects.filter(pk=1).update(domain='untrusted.example')
        Site.objects.clear_cache()
        self.inventory()
        self.redeem(self.quote())
        self.assertEqual(len(mail.outbox), 1)
        self.assertNotIn('https://', mail.outbox[0].body)
        self.assertNotIn('untrusted.example', mail.outbox[0].body)
