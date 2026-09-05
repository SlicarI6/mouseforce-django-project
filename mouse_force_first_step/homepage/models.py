from django.db import models

class ContactSubmission(models.Model):
    full_name = models.CharField(max_length=255)
    business_email = models.EmailField()
    company = models.CharField(max_length=255, blank=True)
    current_locations = models.CharField(max_length=255, blank=True)
    message = models.TextField(blank=True)
    consent = models.BooleanField(default=False)
    submitted_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.full_name} - {self.business_email}"
