from django.contrib.auth.models import AbstractUser
from django.db import models

class CustomUser(AbstractUser):
    ROLE_CHOICES = (
        ('user', 'Simple User'),
        ('customer', 'Customer Account'),
    )
    role = models.CharField(max_length=10, choices=ROLE_CHOICES, default='user')
    birth_date = models.DateField(null=True, blank=True)
    profile_picture = models.URLField(blank=True, null=True)