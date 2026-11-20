"""
In-process zero-dependency Daily 7:00 AM IST WhatsApp Reminder Runner.
Uses Python's standard library (threading + time + datetime) - NO external packages needed.
Sends 1-time WhatsApp reminder ('shreyas_admission_reminder') to applicants
who have paid fees (payment_status == 'RECEIVED') but have not filled the admission form.
"""
import os
import sys
import time
import logging
import threading
from datetime import datetime, timedelta
import pytz

logger = logging.getLogger(__name__)

_runner_started = False
_runner_lock = threading.Lock()


def send_due_admission_reminders():
    """
    Finds candidates who paid fees but have NOT filled the form yet,
    and sends them a 1-time WhatsApp reminder using 'shreyas_admission_reminder'.
    """
    try:
        from forms.models import QuizSubmission
        from forms.whatsapp import send_whatsapp_template_message
        from django.utils import timezone

        # Filter: Payment RECEIVED, No form filled yet, No reminder sent before
        pending_candidates = list(QuizSubmission.objects.filter(
            payment_status='RECEIVED',
            admission_application__isnull=True,
            reminder_sent_at__isnull=True
        ).order_by('created_at'))

        if not pending_candidates:
            logger.info("[7:00 AM REMINDER JOB] No pending candidates found today.")
            return

        logger.info(f"[7:00 AM REMINDER JOB] Found {len(pending_candidates)} pending candidate(s). Sending WhatsApp reminders...")

        for sub in pending_candidates:
            if not sub.whatsapp_number:
                continue

            res = send_whatsapp_template_message(
                sub.whatsapp_number,
                sub.id,
                template_name="shreyas_admission_reminder_"
            )

            if res.get("success"):
                sub.reminder_sent_at = timezone.now()
                sub.save(update_fields=['reminder_sent_at'])
                logger.info(f"[7:00 AM REMINDER] Sent 1-time reminder to {sub.child_name} ({sub.whatsapp_number})")
            else:
                logger.warning(f"[7:00 AM REMINDER] Failed for {sub.child_name} ({sub.whatsapp_number}): {res.get('error')}")

            time.sleep(1)

        logger.info(f"[7:00 AM REMINDER JOB] Finished processing reminders.")
    except Exception as e:
        logger.error(f"[7:00 AM REMINDER JOB] Error during execution: {e}")


def _scheduler_loop():
    """
    Background worker thread: calculates exact seconds until the next 7:00 AM IST
    and sleeps until then, executes the job, then repeats for the next day.
    """
    ist = pytz.timezone('Asia/Kolkata')
    logger.info("[7:00 AM REMINDER SCHEDULER] Background worker thread is ACTIVE.")

    while True:
        try:
            now_ist = datetime.now(ist)

            # Target next 7:00 AM IST
            target_time = now_ist.replace(hour=7, minute=0, second=0, microsecond=0)
            if now_ist >= target_time:
                target_time += timedelta(days=1)

            seconds_to_wait = (target_time - now_ist).total_seconds()
            logger.info(f"[7:00 AM REMINDER SCHEDULER] Next run scheduled at: {target_time.strftime('%Y-%m-%d %H:%M:%S %Z')} (in {int(seconds_to_wait // 3600)}h {int((seconds_to_wait % 3600) // 60)}m)")

            # Sleep until 7:00 AM IST (in chunks so thread can exit cleanly on server shutdown)
            while seconds_to_wait > 0:
                sleep_chunk = min(seconds_to_wait, 60)
                time.sleep(sleep_chunk)
                seconds_to_wait -= sleep_chunk

            # It is now 7:00 AM IST -> Execute reminder job
            logger.info("[7:00 AM REMINDER SCHEDULER] Triggering 7:00 AM daily job now...")
            send_due_admission_reminders()

            # Small 2-minute safety pause to avoid re-triggering within the same minute
            time.sleep(120)

        except Exception as e:
            logger.error(f"[7:00 AM REMINDER SCHEDULER] Unexpected loop error: {e}")
            time.sleep(60)


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

        t = threading.Thread(target=_scheduler_loop, daemon=True, name="Daily7AMReminderThread")
        t.start()
        _runner_started = True

