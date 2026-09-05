from django.db import models
from django.conf import settings
from django.contrib.auth import get_user_model
from django.utils import timezone

User = get_user_model()

class Feedback(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    rating = models.IntegerField(choices=[(i, str(i)) for i in range(1, 6)], null=True, blank=True)
    country = models.CharField(max_length=100, blank=True)
    development_focus = models.CharField(max_length=200, blank=True)
    message = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)
    audio = models.FileField(upload_to='feedback_audio/', null=True, blank=True)

    def __str__(self):
        return f"Feedback from {self.user.username} - {self.created_at.strftime('%Y-%m-%d')}"
    
    
class Message(models.Model):
    room_name = models.CharField(max_length=255)
    sender = models.ForeignKey(get_user_model(), on_delete=models.CASCADE)
    content = models.TextField()
    timestamp = models.DateTimeField(default=timezone.now)

    def __str__(self):
        return f"{self.sender.username}: {self.content[:20]}"


class Notification(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='notifications')
    message = models.TextField()
    is_read = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Notificare pentru {self.user.username} - {'citită' if self.is_read else 'necitită'}"