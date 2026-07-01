from decimal import Decimal
from typing import Any, Dict, Optional

from django import forms
from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _

from apps.orders.models import Order, OrderAddressSnapshot, OrderItem, Payment, Refund


class OrderCancelForm(forms.Form):
    """
    Form for customers to request an order cancellation.
    Validates that the order is in a cancellable state before allowing submission.
    """
    remarks = forms.CharField(
        label=_("Cancellation Reason"),
        widget=forms.Textarea(
            attrs={
                'rows': 3,
                'placeholder': _('Please let us know why you are cancelling this order. Your feedback helps us improve.'),
                'class': 'premium-input'
            }
        ),
        required=True,
        help_text=_("A brief reason for the cancellation is required.")
    )

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.order: Optional[Order] = kwargs.pop('order', None)
        super().__init__(*args, **kwargs)

    def clean(self) -> Dict[str, Any]:
        cleaned_data = super().clean()
        
        if self.order:
            # Business rule validation: Only allow cancellation if pending or processing.
            if self.order.status not in [Order.OrderStatus.PENDING, Order.OrderStatus.PROCESSING]:
                raise ValidationError(
                    _("This order has already been processed or shipped and can no longer be cancelled directly.")
                )
                
        return cleaned_data


class OrderRefundRequestForm(forms.Form):
    """
    Form for customers to request a refund against a completed payment.
    Enforces maximum refund amounts and verifies payment status.
    """
    payment_id = forms.ChoiceField(
        label=_("Select Payment"),
        required=True,
        widget=forms.Select(attrs={'class': 'premium-input'})
    )
    amount = forms.DecimalField(
        label=_("Requested Amount"),
        required=False,
        min_value=Decimal('0.01'),
        widget=forms.NumberInput(attrs={'class': 'premium-input', 'step': '0.01'}),
        help_text=_("Leave blank to request a full refund of the selected payment transaction.")
    )
    reason = forms.CharField(
        label=_("Reason for Refund"),
        widget=forms.Textarea(
            attrs={
                'rows': 4,
                'placeholder': _('Provide details about why you are requesting this refund.'),
                'class': 'premium-input'
            }
        ),
        required=True
    )

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.order: Optional[Order] = kwargs.pop('order', None)
        super().__init__(*args, **kwargs)
        
        self.completed_payments: Dict[str, Payment] = {}
        
        if self.order:
            # Populate choices strictly with COMPLETED payments
            payments = self.order.payments.filter(status=Payment.PaymentState.COMPLETED)
            choices = []
            for payment in payments:
                choices.append((str(payment.id), f"{payment.gateway} - {payment.transaction_id} ({payment.amount} {payment.currency})"))
                self.completed_payments[str(payment.id)] = payment
                
            self.fields['payment_id'].choices = choices

    def clean(self) -> Dict[str, Any]:
        cleaned_data = super().clean()
        payment_id = cleaned_data.get('payment_id')
        amount = cleaned_data.get('amount')

        if payment_id and self.completed_payments:
            payment = self.completed_payments.get(str(payment_id))
            if not payment:
                self.add_error('payment_id', _("Invalid payment transaction selected."))
                return cleaned_data

            # Check if there are already pending/processed refunds for this payment
            existing_refunds = Refund.objects.filter(
                payment=payment, 
                status__in=[Refund.RefundStatus.REQUESTED, Refund.RefundStatus.APPROVED, Refund.RefundStatus.PROCESSED]
            ).aggregate(total=forms.models.Sum('amount'))['total'] or Decimal('0.00')

            available_to_refund = payment.amount - existing_refunds

            if available_to_refund <= 0:
                raise ValidationError(_("This payment has already been fully refunded or has pending refund requests."))

            if amount:
                if amount > available_to_refund:
                    self.add_error(
                        'amount', 
                        _(f"Requested amount cannot exceed the available refundable amount ({available_to_refund} {payment.currency}).")
                    )
            else:
                # If left blank, default to the maximum available
                cleaned_data['amount'] = available_to_refund

        return cleaned_data


class ReturnRequestForm(forms.Form):
    """
    Form for initiating a physical return of order items.
    Dynamically generates quantity fields for eligible active items.
    """
    reason = forms.CharField(
        label=_("Reason for Return"),
        widget=forms.Textarea(
            attrs={
                'rows': 4,
                'placeholder': _('Please detail the condition of the items and why you wish to return them.'),
                'class': 'premium-input'
            }
        ),
        required=True
    )
    comments = forms.CharField(
        label=_("Additional Comments"),
        widget=forms.Textarea(attrs={'rows': 2, 'class': 'premium-input'}),
        required=False
    )

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.order: Optional[Order] = kwargs.pop('order', None)
        super().__init__(*args, **kwargs)
        
        self.eligible_items = []
        
        if self.order and self.order.status in [Order.OrderStatus.DELIVERED, Order.OrderStatus.COMPLETED]:
            self.eligible_items = list(self.order.items.filter(status=OrderItem.ItemStatus.ACTIVE))
            
            for item in self.eligible_items:
                field_name = f'item_qty_{item.id}'
                self.fields[field_name] = forms.IntegerField(
                    label=f"Return Qty: {item.product_name}",
                    min_value=0,
                    max_value=item.quantity,
                    initial=0,
                    required=False,
                    widget=forms.NumberInput(attrs={'class': 'premium-input', 'style': 'width: 80px;'})
                )

    def clean(self) -> Dict[str, Any]:
        cleaned_data = super().clean()
        
        if not self.order:
            raise ValidationError(_("Invalid order context."))
            
        if self.order.status not in [Order.OrderStatus.DELIVERED, Order.OrderStatus.COMPLETED]:
            raise ValidationError(_("Items can only be returned after the order has been delivered."))

        total_return_qty = 0
        for item in getattr(self, 'eligible_items', []):
            field_name = f'item_qty_{item.id}'
            qty = cleaned_data.get(field_name, 0)
            if qty > 0:
                total_return_qty += qty
                
        if total_return_qty == 0:
            raise ValidationError(_("You must select at least one item to return."))

        return cleaned_data


class OrderAddressUpdateForm(forms.ModelForm):
    """
    Form to update an order's immutable shipping or billing address snapshot.
    Typically utilized by staff or by customers during the pending state.
    """
    class Meta:
        model = OrderAddressSnapshot
        fields = [
            'full_name', 'phone_number', 'company', 'address_line_1', 
            'address_line_2', 'city', 'state_or_province', 'postal_code', 'country'
        ]
        widgets = {
            'full_name': forms.TextInput(attrs={'class': 'premium-input'}),
            'phone_number': forms.TextInput(attrs={'class': 'premium-input'}),
            'company': forms.TextInput(attrs={'class': 'premium-input'}),
            'address_line_1': forms.TextInput(attrs={'class': 'premium-input'}),
            'address_line_2': forms.TextInput(attrs={'class': 'premium-input'}),
            'city': forms.TextInput(attrs={'class': 'premium-input'}),
            'state_or_province': forms.TextInput(attrs={'class': 'premium-input'}),
            'postal_code': forms.TextInput(attrs={'class': 'premium-input'}),
            'country': forms.TextInput(attrs={'class': 'premium-input'}),
        }

    def clean_phone_number(self) -> str:
        phone = self.cleaned_data.get('phone_number', '')
        return "".join(phone.split())  # Strip internal whitespace

    def clean_postal_code(self) -> str:
        postal = self.cleaned_data.get('postal_code', '')
        return postal.strip().upper()


class OrderNotesForm(forms.ModelForm):
    """
    Form for updating logistical delivery instructions and general customer notes.
    """
    class Meta:
        model = Order
        fields = ['customer_note', 'delivery_instructions']
        widgets = {
            'customer_note': forms.Textarea(
                attrs={
                    'rows': 3, 
                    'class': 'premium-input',
                    'placeholder': _('Any general requests regarding this order.')
                }
            ),
            'delivery_instructions': forms.Textarea(
                attrs={
                    'rows': 3, 
                    'class': 'premium-input',
                    'placeholder': _('Gate codes, safe dropping locations, etc.')
                }
            ),
        }


class AdminOrderUpdateForm(forms.ModelForm):
    """
    Administrative form providing safe modification boundaries for internal staff.
    Restricts financial field manipulation while allowing tracking updates.
    """
    class Meta:
        model = Order
        fields = [
            'status', 'payment_status', 'carrier', 'tracking_number', 
            'tracking_url', 'invoice_url', 'has_invoice'
        ]
        widgets = {
            'status': forms.Select(attrs={'class': 'premium-input'}),
            'payment_status': forms.Select(attrs={'class': 'premium-input'}),
            'carrier': forms.TextInput(attrs={'class': 'premium-input'}),
            'tracking_number': forms.TextInput(attrs={'class': 'premium-input'}),
            'tracking_url': forms.URLInput(attrs={'class': 'premium-input'}),
            'invoice_url': forms.URLInput(attrs={'class': 'premium-input'}),
            'has_invoice': forms.CheckboxInput(),
        }

    def clean(self) -> Dict[str, Any]:
        cleaned_data = super().clean()
        
        # Enforce consistency between tracking details
        tracking_number = cleaned_data.get('tracking_number')
        carrier = cleaned_data.get('carrier')
        
        if tracking_number and not carrier:
            self.add_error('carrier', _("A carrier must be specified when adding a tracking number."))
            
        return cleaned_data