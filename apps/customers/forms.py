import re
from typing import Any, Dict, Optional
from django import forms
from django.contrib.auth import get_user_model, password_validation
from django.contrib.auth.forms import AuthenticationForm, PasswordChangeForm, PasswordResetForm
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils.translation import gettext_lazy as _

from apps.customers.models import CustomerAddress, CustomerProfile

# ==============================================================================
# MASTER COUNTRY LIST FOR SEARCHABLE DROPDOWNS
# ==============================================================================
COUNTRY_CHOICES = (
    ("Nepal", "Nepal"),
    ("Afghanistan", "Afghanistan"),
    ("Albania", "Albania"),
    ("Algeria", "Algeria"),
    ("Andorra", "Andorra"),
    ("Angola", "Angola"),
    ("Antigua and Barbuda", "Antigua and Barbuda"),
    ("Argentina", "Argentina"),
    ("Armenia", "Armenia"),
    ("Australia", "Australia"),
    ("Austria", "Austria"),
    ("Azerbaijan", "Azerbaijan"),
    ("Bahamas", "Bahamas"),
    ("Bahrain", "Bahrain"),
    ("Bangladesh", "Bangladesh"),
    ("Barbados", "Barbados"),
    ("Belarus", "Belarus"),
    ("Belgium", "Belgium"),
    ("Belize", "Belize"),
    ("Benin", "Benin"),
    ("Bhutan", "Bhutan"),
    ("Bolivia", "Bolivia"),
    ("Bosnia and Herzegovina", "Bosnia and Herzegovina"),
    ("Botswana", "Botswana"),
    ("Brazil", "Brazil"),
    ("Brunei", "Brunei"),
    ("Bulgaria", "Bulgaria"),
    ("Burkina Faso", "Burkina Faso"),
    ("Burundi", "Burundi"),
    ("Cambodia", "Cambodia"),
    ("Cameroon", "Cameroon"),
    ("Canada", "Canada"),
    ("Cape Verde", "Cape Verde"),
    ("Central African Republic", "Central African Republic"),
    ("Chad", "Chad"),
    ("Chile", "Chile"),
    ("China", "China"),
    ("Colombia", "Colombia"),
    ("Comoros", "Comoros"),
    ("Congo", "Congo"),
    ("Costa Rica", "Costa Rica"),
    ("Croatia", "Croatia"),
    ("Cuba", "Cuba"),
    ("Cyprus", "Cyprus"),
    ("Czech Republic", "Czech Republic"),
    ("Denmark", "Denmark"),
    ("Djibouti", "Djibouti"),
    ("Dominica", "Dominica"),
    ("Dominican Republic", "Dominican Republic"),
    ("East Timor", "East Timor"),
    ("Ecuador", "Ecuador"),
    ("Egypt", "Egypt"),
    ("El Salvador", "El Salvador"),
    ("Equatorial Guinea", "Equatorial Guinea"),
    ("Eritrea", "Eritrea"),
    ("Estonia", "Estonia"),
    ("Eswatini", "Eswatini"),
    ("Ethiopia", "Ethiopia"),
    ("Fiji", "Fiji"),
    ("Finland", "Finland"),
    ("France", "France"),
    ("Gabon", "Gabon"),
    ("Gambia", "Gambia"),
    ("Georgia", "Georgia"),
    ("Germany", "Germany"),
    ("Ghana", "Ghana"),
    ("Greece", "Greece"),
    ("Grenada", "Grenada"),
    ("Guatemala", "Guatemala"),
    ("Guinea", "Guinea"),
    ("Guinea-Bissau", "Guinea-Bissau"),
    ("Guyana", "Guyana"),
    ("Haiti", "Haiti"),
    ("Honduras", "Honduras"),
    ("Hungary", "Hungary"),
    ("Iceland", "Iceland"),
    ("India", "India"),
    ("Indonesia", "Indonesia"),
    ("Iran", "Iran"),
    ("Iraq", "Iraq"),
    ("Ireland", "Ireland"),
    ("Israel", "Israel"),
    ("Italy", "Italy"),
    ("Ivory Coast", "Ivory Coast"),
    ("Jamaica", "Jamaica"),
    ("Japan", "Japan"),
    ("Jordan", "Jordan"),
    ("Kazakhstan", "Kazakhstan"),
    ("Kenya", "Kenya"),
    ("Kiribati", "Kiribati"),
    ("Korea, North", "Korea, North"),
    ("Korea, South", "Korea, South"),
    ("Kosovo", "Kosovo"),
    ("Kuwait", "Kuwait"),
    ("Kyrgyzstan", "Kyrgyzstan"),
    ("Laos", "Laos"),
    ("Latvia", "Latvia"),
    ("Lebanon", "Lebanon"),
    ("Lesotho", "Lesotho"),
    ("Liberia", "Liberia"),
    ("Libya", "Libya"),
    ("Liechtenstein", "Liechtenstein"),
    ("Lithuania", "Lithuania"),
    ("Luxembourg", "Luxembourg"),
    ("Madagascar", "Madagascar"),
    ("Malawi", "Malawi"),
    ("Malaysia", "Malaysia"),
    ("Maldives", "Maldives"),
    ("Mali", "Mali"),
    ("Malta", "Malta"),
    ("Marshall Islands", "Marshall Islands"),
    ("Mauritania", "Mauritania"),
    ("Mauritius", "Mauritius"),
    ("Mexico", "Mexico"),
    ("Micronesia", "Micronesia"),
    ("Moldova", "Moldova"),
    ("Monaco", "Monaco"),
    ("Mongolia", "Mongolia"),
    ("Montenegro", "Montenegro"),
    ("Morocco", "Morocco"),
    ("Mozambique", "Mozambique"),
    ("Myanmar", "Myanmar"),
    ("Namibia", "Namibia"),
    ("Nauru", "Nauru"),
    ("Netherlands", "Netherlands"),
    ("New Zealand", "New Zealand"),
    ("Nicaragua", "Nicaragua"),
    ("Niger", "Niger"),
    ("Nigeria", "Nigeria"),
    ("North Macedonia", "North Macedonia"),
    ("Norway", "Norway"),
    ("Oman", "Oman"),
    ("Pakistan", "Pakistan"),
    ("Palau", "Palau"),
    ("Palestine", "Palestine"),
    ("Panama", "Panama"),
    ("Papua New Guinea", "Papua New Guinea"),
    ("Paraguay", "Paraguay"),
    ("Peru", "Peru"),
    ("Philippines", "Philippines"),
    ("Poland", "Poland"),
    ("Portugal", "Portugal"),
    ("Qatar", "Qatar"),
    ("Romania", "Romania"),
    ("Russia", "Russia"),
    ("Rwanda", "Rwanda"),
    ("Saint Kitts and Nevis", "Saint Kitts and Nevis"),
    ("Saint Lucia", "Saint Lucia"),
    ("Saint Vincent and the Grenadines", "Saint Vincent and the Grenadines"),
    ("Samoa", "Samoa"),
    ("San Marino", "San Marino"),
    ("Sao Tome and Principe", "Sao Tome and Principe"),
    ("Saudi Arabia", "Saudi Arabia"),
    ("Senegal", "Senegal"),
    ("Serbia", "Serbia"),
    ("Seychelles", "Seychelles"),
    ("Sierra Leone", "Sierra Leone"),
    ("Singapore", "Singapore"),
    ("Slovakia", "Slovakia"),
    ("Slovenia", "Slovenia"),
    ("Solomon Islands", "Solomon Islands"),
    ("Somalia", "Somalia"),
    ("South Africa", "South Africa"),
    ("South Sudan", "South Sudan"),
    ("Spain", "Spain"),
    ("Sri Lanka", "Sri Lanka"),
    ("Sudan", "Sudan"),
    ("Suriname", "Suriname"),
    ("Sweden", "Sweden"),
    ("Switzerland", "Switzerland"),
    ("Syria", "Syria"),
    ("Taiwan", "Taiwan"),
    ("Tajikistan", "Tajikistan"),
    ("Tanzania", "Tanzania"),
    ("Thailand", "Thailand"),
    ("Togo", "Togo"),
    ("Tonga", "Tonga"),
    ("Trinidad and Tobago", "Trinidad and Tobago"),
    ("Tunisia", "Tunisia"),
    ("Turkey", "Turkey"),
    ("Turkmenistan", "Turkmenistan"),
    ("Tuvalu", "Tuvalu"),
    ("Uganda", "Uganda"),
    ("Ukraine", "Ukraine"),
    ("United Arab Emirates", "United Arab Emirates"),
    ("United Kingdom", "United Kingdom"),
    ("United States", "United States"),
    ("Uruguay", "Uruguay"),
    ("Uzbekistan", "Uzbekistan"),
    ("Vanuatu", "Vanuatu"),
    ("Vatican City", "Vatican City"),
    ("Venezuela", "Venezuela"),
    ("Vietnam", "Vietnam"),
    ("Yemen", "Yemen"),
    ("Zambia", "Zambia"),
    ("Zimbabwe", "Zimbabwe"),
)

class CustomerRegistrationForm(forms.ModelForm):
    """
    Unified Registration Form supporting both Individual buyers and Wholesalers/Organizations/Bulk Suppliers.
    Enforces mandatory baseline fields: Name, Address, Telephone Number, and Email ID.
    Enforces dynamic Tax/PAN validation for Nepal vs International business entities.
    """
    account_type = forms.ChoiceField(
        choices=CustomerProfile.AccountType.choices,
        initial=CustomerProfile.AccountType.INDIVIDUAL,
        label=_("Account Type"),
        widget=forms.Select(attrs={
            'class': 'form-select',
            'id': 'id_account_type'
        })
    )

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
    phone_number = forms.CharField(
        max_length=32,
        required=True,
        label=_("Telephone / Mobile Number"),
        widget=forms.TextInput(attrs={
            'placeholder': _('Enter telephone number (e.g. +977-9800000000)'),
            'autocomplete': 'tel',
            'aria-label': _('Telephone Number')
        })
    )

    # --------------------------------------------------------------------------
    # Mandatory Address Fields
    # --------------------------------------------------------------------------
    address_line_1 = forms.CharField(
        max_length=255,
        required=True,
        label=_("Street Address / Address Line 1"),
        widget=forms.TextInput(attrs={
            'placeholder': _('Enter street address or building number'),
            'autocomplete': 'address-line1'
        })
    )
    address_line_2 = forms.CharField(
        max_length=255,
        required=False,
        label=_("Address Line 2 (Optional)"),
        widget=forms.TextInput(attrs={
            'placeholder': _('Apartment, suite, unit, floor')
        })
    )
    city = forms.CharField(
        max_length=100,
        required=True,
        label=_("City / Town"),
        widget=forms.TextInput(attrs={
            'placeholder': _('City'),
            'autocomplete': 'address-level2'
        })
    )
    state_or_province = forms.CharField(
        max_length=100,
        required=True,
        label=_("State / Province / Region"),
        widget=forms.TextInput(attrs={
            'placeholder': _('State or Province'),
            'autocomplete': 'address-level1'
        })
    )
    postal_code = forms.CharField(
        max_length=20,
        required=False,
        label=_("Postal / ZIP Code"),
        widget=forms.TextInput(attrs={
            'placeholder': _('Postal Code'),
            'autocomplete': 'postal-code'
        })
    )
    country = forms.ChoiceField(
        choices=COUNTRY_CHOICES,
        initial="Nepal",
        required=True,
        label=_("Country"),
        widget=forms.Select(attrs={
            'class': 'form-select country-select',
            'id': 'id_registration_country',
            'data-searchable': 'true'
        })
    )

    # --------------------------------------------------------------------------
    # B2B / Wholesale / Bulk Supplier / Organization Dynamic Fields
    # --------------------------------------------------------------------------
    company_name = forms.CharField(
        max_length=255,
        required=False,
        label=_("Registered Company / Organization Name"),
        widget=forms.TextInput(attrs={
            'placeholder': _('Enter legal company name')
        })
    )
    business_type = forms.ChoiceField(
        choices=[('', _('-- Select Business Category --'))] + list(CustomerProfile.BusinessType.choices),
        required=False,
        label=_("Industry / Business Category"),
        widget=forms.Select(attrs={'class': 'form-select'})
    )
    tax_id_number = forms.CharField(
        max_length=100,
        required=False,
        label=_("Tax Identification Number (Nepal 9-digit PAN/VAT or International Tax ID)"),
        widget=forms.TextInput(attrs={
            'placeholder': _('9-digit PAN/VAT for Nepal entities or EIN/VAT for International')
        })
    )
    business_registration_number = forms.CharField(
        max_length=100,
        required=False,
        label=_("Company Registration / License Number"),
        widget=forms.TextInput(attrs={
            'placeholder': _('Registration number')
        })
    )
    country_of_incorporation = forms.ChoiceField(
        choices=COUNTRY_CHOICES,
        initial="Nepal",
        required=False,
        label=_("Country of Registration / Incorporation"),
        widget=forms.Select(attrs={
            'class': 'form-select country-select',
            'data-searchable': 'true'
        })
    )
    business_website = forms.URLField(
        required=False,
        label=_("Business Website"),
        widget=forms.URLInput(attrs={
            'placeholder': _('https://www.company.com')
        })
    )

    # Security Credentials
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

    def clean_phone_number(self) -> str:
        phone = self.cleaned_data.get('phone_number', '').strip()
        if not phone:
            raise ValidationError(_("Telephone / Mobile number is required."), code='required_phone')
        cleaned_phone = "".join(phone.split())
        return cleaned_phone

    def clean_password2(self) -> str:
        p1 = self.cleaned_data.get('password1')
        p2 = self.cleaned_data.get('password2')
        if p1 and p2 and p1 != p2:
            raise ValidationError(
                _("The two password fields do not match."),
                code='password_mismatch'
            )
        return p2

    def clean(self) -> dict:
        cleaned_data = super().clean()
        account_type = cleaned_data.get('account_type')
        password = cleaned_data.get('password1')
        username = cleaned_data.get('username')
        email = cleaned_data.get('email')

        if password:
            user_model = get_user_model()
            dummy_user = user_model(username=username, email=email)
            password_validation.validate_password(password, dummy_user)

        # Mandatory Baseline Validations for Name and Address
        if not cleaned_data.get('first_name'):
            self.add_error('first_name', _("First Name is required."))
        if not cleaned_data.get('last_name'):
            self.add_error('last_name', _("Last Name is required."))
        if not cleaned_data.get('address_line_1'):
            self.add_error('address_line_1', _("Address Line 1 is required."))
        if not cleaned_data.get('city'):
            self.add_error('city', _("City is required."))
        if not cleaned_data.get('state_or_province'):
            self.add_error('state_or_province', _("State / Province is required."))
        if not cleaned_data.get('country'):
            self.add_error('country', _("Country is required."))

        # ----------------------------------------------------------------------
        # Wholesale / Organization Dynamic Validation
        # ----------------------------------------------------------------------
        if account_type in [CustomerProfile.AccountType.WHOLESALE, CustomerProfile.AccountType.ORGANIZATION]:
            company_name = cleaned_data.get('company_name', '').strip()
            tax_id = cleaned_data.get('tax_id_number', '').strip()
            reg_num = cleaned_data.get('business_registration_number', '').strip()
            inc_country = str(cleaned_data.get('country_of_incorporation', '') or cleaned_data.get('country', '')).strip()

            if not company_name:
                self.add_error('company_name', _("Company / Organization Name is mandatory for Wholesale and Business accounts."))

            is_nepal = inc_country.lower() in ['nepal', 'np']

            if is_nepal:
                # Nepal Domestic Business Rule: Must supply a valid 9-digit numeric PAN/VAT Number
                if not tax_id:
                    self.add_error('tax_id_number', _("9-digit PAN/VAT Number is mandatory for Nepal registered business entities."))
                else:
                    digits = re.sub(r'\D', '', tax_id)
                    if len(digits) != 9:
                        self.add_error('tax_id_number', _("Nepal PAN/VAT Number must consist of exactly 9 numeric digits."))
            else:
                # International Entity Rule: Must supply Tax ID / EIN / VAT or Business Registration Number
                if not tax_id and not reg_num:
                    self.add_error(
                        'tax_id_number',
                        _("International organizations must provide either a Business Tax ID (EIN/VAT) or Company Registration Number.")
                    )

        return cleaned_data

    @transaction.atomic
    def save(self, commit: bool = True):
        user = super().save(commit=False)
        user.email = self.cleaned_data['email'].strip().lower()
        user.set_password(self.cleaned_data['password1'])
        
        if commit:
            user.save()
            profile, _ = CustomerProfile.objects.get_or_create(user=user)
            
            # Populate Profile Details
            profile.account_type = self.cleaned_data.get('account_type')
            profile.phone_number = self.cleaned_data.get('phone_number')
            
            if profile.is_business_account:
                profile.company_name = self.cleaned_data.get('company_name')
                profile.business_type = self.cleaned_data.get('business_type') or None
                profile.tax_id_number = self.cleaned_data.get('tax_id_number')
                profile.business_registration_number = self.cleaned_data.get('business_registration_number')
                profile.country_of_incorporation = self.cleaned_data.get('country_of_incorporation') or self.cleaned_data.get('country')
                profile.business_website = self.cleaned_data.get('business_website')
            
            profile.save()

            # Automatically Save Mandatory Primary Address
            full_name = f"{user.first_name} {user.last_name}".strip()
            CustomerAddress.objects.create(
                customer=profile,
                full_name=full_name,
                phone_number=self.cleaned_data.get('phone_number'),
                address_line_1=self.cleaned_data.get('address_line_1'),
                address_line_2=self.cleaned_data.get('address_line_2', ''),
                city=self.cleaned_data.get('city'),
                state_or_province=self.cleaned_data.get('state_or_province'),
                postal_code=self.cleaned_data.get('postal_code', ''),
                country=self.cleaned_data.get('country'),
                address_type=CustomerAddress.AddressType.BOTH,
                is_default=True,
                is_active=True
            )

        return user

class CustomerLoginForm(AuthenticationForm):
    """
    Extends authentication form to support login via either username or email address.
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
        login_input = self.cleaned_data.get('username')
        password = self.cleaned_data.get('password')

        if login_input and password and '@' in login_input:
            user_model = get_user_model()
            matched_user = user_model.objects.filter(email__iexact=login_input.strip()).first()
            if matched_user:
                self.cleaned_data['username'] = matched_user.username

        return super().clean()

class CustomerProfileForm(forms.ModelForm):
    """
    Unified form to manage CustomerProfile, Organization details, and related User attributes simultaneously.
    """
    first_name = forms.CharField(
        max_length=150,
        required=True,
        label=_("First Name"),
        widget=forms.TextInput(attrs={'placeholder': _('First name'), 'autocomplete': 'given-name'})
    )
    last_name = forms.CharField(
        max_length=150,
        required=True,
        label=_("Last Name"),
        widget=forms.TextInput(attrs={'placeholder': _('Last name'), 'autocomplete': 'family-name'})
    )
    email = forms.EmailField(
        required=True,
        label=_("Email Address"),
        widget=forms.EmailInput(attrs={'placeholder': _('Email address'), 'autocomplete': 'email'})
    )
    country_of_incorporation = forms.ChoiceField(
        choices=COUNTRY_CHOICES,
        initial="Nepal",
        required=False,
        label=_("Country of Registration / Incorporation"),
        widget=forms.Select(attrs={'class': 'form-select country-select', 'data-searchable': 'true'})
    )

    class Meta:
        model = CustomerProfile
        fields = [
            'account_type',
            'phone_number',
            'company_name',
            'business_type',
            'tax_id_number',
            'business_registration_number',
            'country_of_incorporation',
            'business_website',
            'avatar',
            'date_of_birth',
            'gender',
            'preferred_language',
            'newsletter_subscribed'
        ]
        widgets = {
            'account_type': forms.Select(attrs={'class': 'form-select'}),
            'business_type': forms.Select(attrs={'class': 'form-select'}),
            'date_of_birth': forms.DateInput(attrs={'type': 'date', 'placeholder': 'YYYY-MM-DD'}),
            'gender': forms.Select(),
            'preferred_language': forms.TextInput(attrs={'placeholder': _('e.g., en, fr, es')}),
            'avatar': forms.ClearableFileInput(),
            'newsletter_subscribed': forms.CheckboxInput()
        }

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        if self.instance and self.instance.user:
            self.fields['first_name'].initial = self.instance.user.first_name
            self.fields['last_name'].initial = self.instance.user.last_name
            self.fields['email'].initial = self.instance.user.email

    def clean_email(self) -> str:
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
        phone = self.cleaned_data.get('phone_number')
        if phone:
            phone = "".join(phone.split())
        return phone

    def save(self, commit: bool = True) -> CustomerProfile:
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
    Form to manage CustomerAddress creation and updates.
    """
    country = forms.ChoiceField(
        choices=COUNTRY_CHOICES,
        initial="Nepal",
        required=True,
        label=_("Country"),
        widget=forms.Select(attrs={
            'class': 'form-select country-select',
            'data-searchable': 'true'
        })
    )

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
            'address_type': forms.Select(),
            'is_default': forms.CheckboxInput()
        }

    def clean_phone_number(self) -> str:
        phone = self.cleaned_data.get('phone_number')
        return phone.strip() if phone else ""

    def clean_postal_code(self) -> str:
        postal_code = self.cleaned_data.get('postal_code')
        return postal_code.strip().upper() if postal_code else ""

    def clean(self) -> dict:
        cleaned_data = super().clean()
        for field in ['full_name', 'address_line_1', 'address_line_2', 'city', 'state_or_province', 'country']:
            value = cleaned_data.get(field)
            if isinstance(value, str):
                cleaned_data[field] = " ".join(value.split())
        return cleaned_data

class CustomerPasswordChangeForm(PasswordChangeForm):
    """
    Password change form with accessibility and clean placeholder support.
    """
    def __init__(self, user, *args, **kwargs) -> None:
        super().__init__(user, *args, **kwargs)
        
        self.fields['old_password'].widget = forms.PasswordInput(attrs={
            'placeholder': _('Enter current password'),
            'autocomplete': 'current-password',
            'aria-label': _('Current Password')
        })
        self.fields['new_password1'].widget = forms.PasswordInput(attrs={
            'placeholder': _('Enter new password'),
            'autocomplete': 'new-password',
            'aria-label': _('New Password')
        })
        self.fields['new_password2'].widget = forms.PasswordInput(attrs={
            'placeholder': _('Confirm new password'),
            'autocomplete': 'new-password',
            'aria-label': _('Confirm New Password')
        })

    def clean_new_password2(self) -> str:
        p1 = self.cleaned_data.get('new_password1')
        p2 = self.cleaned_data.get('new_password2')
        if p1 and p2 and p1 != p2:
            raise ValidationError(
                _("The two new password fields do not match."),
                code='password_mismatch'
            )
        return p2

class CustomerPasswordResetForm(PasswordResetForm):
    """
    Password reset request form with custom styled inputs and email validation.
    """
    email = forms.EmailField(
        label=_("Email Address"),
        max_length=254,
        widget=forms.EmailInput(attrs={
            'placeholder': _('Enter your registered email address'),
            'autocomplete': 'email',
            'class': 'form-input',
            'required': 'required',
        })
    )

    def clean_email(self) -> str:
        email = self.cleaned_data.get('email', '').strip().lower()
        user_model = get_user_model()
        if not user_model.objects.filter(email__iexact=email, is_active=True).exists():
            raise ValidationError(
                _("No active user account was found with this email address."),
                code='email_not_found'
            )
        return email