from typing import Final, Tuple

class AuditAction:
    CREATE: Final[str] = "create"
    UPDATE: Final[str] = "update"
    DELETE: Final[str] = "delete"
    LOGIN: Final[str] = "login"
    LOGOUT: Final[str] = "logout"
    FAILED_LOGIN: Final[str] = "failed_login"
    PASSWORD_CHANGE: Final[str] = "password_change"
    CHECKOUT: Final[str] = "checkout"
    REFUND: Final[str] = "refund"
    INVENTORY_MUTATION: Final[str] = "inventory_mutation"

    CHOICES: Final[Tuple[Tuple[str, str], ...]] = (
        (CREATE, "Record Created"),
        (UPDATE, "Record Updated"),
        (DELETE, "Record Deleted"),
        (LOGIN, "User Sign-In"),
        (LOGOUT, "User Sign-Out"),
        (FAILED_LOGIN, "Failed Sign-In Attempt"),
        (PASSWORD_CHANGE, "Password Reset / Change"),
        (CHECKOUT, "Order Checkout Processed"),
        (REFUND, "Refund Processed"),
        (INVENTORY_MUTATION, "Inventory Adjusted"),
    )

class AuditSeverity:
    INFO: Final[str] = "info"
    WARNING: Final[str] = "warning"
    SECURITY: Final[str] = "security"
    CRITICAL: Final[str] = "critical"

    CHOICES: Final[Tuple[Tuple[str, str], ...]] = (
        (INFO, "Informational"),
        (WARNING, "Warning / Alert"),
        (SECURITY, "Security Event"),
        (CRITICAL, "Critical System Event"),
    )

LOGGER_NAME: Final[str] = "apps.audit"
CACHE_NAMESPACE: Final[str] = "audit"