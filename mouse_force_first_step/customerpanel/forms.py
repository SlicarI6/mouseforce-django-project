import unicodedata

from django import forms
from .models import Feedback


class CitySearchForm(forms.Form):
    city = forms.CharField(
        label='Search for a city', min_length=2, max_length=80,
        widget=forms.TextInput(attrs={
            'placeholder': 'Search for a city',
            'autocomplete': 'address-level2',
            'aria-describedby': 'weather-city-help weather-city-errors',
        }),
        error_messages={
            'required': 'Enter a city to see its weather.',
            'min_length': 'Enter at least two characters for the city name.',
            'max_length': 'Keep the city name to 80 characters or fewer.',
        },
    )

    def clean_city(self):
        city = unicodedata.normalize('NFKC', self.cleaned_data['city'])
        allowed_punctuation = " -.,'’()"
        if (
            not any(char.isalpha() for char in city)
            or any(
                unicodedata.category(char)[0] not in ('L', 'M', 'N')
                and char not in allowed_punctuation
                for char in city
            )
        ):
            raise forms.ValidationError('Enter a valid city name, for example London or São Paulo.')
        city = ' '.join(city.split())
        if not 2 <= len(city) <= 80:
            raise forms.ValidationError('Enter a city name between 2 and 80 characters.')
        return city


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
