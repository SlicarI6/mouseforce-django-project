from django.contrib import admin
from mouse_force_first_step.accounts.models import CustomUser

@admin.register(CustomUser)
class CustomUserAdmin(admin.ModelAdmin):
    search_fields = ['email', 'username']
    
## Si eu pot sa il stilez acest admin sa imi arata doar acelea care sunt in migrations