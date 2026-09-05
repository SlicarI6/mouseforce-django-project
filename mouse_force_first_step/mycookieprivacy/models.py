from django.db import models

class UserCookies(models.Model):
    user_ip = models.GenericIPAddressField()
    accepted_cookies = models.BooleanField(default=False)
    accepted_at = models.DateTimeField(auto_now_add=True)
    functional_advertising = models.BooleanField(default=False)
    functional_analytics = models.BooleanField(default=False)
    functional_personalisation = models.BooleanField(default=False)

    def __str__(self):
        return f"User {self.user_ip} - Cookies Accepted: {self.accepted_cookies}"