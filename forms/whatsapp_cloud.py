import requests
import logging
from django.conf import settings

logger = logging.getLogger(__name__)

def send_whatsapp_cloud_api(recipient_number: str, message_text: str):
    """
    Sends WhatsApp message using Meta Cloud API.
    Recipient format: Country code + Number without '+' (e.g., 919876543210).
    """
    clean_phone = "".join(filter(str.isdigit, str(recipient_number)))
    if len(clean_phone) == 10:
        clean_phone = "91" + clean_phone

    phone_number_id = getattr(settings, 'WHATSAPP_PHONE_NUMBER_ID', '')
    access_token = getattr(settings, 'WHATSAPP_ACCESS_TOKEN', '')

    if not phone_number_id or not access_token or phone_number_id == 'your_phone_number_id_here':
        logger.warning("WhatsApp Cloud API credentials not configured in .env. Falling back to log.")
        return {
            "success": False,
            "error": "WhatsApp credentials not set in .env",
            "recipient": clean_phone
        }

    url = f"https://graph.facebook.com/v21.0/{phone_number_id}/messages"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json"
    }

    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": clean_phone,
        "type": "text",
        "text": {
            "preview_url": True,
            "body": message_text
        }
    }

    try:
        response = requests.post(url, json=payload, headers=headers, timeout=10)
        res_data = response.json()
        logger.info(f"WhatsApp API response for {clean_phone}: {res_data}")

        if response.status_code == 200:
            return {"success": True, "data": res_data, "recipient": clean_phone}
        else:
            return {"success": False, "error": res_data, "recipient": clean_phone}
    except Exception as e:
        logger.error(f"Failed to send WhatsApp message via Meta Cloud API: {str(e)}")
        return {"success": False, "error": str(e), "recipient": clean_phone}

