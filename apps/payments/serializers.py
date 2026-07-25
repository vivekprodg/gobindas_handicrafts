from typing import Any, Dict, List
from .models import PaymentTransaction

class PaymentSerializer:
    @staticmethod
    def serialize_transaction(transaction: PaymentTransaction) -> Dict[str, Any]:
        if not transaction:
            return {}

        return {
            "id": transaction.pk,
            "transaction_id": transaction.transaction_id,
            "order_number": transaction.order.order_number if transaction.order else "",
            "gateway": transaction.gateway,
            "gateway_display": transaction.get_gateway_display(),
            "amount": str(transaction.amount),
            "currency": transaction.currency,
            "status": transaction.status,
            "status_display": transaction.get_status_display(),
            "paid_at": transaction.paid_at.isoformat() if transaction.paid_at else None,
        }

    @classmethod
    def serialize_transactions_many(cls, transactions: List[PaymentTransaction]) -> List[Dict[str, Any]]:
        return [cls.serialize_transaction(t) for t in transactions if t]