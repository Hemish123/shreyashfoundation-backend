from django.apps import AppConfig


class FormsConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'forms'

    def ready(self):
        try:
            from .reminder_worker import start_reminder_scheduler
            start_reminder_scheduler()
        except Exception as e:
            import logging
            logging.getLogger(__name__).error(f"Failed to start reminder scheduler: {e}")
