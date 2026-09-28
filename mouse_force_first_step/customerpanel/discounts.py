"""Read-only catalogue queries and authoritative deal access/voting services."""
import json
from uuid import uuid4
from urllib.parse import urlencode, urlsplit

from django.core import signing
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Q, Exists, OuterRef, Subquery, Sum, Count, IntegerField
from django.db.models.functions import Coalesce
from django.http import Http404
from django.urls import reverse
from django.utils import timezone
from django.utils.crypto import salted_hmac

from .models import Discount, DiscountVote, CustomerDiscountAccess, CustomerPoints
from .points import _lock_customer, get_points_state
from .section_access import has_section_access, session_fingerprint

SALT = 'customerpanel.discount-confirmation.v1'
MAX_AGE = 15 * 60
TYPE_LABELS = [('all', 'All types'), ('percent', '% Off'), ('money', '£ Off'),
               ('fixed', 'Fixed Price'), ('free', 'Free'), ('bogo', '2-for-1'), ('other', 'Other')]


class DealError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def require_access(user):
    if not has_section_access(user, 'discounts'):
        raise PermissionDenied('Discounts section access is required.')


def current_window(now=None):
    now = now or timezone.now()
    return Q(active=True) & (Q(valid_from__isnull=True) | Q(valid_from__lte=now)) & (
        Q(valid_until__gt=now) | Q(ongoing=True, valid_until__isnull=True))


def is_current(deal):
    now = timezone.now()
    return (deal.active and (not deal.valid_from or deal.valid_from <= now)
            and (deal.valid_until > now if deal.valid_until else deal.ongoing))


def annotated_deals(user):
    scores = DiscountVote.objects.filter(discount_id=OuterRef('pk')).values('discount_id').annotate(
        total=Sum('value'), likes=Count('pk', filter=Q(value=1)), dislikes=Count('pk', filter=Q(value=-1)))
    return Discount.objects.annotate(
        purchased=Exists(CustomerDiscountAccess.objects.filter(user=user, discount_id=OuterRef('pk'))),
        community_score=Coalesce(Subquery(scores.values('total')[:1], output_field=IntegerField()), 0),
        like_count=Coalesce(Subquery(scores.values('likes')[:1], output_field=IntegerField()), 0),
        dislike_count=Coalesce(Subquery(scores.values('dislikes')[:1], output_field=IntegerField()), 0),
        customer_vote=Coalesce(Subquery(DiscountVote.objects.filter(user=user, discount_id=OuterRef('pk')).values('value')[:1], output_field=IntegerField()), 0),
    )


def safe_url(value):
    try:
        url = urlsplit(value)
        return value if url.scheme == 'https' and url.hostname and not url.username and not url.password else ''
    except (ValueError, TypeError):
        return ''


def present_deal(row, *, detail=False):
    """Explicit public projection; never serialize a Discount with model_to_dict."""
    current = is_current(row)
    owned = bool(row.purchased)
    result = {name: getattr(row, name) for name in (
        'id', 'brand', 'title', 'short_description', 'value_label', 'eligibility', 'country', 'region',
        'valid_from', 'valid_until', 'ongoing', 'featured', 'points_to_unlock_deal', 'last_verified_at', 'image_alt')}
    result.update(category=row.get_category_display(), category_key=row.category,
        deal_type=row.get_deal_type_display(), image_url=safe_url(row.image_url),
        current=current, purchased=owned, access_allowed=owned or (current and row.points_to_unlock_deal == 0),
        score=row.community_score, score_label=f'{row.community_score:+d}' if row.community_score else '0',
        likes=row.like_count, dislikes=row.dislike_count,
        vote=row.customer_vote, url=reverse('customer_discount_detail', args=[row.pk]),
        vote_url=reverse('customer_discount_vote', args=[row.pk]),
        channels=[dict(Discount.CHANNELS)[value] for value in row.usage_channels if value in dict(Discount.CHANNELS)])
    if detail:
        result.update(details=row.details, terms_summary=row.terms_summary, offer_version=offer_fingerprint(row))
        if result['access_allowed']:
            result.update(promo_code=row.promo_code, official_url=safe_url(row.official_url))
    return result


def get_customer_deal(user, deal_id):
    require_access(user)
    try:
        return annotated_deals(user).filter(current_window() | Q(purchased=True)).get(pk=deal_id)
    except Discount.DoesNotExist:
        raise Http404('This deal is not available.') from None


def catalogue(user, parameters):
    require_access(user)
    query = ' '.join(parameters.get('q', '').split())[:100]
    category = parameters.get('category', 'all')
    category = category if category in Discount.Category.values else 'all'
    kind = parameters.get('type', 'all')
    kind = kind if kind in Discount.DealType.values else 'all'
    saved = parameters.get('access') == 'unlocked'
    rows = annotated_deals(user).defer('promo_code', 'official_url', 'details', 'terms_summary')
    rows = rows.filter(purchased=True) if saved else rows.filter(current_window())
    for word in query.split()[:12]:
        named_categories = [key for key, label in Discount.Category.choices if word.casefold() in label.casefold()]
        rows = rows.filter(Q(brand__icontains=word) | Q(title__icontains=word) | Q(category__icontains=word)
            | Q(country__icontains=word) | Q(search_keywords__icontains=word) | Q(category__in=named_categories))
    if kind != 'all':
        rows = rows.filter(deal_type=kind)
    counts = dict(rows.order_by().values('category').annotate(total=Count('id')).values_list('category', 'total'))
    base = {'q': query, 'type': kind, 'access': 'unlocked' if saved else ''}

    def url(**changes):
        params = {**base, 'category': category, **changes}
        return reverse('customer_discounts') + '?' + urlencode({k: v for k, v in params.items() if v not in ('', 'all', None)})

    categories = [{'key': 'all', 'label': 'All', 'count': sum(counts.values()), 'url': url(category='all')}]
    categories += [{'key': key, 'label': label, 'count': counts.get(key, 0), 'url': url(category=key)} for key, label in Discount.Category.choices]
    if category != 'all':
        rows = rows.filter(category=category)
    page = Paginator(rows, 12).get_page(parameters.get('page', '1'))
    return {'deals': [present_deal(row) for row in page], 'discount_categories': categories,
        'discount_types': TYPE_LABELS, 'discount_query': query, 'discount_category': category,
        'discount_type': kind, 'discount_saved': saved, 'discount_count': page.paginator.count,
        'discount_page': page.number, 'discount_pages': page.paginator.num_pages,
        'discount_previous': url(page=page.previous_page_number()) if page.has_previous() else '',
        'discount_next': url(page=page.next_page_number()) if page.has_next() else '',
        'discount_url': url(page=page.number if page.number > 1 else None)}


def offer_fingerprint(deal):
    fields = ('brand', 'title', 'short_description', 'details', 'category', 'deal_type', 'value_label',
        'percentage_value', 'money_off_value', 'currency', 'eligibility', 'country', 'region',
        'usage_channels', 'promo_code', 'official_url', 'valid_from', 'valid_until', 'ongoing',
        'terms_summary', 'points_to_unlock_deal', 'active')
    return salted_hmac(SALT, json.dumps({f: getattr(deal, f) for f in fields}, sort_keys=True, default=str)).hexdigest()


def create_deal_confirmation(user, deal_id, *, session_key, expected_offer=None):
    deal = get_customer_deal(user, deal_id)
    state = get_points_state(user)
    allowed = deal.purchased or deal.points_to_unlock_deal == 0
    if not allowed and expected_offer is not None and expected_offer != offer_fingerprint(deal):
        raise DealError('offer_changed')
    balance = state['total_points']
    claims = {'v': 1, 'u': str(user.pk), 's': session_fingerprint(user, session_key),
        'deal': str(deal.pk), 'cost': deal.points_to_unlock_deal, 'balance': balance,
        'offer': offer_fingerprint(deal), 'intent': str(uuid4())}
    return {'title': deal.title, 'brand': deal.brand, 'cost': deal.points_to_unlock_deal,
        'balance': balance, 'balance_after': balance if allowed else balance - deal.points_to_unlock_deal if balance >= deal.points_to_unlock_deal else None,
        'allowed': bool(allowed), 'can_unlock': is_current(deal) and balance >= deal.points_to_unlock_deal,
        'token': signing.dumps(claims, salt=SALT), 'state': state}


@transaction.atomic
def unlock_deal(user, deal_id, token, *, session_key):
    current = _lock_customer(user)
    points = CustomerPoints.objects.select_for_update().filter(user=current).first()
    require_access(current)
    try:
        deal = Discount.objects.select_for_update().get(pk=deal_id)
    except Discount.DoesNotExist:
        raise DealError('unavailable') from None
    receipt = CustomerDiscountAccess.objects.select_for_update().filter(user=current, discount=deal).first()
    if receipt:
        return {'access_id': str(receipt.pk), 'points_spent': 0, 'already_unlocked': True, 'state': get_points_state(current)}
    if not is_current(deal) or not (deal.promo_code.strip() or safe_url(deal.official_url)):
        raise DealError('unavailable')
    if deal.points_to_unlock_deal == 0:
        return {'access_id': None, 'points_spent': 0, 'already_unlocked': True, 'state': get_points_state(current)}
    if not isinstance(token, str) or len(token) > 4096:
        raise DealError('invalid_confirmation')
    try:
        claims = signing.loads(token, salt=SALT, max_age=MAX_AGE)
    except signing.SignatureExpired:
        raise DealError('expired_confirmation') from None
    except (signing.BadSignature, ValueError, TypeError):
        raise DealError('invalid_confirmation') from None
    if (not isinstance(claims, dict) or claims.get('v') != 1 or claims.get('u') != str(current.pk)
            or claims.get('s') != session_fingerprint(current, session_key) or claims.get('deal') != str(deal.pk)):
        raise DealError('invalid_confirmation')
    if claims.get('offer') != offer_fingerprint(deal) or claims.get('cost') != deal.points_to_unlock_deal:
        raise DealError('offer_changed')
    balance = points.total_points if points else 0
    if balance < deal.points_to_unlock_deal:
        raise DealError('insufficient_points')
    if claims.get('balance') != balance:
        raise DealError('balance_changed')
    points.total_points = balance - deal.points_to_unlock_deal
    points.save(update_fields=['total_points'])
    receipt = CustomerDiscountAccess.objects.create(user=current, discount=deal,
        points_spent=deal.points_to_unlock_deal, balance_after=points.total_points)
    # The next authenticated GET reveals the code/link, after this transaction commits.
    return {'access_id': str(receipt.pk), 'points_spent': deal.points_to_unlock_deal,
            'already_unlocked': False, 'state': get_points_state(current)}


@transaction.atomic
def set_vote(user, deal_id, value):
    """A desired state, not a toggle command: retries cannot undo a vote."""
    if type(value) is not int or value not in (-1, 0, 1):
        raise DealError('invalid_vote')
    current = _lock_customer(user)
    require_access(current)
    try:
        Discount.objects.select_for_update().filter(current_window()).get(pk=deal_id)
    except Discount.DoesNotExist:
        raise DealError('unavailable') from None
    if value:
        DiscountVote.objects.update_or_create(user=current, discount_id=deal_id, defaults={'value': value})
    else:
        DiscountVote.objects.filter(user=current, discount_id=deal_id).delete()
    counts = DiscountVote.objects.filter(discount_id=deal_id).aggregate(
        likes=Count('pk', filter=Q(value=1)), dislikes=Count('pk', filter=Q(value=-1)))
    score = counts['likes'] - counts['dislikes']
    return {'vote': value, **counts, 'score': score, 'score_label': f'{score:+d}' if score else '0'}
