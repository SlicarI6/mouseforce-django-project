from .models import Notification
from django.contrib.auth import get_user_model

User = get_user_model()

def create_notification(user, message):
    Notification.objects.create(user=user, message=message)