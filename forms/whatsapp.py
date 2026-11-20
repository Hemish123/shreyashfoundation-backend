import re
import os
import requests
import urllib.parse
import logging
from django.conf import settings

logger = logging.getLogger(__name__)


def trigger_saas_crm_template(phone: str, template_name: str, parameters: dict = None, language: str = "en", submission_id: int = None, message_id: str = None) -> dict:
    """
    Notifies external WhatsApp CRM SaaS webhook to log/display the template.
    If message_id is provided, CRM enters 'LOG_TEMPLATE' mode (no duplicate dispatch to Meta).
    """
    crm_url = os.getenv("WHATSAPP_CRM_TRIGGER_URL", "").strip()
    if not crm_url:
        return {"success": False, "error": "WHATSAPP_CRM_TRIGGER_URL not configured"}

    clean_phone = normalize_phone_number(phone)
    phone_number_id = getattr(settings, 'WHATSAPP_PHONE_NUMBER_ID', '1481244255068012')
    callback_url = "https://shreyashfoundation-e6eagcdeebbbhwgd.centralindia-01.azurewebsites.net/api/whatsapp/webhook/"

    msg_payload = {
        "event": "LOG_TEMPLATE" if message_id else "TRIGGER_TEMPLATE",
        "phone": clean_phone,
        "template_name": template_name,
        "language": language or "en",
        "parameters": parameters or {},
        "submission_id": submission_id,
        "webhook_callback_url": callback_url
    }
    if message_id:
        msg_payload["message_id"] = message_id
        msg_payload["log_only"] = True

    payload = {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "metadata": {
                                "phone_number_id": str(phone_number_id)
                            },
                            "messages": [msg_payload]
                        }
                    }
                ]
            }
        ]
    }

    try:
        logger.info(f"[SAAS CRM LOG] Logging template '{template_name}' (MsgID: {message_id}) for {clean_phone} via CRM")
        res = requests.post(crm_url, json=payload, headers={"Content-Type": "application/json"}, timeout=10)
        logger.info(f"[SAAS CRM LOG] Response code: {res.status_code}")
        if res.status_code in [200, 201, 202]:
            try:
                res_json = res.json()
            except Exception:
                res_json = {"raw": res.text}
            return {"success": True, "via_crm": True, "data": res_json, "recipient": clean_phone}
        else:
            logger.warning(f"[SAAS CRM LOG] CRM responded with code {res.status_code}: {res.text[:200]}")
            return {"success": False, "error": f"CRM responded with status {res.status_code}"}
    except Exception as e:
        logger.error(f"[SAAS CRM LOG] Exception: {e}")
        return {"success": False, "error": str(e)}


def normalize_phone_number(phone_number: str) -> str:
    """
    Standardizes any phone number into Meta WhatsApp format (e.g. 919876543210).
    Robustly handles:
    - 9876543210 -> 919876543210
    - +91 98765-43210 -> 919876543210
    - 09876543210 -> 919876543210
    - 919876543210 -> 919876543210
    - +91919876543210 -> 919876543210
    """
    if not phone_number:
        return ""
    clean = re.sub(r"\D", "", str(phone_number))

    # Strip repeated leading 91 prefixes if user mistakenly entered it multiple times
    while clean.startswith("91") and len(clean) > 12:
        clean = clean[2:]

    # Strip leading 0 if 11 digits (e.g. 09876543210)
    if len(clean) == 11 and clean.startswith("0"):
        clean = clean[1:]

    # Prefix 91 if standard 10 digit Indian number
    if len(clean) == 10:
        return "91" + clean

    return clean


def generate_whatsapp_link(phone_number: str, message: str) -> str:
    """
    Generates a WhatsApp click-to-chat URL (wa.me) with a prefilled message.
    """
    clean_phone = normalize_phone_number(phone_number)
    encoded_message = urllib.parse.quote(message)
    return f"https://api.whatsapp.com/send?phone={clean_phone}&text={encoded_message}"


def send_whatsapp_message(phone_number: str, message: str, gateway: str = "mock"):
    """
    Helper to send plain WhatsApp text message via an API gateway / webhook.
    """
    clean_phone = normalize_phone_number(phone_number)
    logger.info(f"Sending WhatsApp message to {clean_phone}: {message}")
    return {
        "sent": True,
        "recipient": clean_phone,
        "mode": gateway
    }


def send_whatsapp_template_message(recipient_number: str, submission_id: int, template_name: str = "shreyas_admission__"):
    """
    Sends WhatsApp message using Meta Approved Template ('shreyas_admission__' or 'shreyas_admission_reminder_')
    with Dynamic Button URL containing submission_id.
    Sends directly to Meta first, then logs in CRM SaaS (no duplicate!).
    """
    clean_phone = normalize_phone_number(recipient_number)

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

    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": clean_phone,
        "type": "template",
        "template": {
            "name": template_name,
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
        try:
            res_data = response.json()
        except Exception:
            res_data = response.text

        logger.info(f"WhatsApp Template '{template_name}' response for {clean_phone}: status {response.status_code} - {res_data}")

        if response.status_code == 200:
            # Extract Meta Message ID (wamid)
            msg_id = ""
            if isinstance(res_data, dict) and res_data.get("messages"):
                msg_id = res_data["messages"][0].get("id", "")

            # Log into CRM asynchronously so it shows in CRM without duplicate sending
            trigger_saas_crm_template(
                phone=clean_phone,
                template_name=template_name,
                submission_id=submission_id,
                parameters={"1": str(submission_id)},
                language="en",
                message_id=msg_id
            )

            return {"success": True, "template": template_name, "data": res_data, "recipient": clean_phone, "message_id": msg_id}
        else:
            logger.error(f"WhatsApp Template '{template_name}' failed for {clean_phone}: {res_data}")
            return {"success": False, "template": template_name, "error": res_data, "recipient": clean_phone}
    except Exception as e:
        logger.error(f"WhatsApp sending exception: {str(e)}")
        return {"success": False, "template": template_name, "error": str(e), "recipient": clean_phone}


def send_whatsapp_booklet_template(recipient_number: str, language: str = "gu", pdf_url: str = None) -> dict:
    """
    Sends WhatsApp message with school Booklet PDF attachment via Meta Approved Template:
    - English: 'shreyas_booklet_en'
    - Gujarati: 'shreyas_booklet_gu'
    Header: Document PDF attachment.
    Sends directly to Meta first, then logs in CRM SaaS (no duplicate!).
    """
    clean_phone = normalize_phone_number(recipient_number)
    phone_number_id = getattr(settings, 'WHATSAPP_PHONE_NUMBER_ID', '')
    access_token = getattr(settings, 'WHATSAPP_ACCESS_TOKEN', '')

    if not phone_number_id or not access_token:
        logger.warning("WhatsApp credentials not set in .env")
        return {"success": False, "error": "WhatsApp credentials not set in .env"}

    is_gujarati = str(language or '').lower().startswith('gu')
    template_name = "shreyas_booklet_gu_" if is_gujarati else "shreyas_booklet_en_"

    # Load booklet PDF url directly from .env (with fallback)
    if not pdf_url:
        if is_gujarati:
            pdf_url = os.getenv(
                'BOOKLET_PDF_URL_GU',
                'https://shreyasfoundation.blob.core.windows.net/media/documents/health_certificates/04_-_Shreyas_Booklet_Gujarati_-_Final_28.10.2026.pdf'
            )
        else:
            pdf_url = os.getenv(
                'BOOKLET_PDF_URL_EN',
                'https://shreyasfoundation.blob.core.windows.net/media/documents/caste_certificates/02_-_Shreyas_Booklet_English_-_Final_28.10.2026.pdf'
            )

    file_name = "Shreyas_Booklet_Gujarati.pdf" if is_gujarati else "Shreyas_Booklet_English.pdf"

    url = f"https://graph.facebook.com/v21.0/{phone_number_id}/messages"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json"
    }

    # Meta Document Template Payload
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": clean_phone,
        "type": "template",
        "template": {
            "name": template_name,
            "language": {
                "code": "en"  # In Meta templates screenshot, Language is selected as English
            },
            "components": [
                {
                    "type": "header",
                    "parameters": [
                        {
                            "type": "document",
                            "document": {
                                "link": pdf_url,
                                "filename": file_name
                            }
                        }
                    ]
                }
            ]
        }
    }

    try:
        logger.info(f"Sending WhatsApp Booklet Template '{template_name}' with PDF '{file_name}' to {clean_phone}...")
        response = requests.post(url, json=payload, headers=headers, timeout=10)
        try:
            res_data = response.json()
        except Exception:
            res_data = response.text

        logger.info(f"WhatsApp Booklet Template response for {clean_phone}: status {response.status_code} - {res_data}")

        if response.status_code == 200:
            msg_id = ""
            if isinstance(res_data, dict) and res_data.get("messages"):
                msg_id = res_data["messages"][0].get("id", "")

            # Log into CRM asynchronously
            trigger_saas_crm_template(
                phone=clean_phone,
                template_name=template_name,
                parameters={
                    "document_url": pdf_url,
                    "filename": file_name
                },
                language="en",
                message_id=msg_id
            )

            return {"success": True, "template": template_name, "data": res_data, "recipient": clean_phone, "message_id": msg_id}
        else:
            logger.error(f"WhatsApp Booklet Template failed for {clean_phone}: {res_data}")
            return {"success": False, "template": template_name, "error": res_data, "recipient": clean_phone}
    except Exception as e:
        logger.error(f"WhatsApp Booklet Template exception: {str(e)}")
        return {"success": False, "template": template_name, "error": str(e), "recipient": clean_phone}


def send_payment_received_whatsapp_notifications(recipient_number: str, submission_id: int, language: str = "gu") -> dict:
    """
    Sends BOTH WhatsApp messages upon payment receipt:
    1. School Booklet PDF Template FIRST ('shreyas_booklet_gu' or 'shreyas_booklet_en' based on chosen language).
    2. Admission Form Link Template SECOND ('shreyas_admission') with dynamic button link.
    """
    clean_phone = normalize_phone_number(recipient_number)
    logger.info(f"[WHATSAPP DISPATCH] Triggering Booklet PDF first, followed by Form Link for {clean_phone} (Language: {language}, ID: {submission_id})...")

    # 1. School Booklet PDF Message (shreyas_booklet_gu / shreyas_booklet_en) - SENT FIRST
    booklet_result = send_whatsapp_booklet_template(clean_phone, language=language)

    # Slight pause to ensure Meta orders and delivers the Booklet message before the Link message
    try:
        import time
        time.sleep(1.5)
    except Exception:
        pass

    # 2. Admission Form Link Message (shreyas_admission__) - SENT SECOND
    link_result = send_whatsapp_template_message(clean_phone, submission_id, template_name="shreyas_admission__")

    return {
        "booklet_sent": booklet_result.get("success", False),
        "link_sent": link_result.get("success", False),
        "booklet_result": booklet_result,
        "link_result": link_result,
        "overall_success": bool(link_result.get("success") or booklet_result.get("success"))
    }


def send_whatsapp_interaction_scheduled_message(
    recipient_number: str,
    date_str: str,
    slot_no: int,
    time_str: str,
    template_name: str = "shreyas_interaction_scheduled_"
) -> dict:
    """
    Sends WhatsApp notification to parents upon Admission Form submission
    with their scheduled Interaction Date, Slot No, and Time.
    Sends directly to Meta first, then logs in CRM SaaS (no duplicate!).
    """
    clean_phone = normalize_phone_number(recipient_number)
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

    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": clean_phone,
        "type": "template",
        "template": {
            "name": template_name,
            "language": {
                "code": "en"
            },
            "components": [
                {
                    "type": "body",
                    "parameters": [
                        {"type": "text", "text": str(date_str)},
                        {"type": "text", "text": str(slot_no)},
                        {"type": "text", "text": str(time_str)}
                    ]
                }
            ]
        }
    }

    try:
        logger.info(f"[INTERACTION NOTIF] Sending template '{template_name}' to {clean_phone} (Date: {date_str}, Slot: {slot_no}, Time: {time_str})...")
        response = requests.post(url, json=payload, headers=headers, timeout=10)
        try:
            res_data = response.json()
        except Exception:
            res_data = response.text

        logger.info(f"Interaction WhatsApp response for {clean_phone}: status {response.status_code} - {res_data}")

        if response.status_code == 200:
            msg_id = ""
            if isinstance(res_data, dict) and res_data.get("messages"):
                msg_id = res_data["messages"][0].get("id", "")

            # Log into CRM asynchronously
            trigger_saas_crm_template(
                phone=clean_phone,
                template_name=template_name,
                parameters={
                    "1": str(date_str),
                    "2": str(slot_no),
                    "3": str(time_str)
                },
                language="en",
                message_id=msg_id
            )

            return {"success": True, "template": template_name, "data": res_data, "recipient": clean_phone, "message_id": msg_id}
        else:
            logger.error(f"Interaction WhatsApp message failed for {clean_phone}: {res_data}")
            return {"success": False, "template": template_name, "error": res_data, "recipient": clean_phone}
    except Exception as e:
        logger.error(f"Interaction WhatsApp exception: {str(e)}")
        return {"success": False, "template": template_name, "error": str(e), "recipient": clean_phone}


