"""
Email Notification Module

Provides email notification functionality for:
- Payment confirmation with token
- Job ready notifications

Supports two backends:
1. Resend HTTP API (recommended for cloud/Render — get free key at resend.com)
2. SMTP (for local development or hosts with outbound SMTP access)
"""

import smtplib
import logging
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from typing import Optional

from .config import settings

logger = logging.getLogger(__name__)


def is_configured() -> bool:
    """Check if any email backend is configured."""
    return _has_brevo() or _has_resend() or _has_smtp()


def _has_brevo() -> bool:
    return bool(settings.brevo_api_key)


def _has_resend() -> bool:
    return bool(settings.resend_api_key)


def _has_smtp() -> bool:
    return bool(settings.smtp_host and settings.smtp_user and settings.smtp_password)


def _send_via_brevo(to_email: str, subject: str, html_body: str, text_body: Optional[str] = None) -> bool:
    """Send email via Brevo (Sendinblue) HTTP API."""
    import requests
    
    from_email = settings.sender_email or settings.smtp_from or "onboarding@resend.dev"
    
    try:
        response = requests.post(
            "https://api.brevo.com/v3/smtp/email",
            headers={
                "api-key": settings.brevo_api_key,
                "Content-Type": "application/json",
                "Accept": "application/json"
            },
            json={
                "sender": {"email": from_email, "name": "Upload & Print"},
                "to": [{"email": to_email}],
                "subject": subject,
                "htmlContent": html_body,
                "textContent": text_body or "View this email in HTML mode."
            },
            timeout=15
        )
        
        if response.status_code in (200, 201):
            logger.info(f"Email sent via Brevo to {to_email}")
            return True
        else:
            logger.error(f"Brevo API error {response.status_code}: {response.text}")
            return False
    except Exception as e:
        logger.error(f"Brevo send failed: {type(e).__name__}: {e}")
        return False


def _send_via_resend(to_email: str, subject: str, html_body: str) -> bool:
    """Send email via Resend HTTP API."""
    import requests
    
    from_email = settings.sender_email or settings.smtp_from or "onboarding@resend.dev"
    
    try:
        response = requests.post(
            "https://api.resend.com/emails",
            headers={
                "Authorization": f"Bearer {settings.resend_api_key}",
                "Content-Type": "application/json"
            },
            json={
                "from": from_email,
                "to": [to_email],
                "subject": subject,
                "html": html_body
            },
            timeout=15
        )
        
        if response.status_code in (200, 201):
            logger.info(f"Email sent via Resend to {to_email}")
            return True
        else:
            logger.error(f"Resend API error {response.status_code}: {response.text}")
            return False
    except Exception as e:
        logger.error(f"Resend send failed: {type(e).__name__}: {e}")
        return False


def _send_via_smtp(to_email: str, subject: str, html_body: str, text_body: Optional[str] = None) -> bool:
    """Send email via SMTP."""
    try:
        logger.info(f"Sending email via SMTP to {to_email}")
        
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = settings.smtp_from or settings.smtp_user
        msg["To"] = to_email
        
        if text_body:
            msg.attach(MIMEText(text_body, "plain"))
        msg.attach(MIMEText(html_body, "html"))
        
        if settings.smtp_use_tls:
            server = smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=15)
            server.starttls()
        else:
            server = smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port)
        
        server.login(settings.smtp_user, settings.smtp_password)
        server.sendmail(msg["From"], to_email, msg.as_string())
        server.quit()
        
        logger.info(f"Email sent via SMTP to {to_email}")
        return True
    except Exception as e:
        logger.error(f"SMTP send failed to {to_email}: {type(e).__name__}: {e}")
        return False


def send_email(
    to_email: str,
    subject: str,
    html_body: str,
    text_body: Optional[str] = None
) -> bool:
    """
    Send an email. Tries Brevo, then Resend API, then SMTP, then mock.
    """
    # 0. Try Brevo HTTP API
    if _has_brevo():
        return _send_via_brevo(to_email, subject, html_body, text_body)
        
    # 1. Try Resend HTTP API (works on cloud platforms like Render)
    if _has_resend():
        return _send_via_resend(to_email, subject, html_body)
    
    # 2. Try SMTP (works locally or on hosts with outbound SMTP access)
    if _has_smtp():
        return _send_via_smtp(to_email, subject, html_body, text_body)
    
    # 3. Mock mode — no email backend configured
    logger.warning(f"No email backend configured! Set RESEND_API_KEY or SMTP_HOST/USER/PASSWORD")
    logger.info(f"[MOCK EMAIL] To: {to_email} | Subject: {subject}")
    return True


def send_payment_confirmation(
    email: str,
    token: str,
    estimate_cents: int,
    customer_name: Optional[str] = None,
    order_id: Optional[str] = None,
    payment_id: Optional[str] = None,
    payment_method: Optional[str] = None
) -> bool:
    """
    Send payment confirmation email with token and receipt.
    
    Args:
        email: Customer email
        token: Unique token number
        estimate_cents: Amount paid in cents/paise
        customer_name: Optional customer name
        order_id: Cashfree order ID
        payment_id: Cashfree payment ID
        payment_method: Payment method used (UPI, card, etc.)
    """
    from datetime import datetime, timezone, timedelta
    
    amount = (estimate_cents or 0) / 100
    name = customer_name or "Customer"
    ist = timezone(timedelta(hours=5, minutes=30))
    date_str = datetime.now(ist).strftime("%d %b %Y, %I:%M %p")
    
    # Build receipt rows
    receipt_rows = f"""
                <tr>
                    <td style="padding: 8px 0; color: #666; font-size: 14px;">Amount Paid</td>
                    <td style="padding: 8px 0; text-align: right; font-weight: 600; font-size: 14px;">₹{amount:.2f}</td>
                </tr>
                <tr>
                    <td style="padding: 8px 0; color: #666; font-size: 14px;">Date</td>
                    <td style="padding: 8px 0; text-align: right; font-size: 14px;">{date_str}</td>
                </tr>"""
    
    if order_id:
        receipt_rows += f"""
                <tr>
                    <td style="padding: 8px 0; color: #666; font-size: 14px;">Order ID</td>
                    <td style="padding: 8px 0; text-align: right; font-size: 14px;">{order_id}</td>
                </tr>"""
    
    if payment_id:
        receipt_rows += f"""
                <tr>
                    <td style="padding: 8px 0; color: #666; font-size: 14px;">Payment ID</td>
                    <td style="padding: 8px 0; text-align: right; font-size: 14px;">{payment_id}</td>
                </tr>"""
    
    if payment_method:
        receipt_rows += f"""
                <tr>
                    <td style="padding: 8px 0; color: #666; font-size: 14px;">Payment Method</td>
                    <td style="padding: 8px 0; text-align: right; font-size: 14px;">{payment_method}</td>
                </tr>"""
    
    subject = f"Print Confirmed — Token: {token}"
    
    html_body = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <style>
            body {{ font-family: 'Segoe UI', Arial, sans-serif; background: #f2f2f2; padding: 20px; margin: 0; color: #333; }}
            .container {{ max-width: 520px; margin: 0 auto; background: #ffffff; border-radius: 10px; overflow: hidden; box-shadow: 0 1px 4px rgba(0,0,0,0.08); }}
            .header {{ background: #1a1a1a; color: #ffffff; padding: 24px 30px; }}
            .header h1 {{ margin: 0; font-size: 20px; font-weight: 600; letter-spacing: 0.5px; }}
            .body {{ padding: 30px; }}
            .token-box {{ text-align: center; margin: 24px 0; padding: 24px; background: #f8f8f8; border: 2px solid #e0e0e0; border-radius: 8px; }}
            .token-label {{ font-size: 12px; text-transform: uppercase; letter-spacing: 1.5px; color: #888; margin-bottom: 8px; }}
            .token-value {{ font-size: 36px; font-weight: 700; color: #1a1a1a; letter-spacing: 6px; }}
            .receipt {{ margin: 24px 0; border-top: 1px solid #eee; border-bottom: 1px solid #eee; padding: 8px 0; }}
            .steps {{ margin: 24px 0 0 0; padding: 20px; background: #fafafa; border-radius: 8px; }}
            .steps p {{ margin: 0 0 12px 0; font-size: 14px; font-weight: 600; color: #333; }}
            .steps ol {{ margin: 0; padding-left: 20px; }}
            .steps li {{ font-size: 14px; color: #555; line-height: 1.8; }}
            .footer {{ text-align: center; padding: 20px 30px; font-size: 12px; color: #999; background: #fafafa; border-top: 1px solid #f0f0f0; }}
        </style>
    </head>
    <body>
        <div class="container">
            <div class="header">
                <h1>Print Confirmed</h1>
            </div>
            <div class="body">
                <p style="margin-top: 0;">Hi {name},</p>
                <p>Your payment has been received and your files are being processed. Please save your token number below to collect your printout.</p>
                
                <div class="token-box">
                    <div class="token-label">Your Token Number</div>
                    <div class="token-value">{token}</div>
                </div>
                
                <div class="receipt">
                    <table style="width: 100%; border-collapse: collapse;">
                        {receipt_rows}
                    </table>
                </div>
                
                <div class="steps">
                    <p>How to collect your printout</p>
                    <ol>
                        <li>Visit the xerox shop</li>
                        <li>Tell your token number at the xerox shop</li>
                        <li>Collect your printed documents</li>
                    </ol>
                </div>
            </div>
            <div class="footer">
                Print Press — Thank you for your order.
            </div>
        </div>
    </body>
    </html>
    """
    
    # Build text receipt
    text_receipt = f"Amount Paid: ₹{amount:.2f}\nDate: {date_str}"
    if order_id:
        text_receipt += f"\nOrder ID: {order_id}"
    if payment_id:
        text_receipt += f"\nPayment ID: {payment_id}"
    if payment_method:
        text_receipt += f"\nPayment Method: {payment_method}"
    
    text_body = f"""Print Confirmed

Hi {name},

Your payment has been received and your files are being processed.

TOKEN NUMBER: {token}

{text_receipt}

How to collect your printout:
1. Visit the xerox shop
2. Tell your token number at the xerox shop
3. Collect your printed documents

Print Press — Thank you for your order.
    """
    
    return send_email(email, subject, html_body, text_body)


def send_job_ready(
    email: str,
    token: str,
    customer_name: Optional[str] = None
) -> bool:
    """
    Send notification when job is ready for pickup.
    
    Args:
        email: Customer email
        token: Job token
        customer_name: Optional customer name
    """
    name = customer_name or "Customer"
    
    subject = f"Your Print Job is Ready! - Token: {token}"
    
    html_body = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <style>
            body {{ font-family: Arial, sans-serif; background: #f5f5f5; padding: 20px; }}
            .container {{ max-width: 500px; margin: 0 auto; background: white; border-radius: 12px; padding: 30px; box-shadow: 0 2px 10px rgba(0,0,0,0.1); }}
            .header {{ text-align: center; margin-bottom: 20px; }}
            .token {{ font-size: 32px; font-weight: bold; color: #10b981; text-align: center; padding: 20px; background: #ecfdf5; border-radius: 8px; letter-spacing: 4px; margin: 20px 0; }}
            .footer {{ text-align: center; color: #666; font-size: 12px; margin-top: 30px; }}
        </style>
    </head>
    <body>
        <div class="container">
            <div class="header">
                <h1>✅ Your Print Job is Ready!</h1>
            </div>
            <p>Hi {name},</p>
            <p>Great news! Your documents have been printed and are ready for pickup.</p>
            <div class="token">{token}</div>
            <p style="text-align: center;">Show this token at the counter to collect your documents.</p>
            <div class="footer">
                <p>Thank you for choosing our print service!</p>
            </div>
        </div>
    </body>
    </html>
    """
    
    text_body = f"""
    Your Print Job is Ready!
    
    Hi {name},
    
    Great news! Your documents have been printed and are ready for pickup.
    
    YOUR TOKEN: {token}
    
    Show this token at the counter to collect your documents.
    
    Thank you for choosing our print service!
    """
    
    return send_email(email, subject, html_body, text_body)
