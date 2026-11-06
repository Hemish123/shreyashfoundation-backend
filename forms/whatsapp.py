import requests
import logging
from django.conf import settings

logger = logging.getLogger(__name__)

def send_whatsapp_template_message(recipient_number: str, submission_id: int):
    """
    Sends WhatsApp message using Meta Approved Template 'shreyas_admission'
    with Dynamic Button URL containing submission_id.
    """
    clean_phone = "".join(filter(str.isdigit, str(recipient_number)))
    if len(clean_phone) == 10:
        clean_phone = "91" + clean_phone

    phone_number_id = getattr(settings, 'WHATSAPP_PHONE_NUMBER_ID', '')
    access_token = getattr(settings, 'WHATSAPP_ACCESS_TOKEN', '')

    if not phone_number_id or not access_token:
        logger.warning("WhatsApp credentials not set in .env")
        return {"success": False, "error": "WhatsApp credentials not set in .env"}

    url = f"https://graph.facebook.com/v21.0/{phone_number_id}/messages"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json"
    }

    # Payload matching your exact template: shreyas_admission
    # Dynamic button parameter: submission_id (e.g. "1")
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": clean_phone,
        "type": "template",
        "template": {
            "name": "shreyas_admission",
            "language": {
                "code": "en"
            },
            "components": [
                {
                    "type": "button",
                    "sub_type": "url",
                    "index": "0",
                    "parameters": [
                        {
                            "type": "text",
                            "text": str(submission_id)
                        }
                    ]
                }
            ]
        }
    }

    try:
        response = requests.post(url, json=payload, headers=headers, timeout=10)
        res_data = response.json()
        logger.info(f"WhatsApp Template response for {clean_phone}: {res_data}")

        if response.status_code == 200:
            return {"success": True, "data": res_data, "recipient": clean_phone}
        else:
            logger.error(f"WhatsApp Template message failed for {clean_phone}: {res_data}")
            return {"success": False, "error": res_data, "recipient": clean_phone}
    except Exception as e:
        logger.error(f"WhatsApp sending exception: {str(e)}")
        return {"success": False, "error": str(e), "recipient": clean_phone}

