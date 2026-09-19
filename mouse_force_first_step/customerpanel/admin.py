from django.contrib import admin
from django.contrib.admin.models import ADDITION, LogEntry
from django.contrib.admin.utils import unquote
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Case, CharField, Count, Q, Value, When
from django.http import Http404, HttpResponseRedirect
from django.template.response import TemplateResponse
from django.urls import path, reverse
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.views.decorators.debug import sensitive_post_parameters, sensitive_variables

from .models import Feedback, Reward, RewardCode, Redemption, RewardFulfillment, RewardRequest, RedemptionEvent
from .reward_admin_forms import RewardAdminForm, RewardCodeAdminForm, PrivateInventoryImportForm
from .reward_inventory import import_private_inventory, lock_staff_users
from .reward_fulfillment import process_fulfillment
from .reward_requests import RewardRequestReviewForm, review_token, review_reward_request
from .reward_refunds import RefundForm, refund_confirmation, refund_redemption
from django.utils.html import format_html

admin.site.register(Feedback)


class StockFilter(admin.SimpleListFilter):
    title = 'stock'
    parameter_name = 'stock'

    def lookups(self, request, model_admin):
        return [('available', 'In stock'), ('empty', 'Out of stock'), ('unlimited', 'Unlimited')]

    def queryset(self, request, queryset):
        physical = Q(fulfillment_type__in=['physical', 'manual'])
        codes = Q(fulfillment_type__in=['voucher', 'external'])
        if self.value() == 'available':
            return queryset.filter((physical & (Q(stock_remaining__gt=0) | Q(stock_remaining__isnull=True))) | (codes & Q(code_available__gt=0)))
        if self.value() == 'empty':
            return queryset.filter((physical & Q(stock_remaining=0)) | (codes & Q(code_available=0)))
        if self.value() == 'unlimited':
            return queryset.filter(physical, stock_remaining__isnull=True)
        return queryset


class InventoryStatusFilter(admin.SimpleListFilter):
    title = 'inventory status'
    parameter_name = 'inventory_status'

    def lookups(self, request, model_admin):
        return [(value, value.title()) for value in ('available', 'assigned', 'expired', 'disabled')]

    def queryset(self, request, queryset):
        return queryset.filter(inventory_state=self.value()) if self.value() in dict(self.lookups(request, None)) else queryset


class NoDeleteAdmin(admin.ModelAdmin):
    actions = None

    def has_delete_permission(self, request, obj=None):
        return False

    def lookup_allowed(self, lookup, value, request):
        if any(name in lookup.split('__') for name in ('encrypted_payload', 'payload_fingerprint', 'encryption_key_id')):
            return False
        return super().lookup_allowed(lookup, value, request)


@admin.register(Reward)
class RewardAdmin(NoDeleteAdmin):
    form = RewardAdminForm
    list_display = ('title', 'category', 'fulfillment_type', 'partner_name', 'points_required', 'is_active', 'stock_display', 'code_counts', 'valid_until')
    list_filter = ('category', 'fulfillment_type', 'partner_name', 'is_active', 'is_exclusive', 'is_limited_time', 'access_scope', StockFilter)
    search_fields = ('title', 'short_description', 'partner_name', 'country_code', 'region', 'city')
    search_help_text = 'Search titles, summaries, partners or locations.'
    filter_horizontal = ('eligible_users',)
    readonly_fields = ('revision', 'created_at', 'updated_at', 'code_counts')
    fieldsets = (
        ('Reward', {'fields': ('title', 'short_description', 'full_description', 'category', 'image_url', 'image_alt', 'points_required', 'fulfillment_type')}),
        ('Partner and location', {'fields': ('partner_name', 'information_url', 'country_code', 'region', 'city', 'location_details')}),
        ('Validity and conditions', {'fields': ('valid_from', 'valid_until', 'benefit_valid_until', 'terms', 'fulfillment_instructions')}),
        ('Fulfillment information required', {'fields': ('requires_shipping_address', 'requires_contact_details', 'requires_region', 'requires_phone')}),
        ('Inventory and access', {'fields': ('stock_remaining', 'code_counts', 'max_redemptions_per_customer', 'is_active', 'is_limited_time', 'is_exclusive', 'access_scope', 'eligible_users')}),
        ('Record', {'fields': ('revision', 'created_at', 'updated_at', 'edit_token')}),
    )

    def get_queryset(self, request):
        return super().get_queryset(request).annotate(
            code_available=Count('private_codes', filter=Q(private_codes__is_active=True, private_codes__redemption__isnull=True) & (Q(private_codes__expires_at__isnull=True) | Q(private_codes__expires_at__gt=timezone.now())), distinct=True),
            code_assigned=Count('private_codes', filter=Q(private_codes__redemption__isnull=False), distinct=True),
            code_total=Count('private_codes', distinct=True),
        )

    @admin.display(description='Stock')
    def stock_display(self, obj):
        if obj.fulfillment_type in ('voucher', 'external'):
            return f'{getattr(obj, "code_available", 0)} available'
        return 'Unlimited' if obj.stock_remaining is None else obj.stock_remaining

    @admin.display(description='Private inventory')
    def code_counts(self, obj):
        return f'{getattr(obj, "code_available", 0)} available / {getattr(obj, "code_assigned", 0)} assigned / {getattr(obj, "code_total", 0)} total'

    def changeform_view(self, request, object_id=None, form_url='', extra_context=None):
        if request.method == 'POST':
            with transaction.atomic():
                request.user = lock_staff_users(request.user, request.POST.getlist('eligible_users'))
                if not (self.has_change_permission(request) if object_id else self.has_add_permission(request)):
                    raise PermissionDenied
                if object_id:
                    try:
                        Reward.objects.select_for_update().get(pk=unquote(object_id))
                    except (Reward.DoesNotExist, ValidationError):
                        raise Http404 from None
                return super().changeform_view(request, object_id, form_url, extra_context)
        return super().changeform_view(request, object_id, form_url, extra_context)

    def save_model(self, request, obj, form, change):
        if not change:
            obj.save()
            return
        # Save only editable changes. A stale whole-model save could overwrite
        # inventory updated elsewhere, even when its field was not displayed.
        names = {field.name for field in obj._meta.concrete_fields if field.editable and not field.primary_key}
        changed = set(form.changed_data) & names
        if changed or 'eligible_users' in form.changed_data:
            obj.revision += 1
            obj.save(update_fields=changed | {'revision', 'updated_at'})


@admin.register(RewardCode)
class RewardCodeAdmin(NoDeleteAdmin):
    form = RewardCodeAdminForm
    change_list_template = 'admin/customerpanel/rewardcode/change_list.html'
    list_display = ('id', 'reward', 'inventory_status', 'payload_kind', 'expires_at', 'assigned_at', 'created_at')
    list_filter = (InventoryStatusFilter, 'payload_kind', ('reward', admin.RelatedOnlyFieldListFilter))
    search_fields = ('=id', 'reward__title', 'reward__partner_name')
    search_help_text = 'Search inventory references, reward titles or partners. Private values are never searchable.'
    list_select_related = ('reward',)
    readonly_fields = ('id', 'reward', 'payload_kind', 'inventory_status', 'redemption', 'assigned_at', 'created_at')
    fields = ('id', 'reward', 'payload_kind', 'inventory_status', 'is_active', 'expires_at', 'redemption', 'assigned_at', 'created_at', 'edit_token')

    def get_queryset(self, request):
        return super().get_queryset(request).defer('encrypted_payload', 'payload_fingerprint', 'encryption_key_id').annotate(
            inventory_state=Case(
                When(redemption__isnull=False, then=Value('assigned')),
                When(is_active=False, then=Value('disabled')),
                When(expires_at__lte=timezone.now(), then=Value('expired')),
                default=Value('available'), output_field=CharField(),
            ),
        )

    @admin.display(description='Status', ordering='inventory_state')
    def inventory_status(self, obj):
        return obj.inventory_state.title()

    def has_add_permission(self, request):
        # All new inventory must pass through encryption and batch validation.
        return False

    def has_change_permission(self, request, obj=None):
        return super().has_change_permission(request, obj) and (obj is None or obj.redemption_id is None)

    def get_readonly_fields(self, request, obj=None):
        if obj and obj.redemption_id:
            return self.readonly_fields + ('is_active', 'expires_at')
        return self.readonly_fields

    @staticmethod
    def can_import(request):
        return request.user.has_perm('customerpanel.add_rewardcode') and request.user.has_perm('customerpanel.change_reward')

    def get_urls(self):
        return [path('import/', self.admin_site.admin_view(self.import_view), name='customerpanel_rewardcode_import')] + super().get_urls()

    def changelist_view(self, request, extra_context=None):
        return super().changelist_view(request, {**(extra_context or {}), 'can_import': self.can_import(request)})

    def changeform_view(self, request, object_id=None, form_url='', extra_context=None):
        if request.method == 'POST' and object_id:
            with transaction.atomic():
                request.user = lock_staff_users(request.user)
                if not self.has_change_permission(request):
                    raise PermissionDenied
                try:
                    reward_id = RewardCode.objects.values_list('reward_id', flat=True).get(pk=unquote(object_id))
                    Reward.objects.select_for_update().get(pk=reward_id)
                    RewardCode.objects.select_for_update().only('id').get(pk=unquote(object_id))
                except (RewardCode.DoesNotExist, Reward.DoesNotExist, ValidationError):
                    raise Http404 from None
                return super().changeform_view(request, object_id, form_url, extra_context)
        return super().changeform_view(request, object_id, form_url, extra_context)

    def save_model(self, request, obj, form, change):
        changed = set(form.changed_data) & {'is_active', 'expires_at'}
        if changed:
            obj.save(update_fields=changed)
            reward = Reward.objects.get(pk=obj.reward_id)  # Parent lock already held.
            reward.revision += 1
            reward.save(update_fields=['revision', 'updated_at'])

    @method_decorator(sensitive_post_parameters('private_values'))
    @sensitive_variables()
    def import_view(self, request):
        if not self.can_import(request):
            raise PermissionDenied
        form = PrivateInventoryImportForm(request.POST or None)
        if request.method == 'POST' and form.is_valid():
            try:
                with transaction.atomic():
                    reward, rows = import_private_inventory(request.user, form.cleaned_data['reward'].pk,
                        form.cleaned_data['private_values'], expires_at=form.cleaned_data['expires_at'],
                        is_active=form.cleaned_data['is_active'])
                    LogEntry.objects.log_actions(user_id=request.user.pk, queryset=rows,
                        action_flag=ADDITION, change_message='Imported encrypted private inventory.')
                    self.admin_site._registry[Reward].log_change(request, reward, f'Imported {len(rows)} private inventory items.')
                self.message_user(request, f'Imported {len(rows)} private inventory items successfully.')
                return HttpResponseRedirect(reverse('admin:customerpanel_rewardcode_changelist', current_app=self.admin_site.name))
            except ValidationError as error:
                form.add_error(None, error)
        request.current_app = self.admin_site.name
        return TemplateResponse(request, 'admin/customerpanel/rewardcode/import.html', {
            **self.admin_site.each_context(request), 'title': 'Import private reward inventory',
            'opts': self.model._meta, 'form': form,
        })


class ReadOnlyRewardRecordAdmin(NoDeleteAdmin):
    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def get_readonly_fields(self, request, obj=None):
        return tuple(field.name for field in self.model._meta.concrete_fields)


@admin.register(Redemption)
class RedemptionAdmin(ReadOnlyRewardRecordAdmin):
    list_display = ('id', 'user', 'snapshot_title', 'points_spent', 'status', 'refunded_at', 'created_at', 'refund_link')
    list_filter = ('status', 'snapshot_fulfillment_type')
    search_fields = ('=id', 'user__username', 'user__email', 'snapshot_title', 'snapshot_partner_name')
    list_select_related = ('user', 'reward')
    date_hierarchy = 'created_at'

    @admin.display(description='Refund')
    def refund_link(self, obj):
        return format_html('<a href="{}">{}</a>', reverse('admin:customerpanel_redemption_refund', args=[obj.pk]),
            'View refund' if obj.refunded_at else 'Review full refund')

    def get_urls(self):
        return [path('<uuid:redemption_id>/refund/', self.admin_site.admin_view(self.refund_view),
                     name='customerpanel_redemption_refund')] + super().get_urls()

    @method_decorator(sensitive_post_parameters())
    @sensitive_variables()
    def refund_view(self, request, redemption_id):
        if not request.user.has_perm('customerpanel.change_redemption'):
            raise PermissionDenied
        record = self.get_object(request, str(redemption_id))
        if record is None:
            raise Http404
        fulfillment = RewardFulfillment.objects.filter(redemption=record).first()
        form = RefundForm(request.POST if request.method == 'POST' else None,
            initial={'confirmation_token': refund_confirmation(request.user, record, fulfillment)})
        if request.method == 'POST' and form.is_valid():
            try:
                with transaction.atomic():
                    result = refund_redemption(request.user, record.pk, **form.cleaned_data)
                    if not result.replayed:
                        self.log_change(request, record, 'Approved full Points refund. Stock restored.' if result.stock_restored else 'Approved full Points refund. Stock not restored.')
                self.message_user(request, 'This redemption was already refunded; no additional Points or stock were returned.'
                    if result.replayed else f'Full refund completed: {result.refunded_points} Points returned.')
                return HttpResponseRedirect(reverse('admin:customerpanel_redemption_refund', args=[record.pk]))
            except ValidationError as error:
                form.add_error(None, error)
        request.current_app = self.admin_site.name
        return TemplateResponse(request, 'admin/customerpanel/redemption/refund.html', {
            **self.admin_site.each_context(request), 'title': 'Review full refund', 'opts': self.model._meta,
            'record': record, 'fulfillment': fulfillment, 'form': form})


@admin.register(RewardFulfillment)
class RewardFulfillmentAdmin(ReadOnlyRewardRecordAdmin):
    actions = ('begin_processing', 'mark_dispatched', 'complete_fulfillment')
    list_display = ('id', 'redemption', 'status', 'city', 'country_code', 'created_at')
    list_filter = ('status', 'country_code')
    search_fields = ('=id', '=redemption__id', 'redemption__user__username', 'recipient_name', 'tracking_reference')
    list_select_related = ('redemption',)

    def has_process_permission(self, request):
        return request.user.is_active and request.user.has_perm('customerpanel.change_rewardfulfillment')

    def _process(self, request, queryset, status):
        completed = 0
        for pk in queryset.order_by('pk').values_list('pk', flat=True):
            try:
                process_fulfillment(request.user, pk, status)
                completed += 1
            except ValidationError:
                self.message_user(request, 'A selected fulfillment cannot make that status change.', level='warning')
        self.message_user(request, f'{completed} fulfillment records processed.')

    @admin.action(description='Begin fulfillment processing', permissions=['process'])
    def begin_processing(self, request, queryset):
        self._process(request, queryset, 'processing')

    @admin.action(description='Mark as dispatched', permissions=['process'])
    def mark_dispatched(self, request, queryset):
        self._process(request, queryset, 'dispatched')

    @admin.action(description='Complete fulfillment', permissions=['process'])
    def complete_fulfillment(self, request, queryset):
        self._process(request, queryset, 'completed')


@admin.register(RewardRequest)
class RewardRequestAdmin(ReadOnlyRewardRecordAdmin):
    list_display = ('id', 'user', 'title', 'category', 'status', 'created_at', 'review_link')
    list_filter = ('status', 'category', 'country_code')
    search_fields = ('=id', 'title', 'user__username', 'user__email')
    list_select_related = ('user', 'approved_reward')

    @admin.display(description='Review')
    def review_link(self, obj):
        if obj.redemption_id:
            return 'Accepted'
        return format_html('<a href="{}">Review request</a>', reverse('admin:customerpanel_rewardrequest_review', args=[obj.pk]))

    def get_urls(self):
        return [path('<uuid:request_id>/review/', self.admin_site.admin_view(self.review_view),
                     name='customerpanel_rewardrequest_review')] + super().get_urls()

    @method_decorator(sensitive_post_parameters())
    @sensitive_variables()
    def review_view(self, request, request_id):
        if not request.user.has_perm('customerpanel.change_rewardrequest'):
            raise PermissionDenied
        record = self.get_object(request, str(request_id))
        if record is None:
            raise Http404
        form = RewardRequestReviewForm(request.POST if request.method == 'POST' else None, initial={
            'review_token': review_token(record), 'status': record.status, 'approved_reward': record.approved_reward_id,
            'staff_response': record.staff_response, 'internal_notes': record.internal_notes})
        if request.method == 'POST' and form.is_valid():
            try:
                with transaction.atomic():
                    record = review_reward_request(request.user, record.pk, **form.cleaned_data)
                    self.log_change(request, record, 'Reviewed reward request. No Points spent.')
                self.message_user(request, 'Request reviewed. No Points were spent.')
                return HttpResponseRedirect(reverse('admin:customerpanel_rewardrequest_changelist'))
            except ValidationError as error:
                form.add_error(None, error)
        request.current_app = self.admin_site.name
        return TemplateResponse(request, 'admin/customerpanel/rewardrequest/review.html', {
            **self.admin_site.each_context(request), 'title': 'Review reward request', 'opts': self.model._meta,
            'record': record, 'form': form})


@admin.register(RedemptionEvent)
class RedemptionEventAdmin(ReadOnlyRewardRecordAdmin):
    list_display = ('id', 'redemption', 'event_type', 'actor', 'points_delta', 'created_at')
    list_filter = ('event_type',)
    search_fields = ('=id', '=redemption__id', 'redemption__user__username')
    list_select_related = ('redemption', 'actor')
    date_hierarchy = 'created_at'

