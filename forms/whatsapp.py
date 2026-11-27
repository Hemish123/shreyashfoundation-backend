import re
import os
import requests
import urllib.parse
import logging
from django.conf import settings

logger = logging.getLogger(__name__)

# Single WhatsApp CRM SaaS Webhook URL (used directly in code, no .env dependency needed)
WHATSAPP_CRM_WEBHOOK_URL = "https://whatsappcrmsaas-emdke9dnb4f8bne6.centralindia-01.azurewebsites.net/api/webhook/whatsapp/"

# In-memory store for recent WhatsApp message statuses & events (for quick debugging/inspection via API)
WHATSAPP_STATUS_TRACKER = {}


def record_whatsapp_message_status(
    message_id: str,
    status_val: str,
    recipient: str = "",
    template_name: str = "",
    submission_id: int = None,
    timestamp: str = "",
    errors: list = None
):
    if not message_id:
        return
    from django.utils import timezone
    existing = WHATSAPP_STATUS_TRACKER.get(message_id, {})
    history = existing.get("status_history", [])
    if not history or history[-1].get("status") != status_val:
        history.append({
            "status": status_val,
            "updated_at": timezone.now().strftime("%Y-%m-%d %H:%M:%S"),
            "timestamp": timestamp or ""
        })
    WHATSAPP_STATUS_TRACKER[message_id] = {
        "message_id": message_id,
        "current_status": status_val,  # sent | delivered | read | failed
        "recipient": recipient or existing.get("recipient", ""),
        "template_name": template_name or existing.get("template_name", ""),
        "submission_id": submission_id if submission_id is not None else existing.get("submission_id"),
        "errors": errors or existing.get("errors", []),
        "status_history": history,
        "last_updated": timezone.now().strftime("%Y-%m-%d %H:%M:%S")
    }


def trigger_saas_crm_template(phone: str, template_name: str, parameters: dict = None, language: str = "en", submission_id: int = None, message_id: str = None) -> dict:
    """
    Notifies external WhatsApp CRM SaaS webhook to log/display the template.
    If message_id is provided, CRM enters 'LOG_TEMPLATE' mode (no duplicate dispatch to Meta).
    """
    crm_url = WHATSAPP_CRM_WEBHOOK_URL

    clean_phone = normalize_phone_number(phone)
    phone_number_id = str(getattr(settings, 'WHATSAPP_PHONE_NUMBER_ID', '') or os.getenv('WHATSAPP_PHONE_NUMBER_ID', '') or '635086059677025').strip()
    callback_url = "https://shreyashfoundation-e6eagcdeebbbhwgd.centralindia-01.azurewebsites.net/api/whatsapp/webhook/"

    msg_payload = {
        "event": "LOG_TEMPLATE" if message_id else "TRIGGER_TEMPLATE",
        "phone": clean_phone,
        "phone_number_id": phone_number_id,
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
        "phone_number_id": phone_number_id,
        "entry": [
            {
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "metadata": {
                                "phone_number_id": phone_number_id
                            },
                            "messages": [msg_payload]
                        }
                    }
                ]
            }
        ]
    }

    try:
        import threading
        def _send_to_crm():
            try:
                logger.info(f"[SAAS CRM LOG] Logging template '{template_name}' (MsgID: {message_id}) for {clean_phone} via CRM")
                res = requests.post(crm_url, json=payload, headers={"Content-Type": "application/json"}, timeout=8)
                logger.info(f"[SAAS CRM LOG] Response code: {res.status_code}")
                if res.status_code not in [200, 201, 202]:
                    logger.warning(f"[SAAS CRM LOG] CRM responded with code {res.status_code}: {res.text[:200]}")
            except Exception as err:
                logger.error(f"[SAAS CRM LOG] Exception: {err}")

        threading.Thread(target=_send_to_crm, daemon=True).start()
        return {"success": True, "via_crm": True, "queued": True, "recipient": clean_phone}
    except Exception as e:
        logger.error(f"[SAAS CRM LOG] Thread dispatch exception: {e}")
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


def send_whatsapp_template_message(recipient_number: str, submission_id: int, template_name: str = "shreyas_admission_link"):
    """
    Sends WhatsApp message using Meta Approved Template ('shreyas_admission_link' or 'shreyas_admission_reminder_')
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

            if msg_id:
                record_whatsapp_message_status(
                    message_id=msg_id,
                    status_val="sent",
                    recipient=clean_phone,
                    template_name=template_name,
                    submission_id=submission_id
                )

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

            if msg_id:
                record_whatsapp_message_status(
                    message_id=msg_id,
                    status_val="sent",
                    recipient=clean_phone,
                    template_name=template_name
                )

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

    # 2. Admission Form Link Message (shreyas_admission_link) - SENT SECOND
    link_result = send_whatsapp_template_message(clean_phone, submission_id, template_name="shreyas_admission_link")

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

            if msg_id:
                record_whatsapp_message_status(
                    message_id=msg_id,
                    status_val="sent",
                    recipient=clean_phone,
                    template_name=template_name
                )

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


def send_whatsapp_interaction_reminder_message(
    recipient_number: str,
    submission_id: str,
    application_number: str,
    date_str: str,
    slot_no: int,
    time_str: str,
    template_name: str = "shreyas_interaction_reminder__"
) -> dict:
    """
    Sends WhatsApp interaction reminder notification to parents at 8:00 AM IST
    on the day BEFORE their scheduled interaction date.
    Meta Utility Template Parameters (5 Variables):
    {{1}}: Submission ID (e.g. 145)
    {{2}}: Application Number (e.g. SF2026-0001)
    {{3}}: Date (e.g. 26/11/2026)
    {{4}}: Slot Number (e.g. 1)
    {{5}}: Slot Time (e.g. 9:00 AM TO 10:00 AM)
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
                        {"type": "text", "text": str(submission_id)},
                        {"type": "text", "text": str(application_number)},
                        {"type": "text", "text": str(date_str)},
                        {"type": "text", "text": str(slot_no)},
                        {"type": "text", "text": str(time_str)}
                    ]
                }
            ]
        }
    }

    try:
        logger.info(
            f"[INTERACTION REMINDER] Sending template '{template_name}' to {clean_phone} "
            f"(SubID: {submission_id}, AppNo: {application_number}, Date: {date_str}, Slot: {slot_no}, Time: {time_str})..."
        )
        response = requests.post(url, json=payload, headers=headers, timeout=10)
        try:
            res_data = response.json()
        except Exception:
            res_data = response.text

        logger.info(f"Interaction Reminder response for {clean_phone}: status {response.status_code} - {res_data}")

        if response.status_code == 200:
            msg_id = ""
            if isinstance(res_data, dict) and res_data.get("messages"):
                msg_id = res_data["messages"][0].get("id", "")

            if msg_id:
                record_whatsapp_message_status(
                    message_id=msg_id,
                    status_val="sent",
                    recipient=clean_phone,
                    template_name=template_name
                )

            # Log into CRM SaaS asynchronously
            trigger_saas_crm_template(
                phone=clean_phone,
                template_name=template_name,
                parameters={
                    "1": str(submission_id),
                    "2": str(application_number),
                    "3": str(date_str),
                    "4": str(slot_no),
                    "5": str(time_str)
                },
                language="en",
                message_id=msg_id
            )

            return {"success": True, "template": template_name, "data": res_data, "recipient": clean_phone, "message_id": msg_id}
        else:
            logger.error(f"Interaction Reminder failed for {clean_phone}: {res_data}")
            return {"success": False, "template": template_name, "error": res_data, "recipient": clean_phone}
    except Exception as e:
        logger.error(f"Interaction Reminder exception: {str(e)}")
        return {"success": False, "template": template_name, "error": str(e), "recipient": clean_phone}


def send_whatsapp_survey_link_message(recipient_number: str, recipient_name: str = "") -> dict:
    """
    Sends the staff survey link (https://<domain>/survey) via WhatsApp Meta Graph API.
    Uses a Meta template if WHATSAPP_SURVEY_TEMPLATE_NAME is configured in env,
    otherwise sends a direct text message with the survey URL.
    """
    clean_phone = normalize_phone_number(recipient_number)
    phone_number_id = getattr(settings, 'WHATSAPP_PHONE_NUMBER_ID', '')
    access_token = getattr(settings, 'WHATSAPP_ACCESS_TOKEN', '')
    frontend_base = getattr(settings, 'FRONTEND_BASE_URL', 'http://localhost:3000').rstrip('/')
    survey_url = os.getenv('SURVEY_FORM_URL', f"{frontend_base}/survey")
    template_name = os.getenv('WHATSAPP_SURVEY_TEMPLATE_NAME', '').strip()

    if not phone_number_id or not access_token:
        logger.warning("WhatsApp credentials not set in .env for survey invite dispatch")
        return {
            "success": False,
            "error": "WhatsApp credentials not set in .env",
            "recipient": clean_phone,
            "survey_url": survey_url,
        }

    url = f"https://graph.facebook.com/v21.0/{phone_number_id}/messages"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }

    if template_name:
        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": clean_phone,
            "type": "template",
            "template": {
                "name": template_name,
                "language": {"code": "en"},
            },
        }
    else:
        greeting = f"Namaste {recipient_name}," if recipient_name else "Namaste,"
        body_text = (
            f"{greeting}\n\n"
            f"You are invited to complete the anonymous Shreyas Foundation Staff Survey.\n"
            f"Please fill out the form using the link below:\n{survey_url}"
        )
        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": clean_phone,
            "type": "text",
            "text": {
                "preview_url": True,
                "body": body_text,
            },
        }

    try:
        response = requests.post(url, json=payload, headers=headers, timeout=10)
        try:
            res_data = response.json()
        except Exception:
            res_data = response.text

        logger.info(f"Survey WhatsApp dispatch for {clean_phone}: status {response.status_code} - {res_data}")

        if response.status_code == 200:
            msg_id = ""
            if isinstance(res_data, dict) and res_data.get("messages"):
                msg_id = res_data["messages"][0].get("id", "")
            if msg_id:
                record_whatsapp_message_status(
                    message_id=msg_id,
                    status_val="sent",
                    recipient=clean_phone,
                    template_name=template_name or "survey_link_text",
                )
            return {
                "success": True,
                "recipient": clean_phone,
                "message_id": msg_id,
                "survey_url": survey_url,
                "data": res_data,
            }
        else:
            logger.error(f"Survey WhatsApp dispatch failed for {clean_phone}: {res_data}")
            return {
                "success": False,
                "recipient": clean_phone,
                "survey_url": survey_url,
                "error": res_data,
            }
    except Exception as e:
        logger.error(f"Survey WhatsApp dispatch exception: {str(e)}")
        return {
            "success": False,
            "recipient": clean_phone,
            "survey_url": survey_url,
            "error": str(e),
        }


def mask_phone_number(phone_number: str) -> str:
    """
    Masks a phone number for safe display in API responses (e.g. 919876543210 -> ******3210).
    """
    clean = normalize_phone_number(phone_number)
    if len(clean) <= 4:
        return "****"
    return ("*" * (len(clean) - 4)) + clean[-4:]


def send_whatsapp_otp_template(
    recipient_number: str,
    otp_code: str,
    submission_id: int = None,
    template_name: str = None,
) -> dict:
    """
    Sends a 6-digit OTP to the parent's WhatsApp number using a Meta Authentication Template
    (body {{1}} + Copy Code URL button {{1}}).
    Template name defaults to env WHATSAPP_OTP_TEMPLATE_NAME or 'otp'.
    """
    clean_phone = normalize_phone_number(recipient_number)
    phone_number_id = getattr(settings, 'WHATSAPP_PHONE_NUMBER_ID', '')
    access_token = getattr(settings, 'WHATSAPP_ACCESS_TOKEN', '')
    resolved_template = (
        template_name
        or os.getenv('WHATSAPP_OTP_TEMPLATE_NAME', '').strip()
        or 'otp'
    )

    if not phone_number_id or not access_token:
        logger.warning("WhatsApp credentials not set in .env for OTP template dispatch")
        return {
            "success": False,
            "template": resolved_template,
            "error": "WhatsApp credentials not set in .env",
            "recipient": clean_phone,
        }

    url = f"https://graph.facebook.com/v21.0/{phone_number_id}/messages"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }

    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": clean_phone,
        "type": "template",
        "template": {
            "name": resolved_template,
            "language": {
                "code": "en"
            },
            "components": [
                {
                    "type": "body",
                    "parameters": [
                        {
                            "type": "text",
                            "text": str(otp_code)
                        }
                    ]
                },
                {
                    "type": "button",
                    "sub_type": "url",
                    "index": "0",
                    "parameters": [
                        {
                            "type": "text",
                            "text": str(otp_code)
                        }
                    ]
                }
            ]
        }
    }

    try:
        logger.info(f"[ADMISSION OTP] Sending OTP template '{resolved_template}' to {clean_phone} (SubID: {submission_id})...")
        response = requests.post(url, json=payload, headers=headers, timeout=10)
        try:
            res_data = response.json()
        except Exception:
            res_data = response.text

        logger.info(f"WhatsApp OTP Template '{resolved_template}' response for {clean_phone}: status {response.status_code} - {res_data}")

        if response.status_code == 200:
            msg_id = ""
            if isinstance(res_data, dict) and res_data.get("messages"):
                msg_id = res_data["messages"][0].get("id", "")

            if msg_id:
                record_whatsapp_message_status(
                    message_id=msg_id,
                    status_val="sent",
                    recipient=clean_phone,
                    template_name=resolved_template,
                    submission_id=submission_id,
                )

            trigger_saas_crm_template(
                phone=clean_phone,
                template_name=resolved_template,
                submission_id=submission_id,
                parameters={"1": "******"},
                language="en",
                message_id=msg_id,
            )

            return {
                "success": True,
                "template": resolved_template,
                "data": res_data,
                "recipient": clean_phone,
                "message_id": msg_id,
            }
        else:
            logger.error(f"WhatsApp OTP Template '{resolved_template}' failed for {clean_phone}: {res_data}")
            return {
                "success": False,
                "template": resolved_template,
                "error": res_data,
                "recipient": clean_phone,
            }
    except Exception as e:
        logger.error(f"WhatsApp OTP Template exception: {str(e)}")
        return {
            "success": False,
            "template": resolved_template,
            "error": str(e),
            "recipient": clean_phone,
        }




