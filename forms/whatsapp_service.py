import urllib.parse
import logging

logger = logging.getLogger(__name__)

def generate_whatsapp_link(phone_number: str, message: str) -> str:
    """
    Generates a WhatsApp click-to-chat URL (wa.me) with a prefilled message.
    Phone number should be formatted with country code (defaults to 91 if 10 digits).
    """
    clean_phone = "".join(filter(str.isdigit, str(phone_number)))
    if len(clean_phone) == 10:
        clean_phone = "91" + clean_phone
    
    encoded_message = urllib.parse.quote(message)
    return f"https://api.whatsapp.com/send?phone={clean_phone}&text={encoded_message}"


def send_whatsapp_message(phone_number: str, message: str, gateway: str = "mock"):
    """
    Placeholder/Helper to send automated WhatsApp message via an API gateway 
    (such as Twilio, Gupshup, Wati, or Meta Cloud API).
    """
    clean_phone = "".join(filter(str.isdigit, str(phone_number)))
    if len(clean_phone) == 10:
        clean_phone = "91" + clean_phone

    # Log the action (can integrate with external API like Twilio/Meta here)
    logger.info(f"Sending WhatsApp message to {clean_phone}: {message}")
    
    # Ready for integration with any webhook/SMS gateway
    return {
        "sent": True,
        "recipient": clean_phone,
        "mode": gateway
    }

