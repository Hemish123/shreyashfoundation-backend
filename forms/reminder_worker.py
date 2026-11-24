"""
In-process zero-dependency WhatsApp Reminder Runner:
1. Admission Form Pending Reminders (6 Hours & 24 Hours):
   - 1st Reminder: 6 hours after 'shreyas_admission_link' was sent.
   - 2nd Reminder: 24 hours after 'shreyas_admission_link' was sent.
2. Scheduled Interaction Slot Reminder (Previous Day at 8:00 AM IST):
   - Sends 'shreyas_interaction_reminder' on the day before interaction_date.
   - For example, for slots on 26-Nov-2026, reminder goes on 25-Nov-2026 at 8:00 AM IST.
"""
import os
import sys
import time
import logging
import threading
from datetime import timedelta, date, datetime
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")
_runner_started = False
_runner_lock = threading.Lock()
_last_interaction_reminder_date = None  # tracks the calendar date (IST) on which the 8 AM run finished


def send_due_admission_reminders():
    """
    Checks pending candidates who received the admission link (payment_status == 'RECEIVED'
    and admission_application is null) and sends:
      1. 6-Hour Reminder: when elapsed time >= 6 hours (and < 24 hours, or no reminder sent yet)
      2. 24-Hour Reminder: when elapsed time >= 24 hours (and 24h reminder not sent yet)
    Uses existing model fields (created_at, reminder_sent_at) without requiring any schema migration.
    """
    try:
        from forms.models import QuizSubmission
        from forms.whatsapp import send_whatsapp_template_message
        from django.utils import timezone

        now = timezone.now()
        six_hours_ago = now - timedelta(hours=6)
        twenty_four_hours_ago = now - timedelta(hours=24)

        # Fetch all paid enquiries where the admission form is still pending
        # and created at least 6 hours ago
        pending_candidates = list(QuizSubmission.objects.filter(
            payment_status='RECEIVED',
            admission_application__isnull=True,
            created_at__lte=six_hours_ago
        ).order_by('created_at'))

        if not pending_candidates:
            return

        for sub in pending_candidates:
            if not sub.whatsapp_number:
                continue

            elapsed = now - sub.created_at
            reminder_stage = None

            # Case 1: 24+ hours have passed since link was sent
            if sub.created_at <= twenty_four_hours_ago:
                # Send 24h reminder if either no reminder was ever sent,
                # OR only the 6h reminder was sent (meaning reminder_sent_at was before created_at + 23h)
                if sub.reminder_sent_at is None or (sub.reminder_sent_at - sub.created_at) < timedelta(hours=20):
                    reminder_stage = "24-HOUR"

            # Case 2: Between 6 hours and 24 hours have passed
            elif sub.created_at <= six_hours_ago:
                if sub.reminder_sent_at is None:
                    reminder_stage = "6-HOUR"

            if not reminder_stage:
                continue

            logger.info(
                f"[{reminder_stage} REMINDER] Sending 'shreyas_admission_reminder_' to "
                f"{sub.child_name} ({sub.whatsapp_number}, ID: {sub.id}, elapsed: {elapsed})..."
            )

            res = send_whatsapp_template_message(
                sub.whatsapp_number,
                sub.id,
                template_name="shreyas_admission_reminder_"
            )

            if res.get("success"):
                sub.reminder_sent_at = timezone.now()
                sub.save(update_fields=['reminder_sent_at'])
                logger.info(f"[{reminder_stage} REMINDER] Sent successfully to {sub.child_name} ({sub.whatsapp_number})")
            else:
                logger.warning(f"[{reminder_stage} REMINDER] Failed for {sub.child_name} ({sub.whatsapp_number}): {res.get('error')}")

            time.sleep(1)

    except Exception as e:
        logger.error(f"[REMINDER WORKER] Error during execution: {e}")


def send_due_interaction_reminders(target_date: date = None) -> dict:
    """
    Sends WhatsApp reminders ('shreyas_interaction_reminder') to all applicants
    whose interaction is scheduled for tomorrow (target_date or today + 1 day).
    Scheduled to run daily at 8:00 AM IST.
    """
    try:
        from forms.models import AdmissionApplication
        from forms.whatsapp import send_whatsapp_interaction_reminder_message
        from django.utils import timezone

        now_ist = datetime.now(IST)
        tomorrow = target_date if target_date else (now_ist.date() + timedelta(days=1))

        # Query all scheduled applications for tomorrow that have not received the reminder yet
        upcoming_apps = list(AdmissionApplication.objects.filter(
            interaction_date=tomorrow,
            interaction_reminder_sent=False
        ).exclude(
            admission_status__in=['CANCELLED', 'REJECTED']
        ).select_related('submission').order_by('interaction_slot_no', 'id'))

        total_found = len(upcoming_apps)
        logger.info(f"[INTERACTION REMINDER] Checking for interaction_date={tomorrow}. Found {total_found} pending reminder(s).")

        sent_count = 0
        failed_count = 0

        for app in upcoming_apps:
            # Determine best recipient phone number: submission WhatsApp or father's or mother's
            recipient_phone = ""
            if app.submission and app.submission.whatsapp_number:
                recipient_phone = app.submission.whatsapp_number
            elif app.father_whatsapp:
                recipient_phone = app.father_whatsapp
            elif app.mother_whatsapp:
                recipient_phone = app.mother_whatsapp
            elif app.guardian_whatsapp:
                recipient_phone = app.guardian_whatsapp

            if not recipient_phone:
                logger.warning(f"[INTERACTION REMINDER] No phone number found for App ID {app.id} ({app.child_first_name})")
                failed_count += 1
                continue

            child_full_name = f"{app.child_first_name or ''} {app.child_surname or ''}".strip()
            # Format date as DD/MM/YYYY to match Meta template sample (e.g. 26/11/2026)
            date_formatted = app.interaction_date.strftime("%d/%m/%Y") if app.interaction_date else tomorrow.strftime("%d/%m/%Y")
            slot_no = app.interaction_slot_no or 1
            slot_time = app.interaction_time or "9:00 AM TO 10:00 AM"

            sub_id_str = str(app.submission.id) if app.submission else (str(app.submission_id) if app.submission_id else "-")
            app_no_str = str(app.application_number or f"SF_{app.id}")

            res = send_whatsapp_interaction_reminder_message(
                recipient_number=recipient_phone,
                submission_id=sub_id_str,
                application_number=app_no_str,
                date_str=date_formatted,
                slot_no=slot_no,
                time_str=slot_time,
                template_name="shreyas_interaction_reminder__"
            )

            if res.get("success"):
                app.interaction_reminder_sent = True
                app.interaction_reminder_sent_at = timezone.now()
                app.save(update_fields=['interaction_reminder_sent', 'interaction_reminder_sent_at'])
                sent_count += 1
                logger.info(f"[INTERACTION REMINDER] Sent to {child_full_name} ({recipient_phone}) for slot on {date_formatted}")
            else:
                failed_count += 1
                logger.warning(f"[INTERACTION REMINDER] Failed to send to {child_full_name} ({recipient_phone}): {res.get('error')}")

            time.sleep(1)

        return {
            "success": True,
            "target_date": str(tomorrow),
            "total_candidates": total_found,
            "sent_count": sent_count,
            "failed_count": failed_count
        }

    except Exception as e:
        logger.error(f"[INTERACTION REMINDER] Unexpected error: {e}")
        return {"success": False, "error": str(e)}


def _check_and_trigger_daily_interaction_reminder():
    """
    Runs every loop iteration.
    Triggers interaction reminders for tomorrow's scheduled interactions
    daily at 8:00 AM IST.
    """
    global _last_interaction_reminder_date
    now_ist = datetime.now(IST)
    today_date = now_ist.date()

    # Trigger window: at or after 8:00 AM IST (08:00)
    is_trigger_time = (now_ist.hour >= 8)
    if is_trigger_time:
        if _last_interaction_reminder_date != today_date:
            logger.info(f"[INTERACTION REMINDER] It is {now_ist.strftime('%H:%M:%S')} IST on {today_date}. Triggering 8:00 AM reminder check...")
            try:
                res = send_due_interaction_reminders()
                _last_interaction_reminder_date = today_date
                logger.info(f"[INTERACTION REMINDER] Daily 8:00 AM run finished for {today_date}: {res}")
            except Exception as e:
                logger.error(f"[INTERACTION REMINDER] Daily 8:00 AM run failed: {e}")


def _scheduler_loop():
    """
    Background worker thread:
    - Every 5 minutes: checks admission form 6h / 24h reminders.
    - Also checks if 8:00 AM IST has arrived to send tomorrow's interaction reminders.
    """
    logger.info("[REMINDER SCHEDULER] Background worker ACTIVE (Admission 6h/24h + Daily 8:00 AM IST Interaction Reminders).")

    # Initial short wait on server boot before first check
    time.sleep(15)

    while True:
        try:
            # 1. Check admission form completion reminders (6h & 24h)
            send_due_admission_reminders()
        except Exception as e:
            logger.error(f"[REMINDER SCHEDULER] Admission reminders error: {e}")

        try:
            # 2. Check 8:00 AM IST Interaction Reminders for tomorrow
            _check_and_trigger_daily_interaction_reminder()
        except Exception as e:
            logger.error(f"[REMINDER SCHEDULER] Interaction reminder error: {e}")

        # Check every 5 minutes (300 seconds)
        time.sleep(300)


def start_reminder_scheduler():
    """
    Starts the daemon background thread once on Django startup.
    """
    global _runner_started
    with _runner_lock:
        if _runner_started:
            return

        # Don't run during migration/command scripts
        if any(cmd in sys.argv for cmd in ['makemigrations', 'migrate', 'test', 'collectstatic']):
            return

        # Avoid duplicate thread in Django development auto-reloader
        if os.environ.get('RUN_MAIN') != 'true' and 'runserver' in sys.argv:
            return

        t = threading.Thread(target=_scheduler_loop, daemon=True, name="Admission6h24hReminderThread")
        t.start()
        _runner_started = True

