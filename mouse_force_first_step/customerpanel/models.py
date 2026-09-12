from django.db import models
from django.conf import settings
from django.contrib.auth import get_user_model
from django.utils import timezone

User = get_user_model()

class CustomerPoints(models.Model):
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        primary_key=True,
        on_delete=models.CASCADE,
    )
    total_points = models.PositiveBigIntegerField(default=0)
    last_daily_claim_date = models.DateField(null=True, blank=True)
    streak_days = models.PositiveSmallIntegerField(default=0)
    day_7_bonus_awarded = models.BooleanField(default=False)
    day_14_bonus_awarded = models.BooleanField(default=False)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=models.Q(streak_days__gte=0, streak_days__lte=14),
                name='customerpoints_streak_range',
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(streak_days=0, last_daily_claim_date__isnull=True)
                    | models.Q(streak_days__gte=1, last_daily_claim_date__isnull=False)
                ),
                name='customerpoints_claim_date',
            ),
            models.CheckConstraint(
                condition=models.Q(day_7_bonus_awarded=False) | models.Q(streak_days__gte=7),
                name='customerpoints_day7_eligible',
            ),
            models.CheckConstraint(
                condition=models.Q(day_14_bonus_awarded=False) | models.Q(streak_days=14),
                name='customerpoints_day14_eligible',
            ),
        ]


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