from django.contrib import admin
from .models import ContactSubmission

@admin.register(ContactSubmission)
class ContactSubmissionAdmin(admin.ModelAdmin):
    list_display = ('full_name', 'business_email', 'company', 'submitted_at')
    list_filter = ('submitted_at', 'consent')
    search_fields = ('full_name', 'business_email', 'company', 'current_locations', 'message')