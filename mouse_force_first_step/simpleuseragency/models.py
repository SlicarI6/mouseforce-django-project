from django.db import models
from django.contrib.auth import get_user_model

User = get_user_model()

class Subscription(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE)
    is_active = models.BooleanField(default=False)
    start_date = models.DateTimeField(auto_now_add=True)
    end_date = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"{self.user.email} - {'Active' if self.is_active else 'Inactive'}"
    
class SubscriptionVerification(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE)
    code = models.CharField(max_length=6)
    created_at = models.DateTimeField(auto_now_add=True)
    is_verified = models.BooleanField(default=False)

    def __str__(self):
        return f"{self.user.email} - Verified: {self.is_verified}"
