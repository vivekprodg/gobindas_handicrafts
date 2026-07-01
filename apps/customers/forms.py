from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth import password_validation
from django.contrib.auth.forms import (
    AuthenticationForm,
    PasswordChangeForm,
)
from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _
from apps.customers.models import CustomerProfile, CustomerAddress


class CustomerRegistrationForm(forms.ModelForm):
    """
    Handles secure and fully validated customer user registrations.
    Supports automatic case-insensitive email unique checks, password validation,
    and clean layout widgets with appropriate autocomplete tags.
    """
    first_name = forms.CharField(
        max_length=150,
        required=True,
        label=_("First Name"),
        widget=forms.TextInput(attrs={
            'placeholder': _('Enter your first name'),
            'autocomplete': 'given-name',
            'aria-label': _('First Name')
        })
    )
    last_name = forms.CharField(
        max_length=150,
        required=True,
        label=_("Last Name"),
        widget=forms.TextInput(attrs={
            'placeholder': _('Enter your last name'),
            'autocomplete': 'family-name',
            'aria-label': _('Last Name')
        })
    )
    email = forms.EmailField(
        required=True,
        label=_("Email Address"),
        widget=forms.EmailInput(attrs={
            'placeholder': _('Enter your email address'),
            'autocomplete': 'email',
            'aria-label': _('Email Address')
        })
    )
    password1 = forms.CharField(
        label=_("Password"),
        widget=forms.PasswordInput(attrs={
            'placeholder': _('Create a secure password'),
            'autocomplete': 'new-password',
            'aria-label': _('Password')
        }),
        help_text=password_validation.password_validators_help_text_html()
    )
    password2 = forms.CharField(
        label=_("Password confirmation"),
        widget=forms.PasswordInput(attrs={
            'placeholder': _('Repeat your secure password'),
            'autocomplete': 'new-password',
            'aria-label': _('Password Confirmation')
        })
    )

    class Meta:
        model = get_user_model()
        fields = ['username', 'first_name', 'last_name', 'email']
        widgets = {
            'username': forms.TextInput(attrs={
                'placeholder': _('Choose a unique username'),
                'autocomplete': 'username',
                'aria-label': _('Username')
            })
        }

    def clean_email(self) -> str:
        """
        Normalizes email data and runs a case-insensitive uniqueness verification 
        across active user entries.
        """
        email = self.cleaned_data.get('email')
        if email:
            email = email.strip().lower()
            user_model = get_user_model()
            if user_model.objects.filter(email__iexact=email).exists():
                raise ValidationError(
                    _("A user registration already exists with this email address."),
                    code='duplicate_email'
                )
        return email

    def clean_password2(self) -> str:
        """
        Validates that password entry rows match exactly.
        """
        p1 = self.cleaned_data.get('password1')
        p2 = self.cleaned_data.get('password2')
        if p1 and p2 and p1 != p2:
            raise ValidationError(
                _("The two password fields do not match."),
                code='password_mismatch'
            )
        return p2

    def clean(self) -> dict:
        """
        Applies standard backend security check parameters to password strength validation.
        """
        cleaned_data = super().clean()
        password = cleaned_data.get('password1')
        username = cleaned_data.get('username')
        email = cleaned_data.get('email')

        if password:
            user_model = get_user_model()
            # Construct a dummy user instance for context-aware password validation
            dummy_user = user_model(username=username, email=email)
            password_validation.validate_password(password, dummy_user)

        return cleaned_data

    def save(self, commit: bool = True):
        """
        Saves the authenticated user, ensures password encoding, and standardizes parameters.
        """
        user = super().save(commit=False)
        user.email = self.cleaned_data['email'].strip().lower()
        user.set_password(self.cleaned_data['password1'])
        if commit:
            user.save()
        return user


class CustomerLoginForm(AuthenticationForm):
    """
    Extends standard auth logic to handle clean login matching either 
    by standard username or case-insensitive email string addresses.
    """
    username = forms.CharField(
        label=_("Username or Email"),
        widget=forms.TextInput(attrs={
            'placeholder': _('Enter your username or email'),
            'autocomplete': 'username',
            'aria-label': _('Username or Email')
        })
    )
    password = forms.CharField(
        label=_("Password"),
        widget=forms.PasswordInput(attrs={
            'placeholder': _('Enter your password'),
            'autocomplete': 'current-password',
            'aria-label': _('Password')
        })
    )

    def clean(self) -> dict:
        """
        Translates incoming email logins into real user model identity nicknames 
        prior to executing standard auth validation.
        """
        login_input = self.cleaned_data.get('username')
        password = self.cleaned_data.get('password')

        if login_input and password:
            user_model = get_user_model()
            # Check for email matches case-insensitively
            if '@' in login_input:
                matched_user = user_model.objects.filter(email__iexact=login_input.strip()).first()
                if matched_user:
                    self.cleaned_data['username'] = matched_user.username

        # Hand over validation to base AuthenticationForm logic to protect against enumeration
        return super().clean()


class CustomerProfileForm(forms.ModelForm):
    """
    Unified form providing secure modification parameters across CustomerProfile model 
    as well as linked essential User auth fields.
    """
    first_name = forms.CharField(
        max_length=150,
        required=True,
        label=_("First Name"),
        widget=forms.TextInput(attrs={
            'placeholder': _('First name'),
            'autocomplete': 'given-name'
        })
    )
    last_name = forms.CharField(
        max_length=150,
        required=True,
        label=_("Last Name"),
        widget=forms.TextInput(attrs={
            'placeholder': _('Last name'),
            'autocomplete': 'family-name'
        })
    )
    email = forms.EmailField(
        required=True,
        label=_("Email Address"),
        widget=forms.EmailInput(attrs={
            'placeholder': _('Email address'),
            'autocomplete': 'email'
        })
    )

    class Meta:
        model = CustomerProfile
        fields = [
            'phone_number',
            'avatar',
            'date_of_birth',
            'gender',
            'preferred_language',
            'newsletter_subscribed'
        ]
        widgets = {
            'date_of_birth': forms.DateInput(attrs={
                'type': 'date',
                'placeholder': 'YYYY-MM-DD'
            }),
            'gender': forms.Select(),
            'preferred_language': forms.TextInput(attrs={
                'placeholder': _('e.g., en, fr, es')
            }),
            'avatar': forms.ClearableFileInput(),
            'newsletter_subscribed': forms.CheckboxInput()
        }

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        if self.instance and self.instance.user:
            self.fields['first_name'].initial = self.instance.user.first_name
            self.fields['last_name'].initial = self.instance.user.last_name
            self.fields['email'].initial = self.instance.user.email

    def clean_email(self) -> str:
        """
        Enforces strict validation against updating an email to match an existing account.
        """
        email = self.cleaned_data.get('email')
        if email:
            email = email.strip().lower()
            user_model = get_user_model()
            qs = user_model.objects.filter(email__iexact=email)
            if self.instance and self.instance.user:
                qs = qs.exclude(pk=self.instance.user.pk)
            if qs.exists():
                raise ValidationError(
                    _("This email address is already in use by another account."),
                    code='email_in_use'
                )
        return email

    def clean_phone_number(self) -> str:
        """
        Standardizes telephone whitespace formats.
        """
        phone = self.cleaned_data.get('phone_number')
        if phone:
            phone = "".join(phone.split())
        return phone

    def save(self, commit: bool = True) -> CustomerProfile:
        """
        Atomically saves both the user entity values and profile fields.
        """
        profile = super().save(commit=False)
        user = profile.user
        
        user.first_name = self.cleaned_data['first_name'].strip()
        user.last_name = self.cleaned_data['last_name'].strip()
        user.email = self.cleaned_data['email']
        
        if commit:
            user.save()
            profile.save()
            
        return profile


class CustomerAddressForm(forms.ModelForm):
    """
    ModelForm supporting operational workflows for creating or updating physical CustomerAddress entities.
    """
    class Meta:
        model = CustomerAddress
        fields = [
            'full_name',
            'phone_number',
            'address_line_1',
            'address_line_2',
            'city',
            'state_or_province',
            'postal_code',
            'country',
            'address_type',
            'is_default'
        ]
        widgets = {
            'full_name': forms.TextInput(attrs={'placeholder': _('Recipient full name')}),
            'phone_number': forms.TextInput(attrs={'placeholder': _('Delivery phone number')}),
            'address_line_1': forms.TextInput(attrs={'placeholder': _('Street address, P.O. box, company name')}),
            'address_line_2': forms.TextInput(attrs={'placeholder': _('Apartment, suite, unit, building, floor')}),
            'city': forms.TextInput(attrs={'placeholder': _('City')}),
            'state_or_province': forms.TextInput(attrs={'placeholder': _('State / Province / Region')}),
            'postal_code': forms.TextInput(attrs={'placeholder': _('Postal / ZIP code')}),
            'country': forms.TextInput(attrs={'placeholder': _('Country name')}),
            'address_type': forms.Select(),
            'is_default': forms.CheckboxInput()
        }

    def clean_phone_number(self) -> str:
        phone = self.cleaned_data.get('phone_number')
        if phone:
            phone = phone.strip()
        return phone

    def clean_postal_code(self) -> str:
        postal_code = self.cleaned_data.get('postal_code')
        if postal_code:
            postal_code = postal_code.strip().upper()
        return postal_code

    def clean(self) -> dict:
        cleaned_data = super().clean()
        # Standardize and normalize all text entries
        for field in ['full_name', 'address_line_1', 'address_line_2', 'city', 'state_or_province', 'country']:
            value = cleaned_data.get(field)
            if isinstance(value, str):
                cleaned_data[field] = " ".join(value.split())
        return cleaned_data


class CustomerPasswordChangeForm(PasswordChangeForm):
    """
    Extends Django's native security PasswordChangeForm workflow, providing 
    clean accessibility placeholders and structured markup hooks.
    """
    def __init__(self, user, *args, **kwargs) -> None:
        super().__init__(user, *args, **kwargs)
        
        self.fields['old_password'].widget = forms.PasswordInput(attrs={
            'placeholder': _('Enter current password'),
            'autocomplete': 'current-password',
            'aria-label': _('Current Password')
        })
        self.fields['new_password1'].widget = forms.PasswordInput(attrs={
            'placeholder': _('Enter shiny new password'),
            'autocomplete': 'new-password',
            'aria-label': _('New Password')
        })
        self.fields['new_password2'].widget = forms.PasswordInput(attrs={
            'placeholder': _('Verify shiny new password'),
            'autocomplete': 'new-password',
            'aria-label': _('Confirm New Password')
        })

    def clean_new_password2(self) -> str:
        """
        Verifies that new passwords match cleanly before handing off validation checks.
        """
        p1 = self.cleaned_data.get('new_password1')
        p2 = self.cleaned_data.get('new_password2')
        if p1 and p2 and p1 != p2:
            raise ValidationError(
                _("The two new password fields do not match."),
                code='password_mismatch'
            )
        return p2