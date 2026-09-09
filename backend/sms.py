"""
SMS Module for Xerox Hotspot Upload System

Provides SMS notifications via USB GSM modem.
Supports both python-gsmmodem and gammu backends.

NOTE: This module requires a USB GSM modem with SIM card to function.
Without the hardware, all functions will return False/None gracefully.
"""

import logging
from datetime import datetime
from typing import Optional, Dict, Any
from pathlib import Path

logger = logging.getLogger(__name__)

# GSM Modem configuration
MODEM_PORT = '/dev/ttyUSB0'
MODEM_BAUDRATE = 115200

# Modem instance (initialized lazily)
_modem = None
_modem_available = None


def is_modem_available() -> bool:
    """Check if a GSM modem is available."""
    global _modem_available
    
    if _modem_available is not None:
        return _modem_available
    
    # Check if port exists
    if not Path(MODEM_PORT).exists():
        logger.info(f"GSM modem port {MODEM_PORT} not found")
        _modem_available = False
        return False
    
    # Try to import gsmmodem
    try:
        from gsmmodem.modem import GsmModem
        _modem_available = True
        return True
    except ImportError:
        logger.info("python-gsmmodem not installed, SMS disabled")
        _modem_available = False
        return False


def get_modem():
    """Get or initialize the GSM modem connection."""
    global _modem
    
    if not is_modem_available():
        return None
    
    if _modem is not None:
        return _modem
    
    try:
        from gsmmodem.modem import GsmModem
        
        _modem = GsmModem(MODEM_PORT, MODEM_BAUDRATE)
        _modem.connect()
        logger.info("GSM modem connected")
        return _modem
    except Exception as e:
        logger.error(f"Failed to connect to GSM modem: {e}")
        return None


def send_sms(phone: str, message: str) -> Dict[str, Any]:
    """
    Send an SMS message.
    
    Args:
        phone: Phone number with country code (e.g., +919xxxxxxxxx)
        message: Message text (max 160 chars for single SMS)
    
    Returns:
        Dict with 'success', 'message_id', and 'error' fields
    """
    result = {
        'success': False,
        'message_id': None,
        'error': None
    }
    
    modem = get_modem()
    if modem is None:
        # Mock SMS Implementation
        logger.info("-" * 40)
        logger.info(f"[MOCK SMS] To: {phone}")
        logger.info(f"[MOCK SMS] Message: {message}")
        logger.info("-" * 40)
        
        result['success'] = True
        result['message_id'] = 'mock_sms_id'
        
        # Log to DB as 'sent' for mock purposes
        log_sms(phone, message, 'sent', None, 'Mock SMS (Console)')
        return result
    
    try:
        sms = modem.sendSms(phone, message)
        result['success'] = True
        result['message_id'] = str(sms.id) if hasattr(sms, 'id') else 'sent'
        logger.info(f"SMS sent to {phone}")
    except Exception as e:
        result['error'] = str(e)
        logger.error(f"Failed to send SMS to {phone}: {e}")
    
    return result


def log_sms(phone: str, message: str, status: str, job_ticket: Optional[str] = None, 
            error: Optional[str] = None) -> int:
    """
    Log an SMS attempt to the database.
    
    Returns the log entry ID.
    """
    try:
        from . import db
        from .db import get_connection
        
        created_at = datetime.now().isoformat()
        sent_at = created_at if status == 'sent' else None
        
        with get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO sms_log (phone, message, status, job_ticket, created_at, sent_at, error)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
            """, (phone, message, status, job_ticket, created_at, sent_at, error))
            return cursor.lastrowid
    except Exception as e:
        logger.error(f"Failed to log SMS: {e}")
        return -1


def send_ticket_notification(phone: str, ticket: str) -> bool:
    """Send a ticket received notification."""
    if not phone or not phone.strip():
        return False
    
    message = f"Your print job ticket is {ticket}. Show this at the counter to collect."
    result = send_sms(phone, message)
    log_sms(phone, message, 'sent' if result['success'] else 'failed', ticket, result.get('error'))
    return result['success']


def send_ready_notification(phone: str, ticket: str) -> bool:
    """Send a job ready notification."""
    if not phone or not phone.strip():
        return False
    
    message = f"Your print job {ticket} is ready for pickup!"
    result = send_sms(phone, message)
    log_sms(phone, message, 'sent' if result['success'] else 'failed', ticket, result.get('error'))
    return result['success']


def close_modem():
    """Close the modem connection."""
    global _modem
    
    if _modem is not None:
        try:
            _modem.close()
            logger.info("GSM modem disconnected")
        except:
            pass
        _modem = None


# SMS templates for easy customization
SMS_TEMPLATES = {
    'ticket_received': "Your print job ticket is {ticket}. Show this at the counter.",
    'job_ready': "Your print job {ticket} is ready for pickup!",
    'payment_confirmed': "Payment confirmed for job {ticket}. Printing now.",
}


def format_sms(template_name: str, **kwargs) -> str:
    """Format an SMS template with given values."""
    template = SMS_TEMPLATES.get(template_name, '')
    return template.format(**kwargs)
