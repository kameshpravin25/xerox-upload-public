"""
Razorpay Payment Gateway Integration Module

Provides functions for:
- Creating Razorpay orders
- Verifying payment signatures (HMAC-SHA256)
- Verifying webhook signatures
- Processing refunds
"""

import hmac
import hashlib
import logging
import requests
from typing import Optional, Dict, Any

from .config import settings

logger = logging.getLogger(__name__)


def _get_base_url() -> str:
    """Get Razorpay API base URL."""
    return "https://api.razorpay.com/v1"


def _get_auth() -> tuple:
    """Get HTTP Basic Auth credentials for Razorpay API."""
    return (settings.razorpay_key_id, settings.razorpay_key_secret)


def is_configured() -> bool:
    """Check if Razorpay credentials are configured."""
    return bool(settings.razorpay_key_id and settings.razorpay_key_secret)


def create_order(
    order_id: str,
    amount_cents: int,
    currency: str = "INR",
    notes: Optional[Dict[str, str]] = None,
    receipt: Optional[str] = None
) -> Optional[Dict[str, Any]]:
    """
    Create a Razorpay order.

    Args:
        order_id: Internal order ID (stored in 'receipt' field for reference)
        amount_cents: Amount in smallest currency unit (paise for INR)
        currency: Currency code (default: INR)
        notes: Optional key-value notes for the order
        receipt: Optional receipt ID (defaults to order_id)

    Returns:
        Razorpay order object with 'id' (razorpay_order_id), 'amount', etc.
        None if creation fails
    """
    if not is_configured():
        logger.error("Razorpay credentials not configured")
        return None

    url = f"{_get_base_url()}/orders"

    data = {
        "amount": amount_cents,  # Razorpay expects amount in paise
        "currency": currency,
        "receipt": receipt or order_id,
    }

    if notes:
        data["notes"] = notes

    try:
        response = requests.post(
            url,
            auth=_get_auth(),
            json=data,
            timeout=30
        )
        response.raise_for_status()
        order = response.json()
        logger.info(f"Created Razorpay order: {order.get('id')}, receipt: {order.get('receipt')}")
        return order
    except requests.RequestException as e:
        logger.error(f"Failed to create Razorpay order: {e}")
        if hasattr(e, 'response') and e.response is not None:
            logger.error(f"Response: {e.response.text}")
        return None


def verify_payment_signature(
    razorpay_order_id: str,
    razorpay_payment_id: str,
    razorpay_signature: str
) -> bool:
    """
    Verify the payment signature returned by Razorpay Checkout.

    Razorpay generates a signature using HMAC-SHA256:
    signature = HMAC-SHA256(razorpay_order_id + "|" + razorpay_payment_id, key_secret)

    Args:
        razorpay_order_id: Razorpay order ID (starts with 'order_')
        razorpay_payment_id: Razorpay payment ID (starts with 'pay_')
        razorpay_signature: Signature from checkout response

    Returns:
        True if signature is valid, False otherwise
    """
    if not settings.razorpay_key_secret:
        logger.error("SECURITY: No Razorpay key secret configured — signature verification SKIPPED. "
                     "This MUST NOT happen in production. Set RAZORPAY_KEY_SECRET in .env")
        return True  # Allow in dev mode only

    message = f"{razorpay_order_id}|{razorpay_payment_id}"
    expected_signature = hmac.new(
        settings.razorpay_key_secret.encode(),
        message.encode(),
        hashlib.sha256
    ).hexdigest()

    is_valid = hmac.compare_digest(expected_signature, razorpay_signature)
    if is_valid:
        logger.info(f"Payment signature verified: order={razorpay_order_id}, payment={razorpay_payment_id}")
    else:
        logger.warning(f"Payment signature INVALID: order={razorpay_order_id}")

    return is_valid


def verify_webhook_signature(body: bytes, signature: str) -> bool:
    """
    Verify Razorpay webhook signature.

    Razorpay webhook signature is computed as:
    HMAC-SHA256(raw_body, webhook_secret)

    Args:
        body: Raw request body as bytes
        signature: X-Razorpay-Signature header value

    Returns:
        True if signature is valid, False otherwise
    """
    secret = settings.razorpay_webhook_secret
    if not secret:
        logger.error("SECURITY: No Razorpay webhook secret configured — webhook verification SKIPPED. "
                     "This MUST NOT happen in production. Set RAZORPAY_WEBHOOK_SECRET in .env")
        return True  # Allow in dev mode only

    expected_signature = hmac.new(
        secret.encode(),
        body,
        hashlib.sha256
    ).hexdigest()

    return hmac.compare_digest(expected_signature, signature)


def get_payment_details(payment_id: str) -> Optional[Dict[str, Any]]:
    """
    Fetch payment details from Razorpay.

    Args:
        payment_id: Razorpay payment ID (e.g., 'pay_xxx')

    Returns:
        Payment object with status, method, etc. or None if fetch fails
    """
    if not is_configured():
        return None

    url = f"{_get_base_url()}/payments/{payment_id}"

    try:
        response = requests.get(
            url,
            auth=_get_auth(),
            timeout=30
        )
        response.raise_for_status()
        return response.json()
    except requests.RequestException as e:
        logger.error(f"Failed to fetch payment {payment_id}: {e}")
        return None


def create_refund(
    payment_id: str,
    amount_cents: int,
    refund_note: str = "Customer refund",
    receipt: Optional[str] = None
) -> Optional[Dict[str, Any]]:
    """
    Create a refund for a Razorpay payment.

    Args:
        payment_id: Razorpay payment ID to refund (e.g., 'pay_xxx')
        amount_cents: Amount to refund in paise
        refund_note: Reason for refund
        receipt: Optional unique refund receipt ID

    Returns:
        Razorpay refund object or None if failed
    """
    if not is_configured():
        logger.error("Razorpay credentials not configured")
        return None

    url = f"{_get_base_url()}/payments/{payment_id}/refund"

    data = {
        "amount": amount_cents,
        "notes": {
            "reason": refund_note
        }
    }

    if receipt:
        data["receipt"] = receipt

    try:
        response = requests.post(
            url,
            auth=_get_auth(),
            json=data,
            timeout=30
        )
        response.raise_for_status()
        refund = response.json()
        logger.info(f"Created refund for payment {payment_id}: refund_id={refund.get('id')}")
        return refund
    except requests.RequestException as e:
        logger.error(f"Failed to create refund for payment {payment_id}: {e}")
        if hasattr(e, 'response') and e.response is not None:
            logger.error(f"Response: {e.response.text}")
        return None


def get_order_details(order_id: str) -> Optional[Dict[str, Any]]:
    """
    Fetch order details from Razorpay.
    
    Used as a webhook fallback to retrieve the 'receipt' field
    (which contains our internal order_id).
    
    Args:
        order_id: Razorpay order ID (e.g., 'order_xxx')
    
    Returns:
        Order object with receipt, amount, status, etc. or None if fetch fails
    """
    if not is_configured():
        return None

    url = f"{_get_base_url()}/orders/{order_id}"

    try:
        response = requests.get(
            url,
            auth=_get_auth(),
            timeout=30
        )
        response.raise_for_status()
        return response.json()
    except requests.RequestException as e:
        logger.error(f"Failed to fetch order {order_id}: {e}")
        return None

