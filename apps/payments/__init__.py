"""
Payments Application.
Provides multi-gateway payment processing (eSewa, Khalti, Stripe, COD, Bank Wire),
transaction reconciliation, refund processing, and payment webhook verification.
"""

default_app_config = "apps.payments.apps.PaymentsConfig"