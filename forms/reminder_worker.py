"""
In-process zero-dependency WhatsApp Reminder Runner (6 Hours & 24 Hours).
Uses Python's standard library (threading + time + datetime) - NO external packages needed.
Sends WhatsApp reminders ('shreyas_admission_reminder_') to applicants
who have paid fees (payment_status == 'RECEIVED') and received 'shreyas_admission_link',
but have NOT filled the admission form yet:
  - 1st Reminder: 6 hours after 'shreyas_admission_link' was sent.
  - 2nd Reminder: 24 hours after 'shreyas_admission_link' was sent.
"""
import os
import sys
import time
import logging
import threading
from datetime import timedelta

logger = logging.getLogger(__name__)

_runner_started = False
_runner_lock = threading.Lock()


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


def _scheduler_loop():
    """
    Background worker thread: checks every 5 minutes for any pending admission forms
    that have crossed the 6-hour or 24-hour mark since receiving 'shreyas_admission_link'.
    """
    logger.info("[REMINDER SCHEDULER] 6-Hour & 24-Hour Reminder background worker is ACTIVE.")

    # Initial short wait on server boot before first check
    time.sleep(15)

    while True:
        try:
            send_due_admission_reminders()
        except Exception as e:
            logger.error(f"[REMINDER SCHEDULER] Unexpected loop error: {e}")

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

