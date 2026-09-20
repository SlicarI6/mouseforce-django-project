"""Post-commit digital reward notices. Never load or reveal private inventory."""
import logging
from urllib.parse import urlsplit

from django.conf import settings
from django.contrib.sites.models import Site
from django.core.mail import get_connection, send_mail
from django.http.request import validate_host
from django.urls import reverse

logger = logging.getLogger(__name__)


def _result_url(redemption_id):
    """Use the configured Django Site, never a browser-supplied host or URL."""
    try:
        domain = Site.objects.get_current().domain
        parsed = urlsplit('https://' + domain)
        if (not parsed.hostname or parsed.username or parsed.password
                or parsed.path or parsed.query or parsed.fragment
                or any(character.isspace() for character in domain)
                or parsed.hostname in ('example.com', 'www.example.com')
                or not validate_host(parsed.hostname, settings.ALLOWED_HOSTS)):
            return None
        # Accessing port also rejects a malformed configured port.
        parsed.port
        return 'https://' + domain + reverse('customer_redemption_result', args=[redemption_id])
    except Exception:
        # A missing/misconfigured Site must not prevent the notification itself.
        return None


def send_reward_ready_email(recipient, reward_title, redemption_id):
    """Best-effort delivery after commit; failures never escape into redemption."""
    if not recipient:
        return
    try:
        lines = [
            'Your MouseForce reward is ready. Sign in to your account to view your voucher or private reward securely.',
            '',
            f'Reward: {reward_title}',
            f'Redemption reference: {redemption_id}',
        ]
        result_url = _result_url(redemption_id)
        if result_url:
            lines.extend(['', 'View your reward securely: ' + result_url])
        connection = get_connection(timeout=settings.EMAIL_TIMEOUT or 10)
        sent = send_mail(
            subject='Your MouseForce reward is ready', message='\n'.join(lines),
            from_email=settings.DEFAULT_FROM_EMAIL, recipient_list=[recipient],
            connection=connection, fail_silently=False,
        )
        if sent != 1:
            logger.warning('Reward notification was not sent for redemption %s.', redemption_id)
    except Exception:
        # Do not log SMTP exceptions, recipient addresses, or message contents.
        logger.warning('Reward notification failed for redemption %s.', redemption_id)
