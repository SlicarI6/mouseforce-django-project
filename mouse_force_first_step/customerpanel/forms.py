from django import forms
from .models import Feedback


class FeedbackForm(forms.ModelForm):
    class Meta:
        model = Feedback
        fields = ['rating', 'country', 'development_focus', 'message', 'audio']  # ✅ am adăugat 'audio'
        widgets = {
            'rating': forms.Select(attrs={'class': 'form-control'}),
            'country': forms.TextInput(attrs={'class': 'form-control'}),
            'development_focus': forms.TextInput(attrs={'class': 'form-control'}),
            'message': forms.Textarea(attrs={'class': 'form-control', 'rows': 4}),
            'audio': forms.FileInput(attrs={'class': 'form-control'}),  # ✅ widget pentru fișier
        }