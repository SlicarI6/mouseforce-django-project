"""Discount publishing and read-only customer receipts/votes."""
from django import forms
from django.contrib import admin
from django.db import connection
from django.utils.decorators import method_decorator
from django.views.decorators.debug import sensitive_post_parameters

from .models import Discount, CustomerDiscountAccess, DiscountVote
from .discounts import current_window


class DiscountForm(forms.ModelForm):
    usage_channels = forms.MultipleChoiceField(choices=Discount.CHANNELS, required=False,
        widget=forms.CheckboxSelectMultiple, help_text='Select all applicable channels.')

    class Meta:
        model = Discount
        fields = '__all__'


class DiscountAvailabilityFilter(admin.SimpleListFilter):
    title = 'publication window'
    parameter_name = 'window'

    def lookups(self, request, model_admin):
        return [('current', 'Current'), ('not-current', 'Draft / scheduled / expired')]

    def queryset(self, request, queryset):
        if self.value() == 'current':
            return queryset.filter(current_window())
        if self.value() == 'not-current':
            return queryset.exclude(current_window())
        return queryset


@admin.register(Discount)
class DiscountAdmin(admin.ModelAdmin):
    form = DiscountForm
    list_display = ('brand', 'title', 'category', 'deal_type', 'value_label', 'points_to_unlock_deal', 'active', 'featured', 'valid_until', 'last_verified_at')
    list_filter = ('active', DiscountAvailabilityFilter, 'category', 'deal_type', 'country', 'ongoing', 'featured')
    search_fields = ('brand', 'title', 'search_keywords', 'country')
    readonly_fields = ('id', 'created_at', 'updated_at', 'free_published_at')
    fieldsets = (
        ('Deal', {'fields': ('brand', 'title', 'short_description', 'details', 'category', 'deal_type', 'value_label', 'percentage_value', 'money_off_value', 'currency')}),
        ('Requirements and location', {'fields': ('eligibility', 'country', 'region', 'usage_channels', 'terms_summary')}),
        ('Retailer access', {'fields': ('points_to_unlock_deal', 'promo_code', 'official_url'),
            'description': 'Only entitled customers receive the code/link. Permanent deal access is separate from the Discounts section fee.'}),
        ('Publication', {'fields': ('active', 'featured', 'valid_from', 'valid_until', 'ongoing', 'last_verified_at', 'search_keywords')}),
        ('Image', {'fields': ('image_url', 'image_alt')}),
        ('Record', {'fields': ('id', 'free_published_at', 'created_at', 'updated_at'), 'classes': ('collapse',)}),
    )

    def get_queryset(self, request):
        queryset = super().get_queryset(request)
        # Django wraps changeform POSTs in an atomic transaction. Serialize edits
        # with purchase/vote operations without ever taking a customer lock here.
        return queryset.select_for_update() if request.method == 'POST' and connection.in_atomic_block else queryset

    def get_readonly_fields(self, request, obj=None):
        return self.readonly_fields + (('points_to_unlock_deal',) if obj and obj.free_published_at else ())

    @method_decorator(sensitive_post_parameters('promo_code', 'official_url'))
    def changeform_view(self, request, object_id=None, form_url='', extra_context=None):
        return super().changeform_view(request, object_id, form_url, extra_context)


class ReadOnlyDiscountRecord(admin.ModelAdmin):
    actions = None

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(CustomerDiscountAccess)
class CustomerDiscountAccessAdmin(ReadOnlyDiscountRecord):
    list_display = ('user', 'discount', 'points_spent', 'balance_after', 'unlocked_at')
    list_select_related = ('user', 'discount')
    search_fields = ('user__username', 'user__email', 'discount__brand', 'discount__title')
    readonly_fields = ('id', 'user', 'discount', 'points_spent', 'balance_after', 'unlocked_at')
    date_hierarchy = 'unlocked_at'


@admin.register(DiscountVote)
class DiscountVoteAdmin(ReadOnlyDiscountRecord):
    list_display = ('user', 'discount', 'value', 'updated_at')
    list_filter = ('value',)
    list_select_related = ('user', 'discount')
    search_fields = ('user__username', 'discount__brand', 'discount__title')
    readonly_fields = ('id', 'user', 'discount', 'value', 'updated_at')
