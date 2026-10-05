"""Small, read-only, allowlisted context for the existing Message Us assistant."""
import json
from datetime import timezone as utc_timezone

from django.urls import reverse
from django.utils import timezone

from .models import CustomerSectionUnlock, Discount
from .points import DAILY_POINTS, DAY_7_BONUS, DAY_14_BONUS
from .section_access import UNLOCK_COST, SECTIONS
from .reward_catalogue import published_rewards
from .discounts import current_window


def mouseforce_context(user):
    """Never serialize models, inventory, customer balances, notes or entitlements."""
    unlocked = set(CustomerSectionUnlock.objects.filter(user=user).values_list('section', flat=True))
    facts = {
        'today_utc': timezone.now().astimezone(utc_timezone.utc).date().isoformat(),
        'points': {
            'daily_claim': DAILY_POINTS, 'day_7_bonus': DAY_7_BONUS, 'day_14_bonus': DAY_14_BONUS,
            'rules': 'Claim daily, once per UTC calendar day. Bonuses are manually claimable at days 7 and 14. '
                'An unclaimed bonus auto-awards only with the next consecutive daily claim. '
                'A missed day resets the streak and loses unclaimed bonuses, never accumulated points. '
                'After day 14, the next valid daily claim starts a new day-1 cycle. Points cannot be bought or exchanged for cash.',
            'full_cycle_total': DAILY_POINTS * 14 + DAY_7_BONUS + DAY_14_BONUS,
        },
        'section_access': {'cost_once_per_section': UNLOCK_COST, 'sections': list(SECTIONS.values()),
            'rules': 'Permanent access per account. Individual rewards and some deals have a separate Points price.'},
        'free': 'Message Us AI has no daily question quota and costs no Points. Dashboard, Daily/Bonus claims, '
            'How Points Work and the music player are free. News and Weather still require their one-time section unlock.',
        'rewards': 'Customers read reward details and explicitly confirm the Points cost before redeeming. '
            'Types include vouchers, private partner links, physical products and manually arranged rewards. '
            'Reward requests are reviewed by staff; submitting a request does not spend Points or reserve stock. '
            'A later priced offer needs explicit customer acceptance. Availability and eligibility are checked at redemption.',
        'discounts': 'A searchable, filterable deal catalogue with like/dislike voting. Some deals are free; others '
            'have a one-time Points access cost shown before confirmation. Retailer terms, eligibility and expiry apply. '
            'Points unlock deal information, not a purchase from the retailer or a guarantee that an offer remains available.',
        'offers': 'A customer information page inviting specific requests through ChatMe. Do not invent listed offers or partnerships.',
        'discover': 'The homepage Discover menu introduces Top Picks, Nightlife, Dating & Social, Food & Drink, '
            'Travel & Stay and Entertainment. These are discovery themes, not a separate live booking or listings service. '
            'Many menu links are currently placeholders. Help with research or direct a personal request to Contact/ChatMe.',
        'guides_resources': 'How to Ask MouseForce (/startupguid/) and Guides & Resources (/marketingresources/) '
            'are existing public informational pages, covering better requests, saving/earning, discovery, travel, smart tools and business resources. '
            'The homepage also describes automation, custom scripts and business-tool categories. These are service/information '
            'themes, not proof of integrations, partnerships or functioning tools. Do not promise unavailable features.',
        'automation': 'Offer practical advice about repetitive tasks and scripts. Ask about the task, browser/tool and permission. '
            'You cannot operate a browser, run scripts, access accounts or arrange paid work from this chat. '
            'Customers can send specific service requests through Contact or the separate ChatMe messaging page.',
        'other_features': 'Customer News uses GNews, Weather supports city searches, and music uses Jamendo. '
            'Feedback and human ChatMe messaging also exist. Never claim a human has joined this AI conversation.',
        'links': {name: reverse(name) for name in ('customer_how_points_work', 'customer_rewards',
            'customer_discounts', 'user_chat_redirect', 'contact', 'startupguid', 'marketingresources')},
    }
    # Reuse the catalogue eligibility query, but select only this small public projection.
    # No RewardCode value, inventory count, fulfillment data or private link reaches the provider.
    if 'rewards' in unlocked:
        facts['reward_examples'] = list(published_rewards(user).order_by('title').values(
            'title', 'short_description', 'points_required', 'category')[:8])
        facts['reward_examples_note'] = 'A limited current catalogue sample; check the Rewards page for final availability.'
    else:
        facts['reward_examples_note'] = 'Rewards is locked for this account. Explain the concept; direct the customer to unlock Rewards to see its catalogue.'
    if 'discounts' in unlocked:
        facts['discount_examples'] = list(Discount.objects.filter(current_window()).order_by('brand', 'title').values(
            'brand', 'title', 'short_description', 'points_to_unlock_deal')[:8])
        facts['discount_examples_note'] = 'A limited published sample, not fresh verification by the merchant. Private deal URLs/codes are deliberately omitted.'
    return json.dumps(facts, ensure_ascii=False)
