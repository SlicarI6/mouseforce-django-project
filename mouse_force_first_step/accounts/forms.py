from django import forms
from captcha.fields import CaptchaField
from .models import CustomUser


class CustomUserCreationForm(forms.ModelForm):
    password1 = forms.CharField(label='Password', widget=forms.PasswordInput)
    password2 = forms.CharField(label='Confirm Password', widget=forms.PasswordInput)
    birth_date = forms.DateField(widget=forms.DateInput(attrs={'type': 'date'}))
    captcha = CaptchaField()
    role = None

    class Meta:
        model = CustomUser
        fields = ['first_name', 'last_name', 'email', 'birth_date', 'password1', 'password2']

    def __init__(self, *args, **kwargs):
        self.role = kwargs.pop('role', None)
        super().__init__(*args, **kwargs)

    def clean_email(self):
        email = self.cleaned_data.get('email')
        existing_user = CustomUser.objects.filter(email=email).first()

        if existing_user:
            if self.role == 'customer' and existing_user.role == 'simple': 
                raise forms.ValidationError(
                    "An account with this email already exists in Skilled Worker Finder . Use another email."
                )
            elif self.role == 'simple' and existing_user.role == 'customer':
                raise forms.ValidationError(
                    "An account with this email already exists in  Customer IT Innovation . Use another email."
                )
            else:
                raise forms.ValidationError(
                    "An account with this email already exists. Please login instead."
                )
        return email

    def clean(self):
        cleaned_data = super().clean()

        # Dacă captcha a dat eroare, ieșim devreme
        if self.errors.get('captcha'):
            return cleaned_data

        password1 = cleaned_data.get("password1")
        password2 = cleaned_data.get("password2")

        if password1 and password2 and password1 != password2:
            raise forms.ValidationError("Passwords do not match.")

        return cleaned_data

    def save(self, commit=True):
        user = super().save(commit=False)
        base_username = f"{self.cleaned_data['first_name'].lower()}{self.cleaned_data['last_name'].lower()}"
        birth_date = self.cleaned_data.get('birth_date')
        suffix = str(birth_date.year)[-2:] if birth_date else ''
        username = base_username

        if CustomUser.objects.filter(username=username).exists():
            username = f"{base_username}{suffix}"
            if CustomUser.objects.filter(username=username).exists():
                username = f"{base_username}{suffix}{birth_date.day}"

        user.username = username
        user.set_password(self.cleaned_data["password1"])
        if commit:
            user.save()
        return user


# 👇 Formular simplu doar pentru test captcha
class CaptchaTestForm(forms.Form):
    captcha = CaptchaField()
