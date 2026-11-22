"""
Service for auto-allocating interaction time slots as per Shreyas Foundation Chart:
- Scheduled Range: November 26 – December 8 (10-11 working days)
- Capacity: 30–40 families daily
- 4 Slots per day (10 children per slot):
  * Slot 1: 9:00 AM TO 10:00 AM
  * Slot 2: 10:00 AM TO 11:00 AM
  * Slot 3: 11:00 AM TO 12:00 PM
  * Slot 4: 12:00 PM TO 1:00 PM
- Schedule Days: Monday to Saturday (Sundays skipped)
- First-come, first-served
"""
import logging
from datetime import date, timedelta
from django.utils import timezone
from .models import AdmissionApplication

logger = logging.getLogger(__name__)

INTERACTION_START_DATE = date(2026, 11, 26)
INTERACTION_END_DATE = date(2026, 12, 8)

STANDARD_SLOT_CAPACITY = 10
ADMIN_OVERRIDE_CAPACITY = 13  # Admin can book up to 3 extra children per slot (total 13)

SLOT_DEFINITIONS = [
    {"slot_no": 1, "time": "9:00 AM TO 10:00 AM", "capacity": STANDARD_SLOT_CAPACITY, "max_capacity": ADMIN_OVERRIDE_CAPACITY},
    {"slot_no": 2, "time": "10:00 AM TO 11:00 AM", "capacity": STANDARD_SLOT_CAPACITY, "max_capacity": ADMIN_OVERRIDE_CAPACITY},
    {"slot_no": 3, "time": "11:00 AM TO 12:00 PM", "capacity": STANDARD_SLOT_CAPACITY, "max_capacity": ADMIN_OVERRIDE_CAPACITY},
    {"slot_no": 4, "time": "12:00 PM TO 1:00 PM", "capacity": STANDARD_SLOT_CAPACITY, "max_capacity": ADMIN_OVERRIDE_CAPACITY},
]


def allocate_next_available_interaction_slot(reference_date=None, allow_admin_buffer: bool = False) -> dict:
    """
    Finds the earliest available interaction slot strictly in the Official Interaction Window:
    - Scheduled Range: November 26, 2026 – December 8, 2026 (Working days Mon-Sat)
    - Default Capacity: 10 children per slot (Auto-allocation strictly caps at 10).
    - Admin buffer: Admin can manually place up to 3 extra children (up to 13 per slot).
    - Sundays are skipped automatically.
    - First-come, first-served allocation on admission form completion.
    """
    current_date = reference_date or INTERACTION_START_DATE

    # Fetch all booked slot counts in a single DB query instead of 44 separate queries
    from django.db.models import Count
    slot_rows = AdmissionApplication.objects.filter(
        interaction_date__gte=current_date,
        interaction_date__lte=(INTERACTION_END_DATE or date(2026, 12, 31))
    ).exclude(
        admission_status__in=['CANCELLED', 'REJECTED']
    ).values('interaction_date', 'interaction_slot_no').annotate(total=Count('id'))
    booked_map = {(r['interaction_date'], r['interaction_slot_no']): r['total'] for r in slot_rows}

    # Search through the interaction window (Nov 26 to Dec 8)
    while current_date <= (INTERACTION_END_DATE or date(2026, 12, 31)):
        # Skip Sundays
        if current_date.weekday() == 6:
            current_date += timedelta(days=1)
            continue

        for slot_def in SLOT_DEFINITIONS:
            slot_no = slot_def["slot_no"]
            slot_time = slot_def["time"]
            capacity = slot_def["max_capacity"] if allow_admin_buffer else slot_def["capacity"]

            booked_count = booked_map.get((current_date, slot_no), 0)

            if booked_count < capacity:
                logger.info(
                    f"[SLOT ALLOCATED] Date: {current_date}, Slot: {slot_no} ({slot_time}), "
                    f"Position: {booked_count + 1}/{capacity}"
                )
                return {
                    "date": current_date,
                    "date_str": current_date.strftime("%d-%m-%Y"),
                    "slot_no": slot_no,
                    "time": slot_time,
                    "venue": "Shreyas Foundation – Main Office",
                    "position": booked_count + 1,
                    "capacity": capacity
                }

        # All 4 slots on current_date are full -> move to next working day
        current_date += timedelta(days=1)

    raise RuntimeError("All interaction slots for November 26 – December 8 are full.")


def validate_and_assign_admin_slot(application: AdmissionApplication, chosen_date: date, chosen_slot_no: int) -> dict:
    """
    Allows Admin to manually choose and assign a specific date & slot for an application:
    - Allows normal booking up to 10.
    - Allows Admin to add up to 3 extra children (maximum 13 per slot).
    - Rejects if slot already has 13 children.
    """
    slot_no = int(chosen_slot_no)
    slot_match = next((s for s in SLOT_DEFINITIONS if s["slot_no"] == slot_no), None)
    if not slot_match:
        raise ValueError(f"Invalid slot_no {slot_no}. Must be between 1 and 4.")

    # Check if Sunday
    if chosen_date.weekday() == 6:
        raise ValueError(f"Selected date {chosen_date.strftime('%d-%m-%Y')} is a Sunday. Slots cannot be scheduled on Sundays.")

    # Count current bookings excluding this application itself (if updating)
    booked_count = AdmissionApplication.objects.filter(
        interaction_date=chosen_date,
        interaction_slot_no=slot_no
    ).exclude(id=application.id).exclude(admission_status__in=['CANCELLED', 'REJECTED']).count()

    max_allowed = ADMIN_OVERRIDE_CAPACITY  # 13

    if booked_count >= max_allowed:
        raise ValueError(
            f"Slot {slot_no} ({slot_match['time']}) on {chosen_date.strftime('%d-%m-%Y')} is completely full. "
            f"Maximum capacity including Admin buffer (13 children) has been reached."
        )

    return {
        "date": chosen_date,
        "date_str": chosen_date.strftime("%d-%m-%Y"),
        "slot_no": slot_no,
        "time": slot_match["time"],
        "venue": "Shreyas Foundation – Main Office",
        "booked_count": booked_count + 1,
        "is_extra_buffer": booked_count >= STANDARD_SLOT_CAPACITY  # True if it's 11th, 12th, or 13th
    }

