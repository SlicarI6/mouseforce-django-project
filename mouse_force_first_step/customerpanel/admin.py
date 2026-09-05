from django.contrib import admin
from .models import Feedback
from django.contrib.admin.views.decorators import staff_member_required
from django.contrib.auth import get_user_model

admin.site.register(Feedback)

