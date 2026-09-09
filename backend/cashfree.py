"""
Cashfree Payment Gateway Integration Module

Provides functions for:
- Creating Cashfree orders (with payment_session_id)
- Verifying payment status
- Verifying webhook signatures
- Processing refunds
"""

import base64
import hmac
import hashlib
import logging
import requests
from typing import Optional, Dict, Any

from .config import settings

logger = logging.getLogger(__name__)


def _get_base_url() -> str:
    """Get Cashfree API base URL based on environment."""
    if settings.cashfree_environment.upper() == "PRODUCTION":
        return "https://api.cashfree.com/pg"
    return "https://sandbox.cashfree.com/pg"


def _get_headers() -> Dict[str, str]:
    """Get authentication headers for Cashfree API."""
    return {
        "x-client-id": settings.cashfree_app_id,
        "x-client-secret": settings.cashfree_secret_key,
        "x-api-version": "2023-08-01",
        "Content-Type": "application/json",
        "Accept": "application/json"
    }


def is_configured() -> bool:
    """Check if Cashfree credentials are configured."""
    return bool(settings.cashfree_app_id and settings.cashfree_secret_key)


def create_order(
    order_id: str,
    amount_cents: int,
    currency: str = "INR",
    customer_name: str = "",
    customer_email: str = "",
    customer_phone: str = "",
    return_url: Optional[str] = None,
    notes: Optional[Dict[str, str]] = None
) -> Optional[Dict[str, Any]]:
    """
    Create a Cashfree order.
    
    Args:
        order_id: Unique order ID (use ticket number)
        amount_cents: Amount in smallest currency unit (paise for INR)
        currency: Currency code (default: INR)
        customer_name: Customer's name
        customer_email: Customer's email
        customer_phone: Customer's phone
        return_url: URL to redirect after payment
        notes: Optional notes/tags for the order
    
    Returns:
        Cashfree order object with 'payment_session_id', 'order_id', etc.
        None if creation fails
    """
    if not is_configured():
        logger.error("Cashfree credentials not configured")
        return None
    
    url = f"{_get_base_url()}/orders"
    
    # Convert paise to rupees (Cashfree expects amount in rupees as float)
    amount_rupees = round(amount_cents / 100, 2)
    
    # Sanitize customer_id: Cashfree only allows alphanumeric, underscore, hyphen
    import re
    raw_customer_id = customer_email or order_id
    sanitized_customer_id = re.sub(r'[^a-zA-Z0-9_-]', '_', raw_customer_id)
    
    data = {
        "order_id": order_id,
        "order_amount": amount_rupees,
        "order_currency": currency,
        "customer_details": {
            "customer_id": sanitized_customer_id,
            "customer_name": customer_name or "Customer",
            "customer_email": customer_email or "customer@example.com",
            "customer_phone": customer_phone or "9999999999"
        },
        "order_meta": {
            "notify_url": return_url  # Webhook URL
        }
    }
    
    if notes:
        data["order_tags"] = notes
    
    try:
        response = requests.post(
            url,
            headers=_get_headers(),
            json=data,
            timeout=30
        )
        response.raise_for_status()
        order = response.json()
        logger.info(f"Created Cashfree order: {order.get('order_id')}, session: {order.get('payment_session_id', 'N/A')}")
        return order
    except requests.RequestException as e:
        logger.error(f"Failed to create Cashfree order: {e}")
        if hasattr(e, 'response') and e.response is not None:
            logger.error(f"Response: {e.response.text}")
        return None


def get_order_status(order_id: str) -> Optional[Dict[str, Any]]:
    """
    Get the status of a Cashfree order.
    
    Args:
        order_id: Cashfree order ID
    
    Returns:
        Order object with 'order_status', payment details, etc.
        None if fetch fails
    """
    if not is_configured():
        return None
    
    url = f"{_get_base_url()}/orders/{order_id}"
    
    try:
        response = requests.get(
            url,
            headers=_get_headers(),
            timeout=30
        )
        response.raise_for_status()
        return response.json()
    except requests.RequestException as e:
        logger.error(f"Failed to fetch order {order_id}: {e}")
        return None


def get_payments_for_order(order_id: str) -> Optional[list]:
    """
    Get payments associated with a Cashfree order.
    
    Args:
        order_id: Cashfree order ID
    
    Returns:
        List of payment objects, or None if fetch fails
    """
    if not is_configured():
        return None
    
    url = f"{_get_base_url()}/orders/{order_id}/payments"
    
    try:
        response = requests.get(
            url,
            headers=_get_headers(),
            timeout=30
        )
        response.raise_for_status()
        return response.json()
    except requests.RequestException as e:
        logger.error(f"Failed to fetch payments for order {order_id}: {e}")
        return None


def verify_payment(order_id: str) -> Dict[str, Any]:
    """
    Verify a payment by checking the order status with Cashfree.
    
    Unlike Razorpay which uses signature verification, Cashfree
    recommends server-side order status check for payment verification.
    
    Includes retry logic because Cashfree may return 'ACTIVE' status
    briefly after payment before updating to 'PAID'.
    
    Args:
        order_id: Cashfree order ID
    
    Returns:
        Dict with 'verified' (bool), 'order_status', 'payment_id', etc.
    """
    import time
    
    result = {
        "verified": False,
        "order_status": None,
        "payment_id": None,
        "payment_method": None
    }
    
    # Retry up to 3 times with 2-second delays
    # Cashfree sometimes returns ACTIVE briefly before updating to PAID
    max_retries = 3
    for attempt in range(max_retries):
        order = get_order_status(order_id)
        if not order:
            logger.warning(f"Could not fetch order {order_id} for verification (attempt {attempt + 1})")
            if attempt < max_retries - 1:
                time.sleep(2)
            continue
        
        order_status = order.get("order_status")
        result["order_status"] = order_status
        
        if order_status == "PAID":
            result["verified"] = True
            
            # Fetch actual payment details
            payments = get_payments_for_order(order_id)
            if payments and len(payments) > 0:
                for payment in payments:
                    if payment.get("payment_status") == "SUCCESS":
                        result["payment_id"] = payment.get("cf_payment_id")
                        result["payment_method"] = payment.get("payment_group")
                        break
            
            logger.info(f"Payment verified for order {order_id}: payment_id={result['payment_id']}")
            return result
        
        if order_status == "ACTIVE" and attempt < max_retries - 1:
            # Order still ACTIVE — payment may not have settled yet, retry
            logger.info(f"Order {order_id} still ACTIVE, retrying in 2s (attempt {attempt + 1}/{max_retries})")
            time.sleep(2)
            continue
        
        # Terminal non-PAID status (EXPIRED, CANCELLED, etc.) — don't retry
        if order_status not in ("ACTIVE",):
            break
    
    # Fallback: if order is still ACTIVE, check payments endpoint directly
    # Payment can be SUCCESS even when order status hasn't updated yet
    if result["order_status"] == "ACTIVE":
        logger.info(f"Order {order_id} still ACTIVE after retries, checking payments directly")
        payments = get_payments_for_order(order_id)
        if payments and len(payments) > 0:
            for payment in payments:
                if payment.get("payment_status") == "SUCCESS":
                    result["verified"] = True
                    result["payment_id"] = payment.get("cf_payment_id")
                    result["payment_method"] = payment.get("payment_group")
                    logger.info(f"Payment found via payments API for order {order_id} (order still ACTIVE): payment_id={result['payment_id']}")
                    return result
    
    logger.warning(f"Order {order_id} status is {result['order_status']}, not PAID")
    return result


def verify_webhook_signature(body: bytes, timestamp: str, signature: str) -> bool:
    """
    Verify Cashfree webhook signature.
    
    Per Cashfree docs, webhook signature is computed as:
    Base64Encode(HMAC-SHA256(timestamp + raw_body, clientSecretKey))
    
    Args:
        body: Raw request body as bytes
        timestamp: x-webhook-timestamp header value
        signature: x-webhook-signature header value
    
    Returns:
        True if signature is valid, False otherwise
    """
    # Use webhook secret if set, otherwise fall back to API client secret
    secret = settings.cashfree_webhook_secret or settings.cashfree_secret_key
    if not secret:
        logger.warning("No Cashfree secret configured, skipping webhook verification")
        return True  # Allow in dev mode
    
    # Cashfree signature: Base64(HMAC-SHA256(timestamp + body, secret))
    payload = timestamp.encode() + body
    expected_signature = base64.b64encode(
        hmac.new(
            secret.encode(),
            payload,
            hashlib.sha256
        ).digest()
    ).decode()
    
    return hmac.compare_digest(expected_signature, signature)


def create_refund(
    order_id: str,
    refund_id: str,
    amount_cents: int,
    refund_note: str = "Customer refund"
) -> Optional[Dict[str, Any]]:
    """
    Create a refund for a Cashfree order.
    
    Args:
        order_id: Cashfree order ID
        refund_id: Unique refund ID for your reference
        amount_cents: Amount to refund in paise
        refund_note: Reason for refund
    
    Returns:
        Cashfree refund object or None if failed
    """
    if not is_configured():
        logger.error("Cashfree credentials not configured")
        return None
    
    url = f"{_get_base_url()}/orders/{order_id}/refunds"
    
    # Convert paise to rupees
    amount_rupees = round(amount_cents / 100, 2)
    
    data = {
        "refund_id": refund_id,
        "refund_amount": amount_rupees,
        "refund_note": refund_note
    }
    
    try:
        response = requests.post(
            url,
            headers=_get_headers(),
            json=data,
            timeout=30
        )
        response.raise_for_status()
        refund = response.json()
        logger.info(f"Created refund {refund_id} for order {order_id}")
        return refund
    except requests.RequestException as e:
        logger.error(f"Failed to create refund for order {order_id}: {e}")
        if hasattr(e, 'response') and e.response is not None:
            logger.error(f"Response: {e.response.text}")
        return None
