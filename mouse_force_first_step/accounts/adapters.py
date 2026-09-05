from allauth.account.adapter import DefaultAccountAdapter
from allauth.exceptions import ImmediateHttpResponse
from django.shortcuts import redirect

class MyAccountAdapter(DefaultAccountAdapter):
    def is_open_for_signup(self, request):
        if request.path.startswith('/accounts/google/'):
            # Dacă userul vine de pe Google login și nu există → redirecționează
            raise ImmediateHttpResponse(redirect('google_account_not_registered'))
        return super().is_open_for_signup(request)