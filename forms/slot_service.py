"""
Service for auto-allocating interaction time slots as per Shreyas Foundation Chart:
- Scheduled Start Date: November 26, 2026 (Open-ended / no fixed end date)
- Capacity: 40 families daily (10 per slot; up to 13 with Admin buffer)
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
INTERACTION_END_DATE = None  # Open-ended: continues automatically across working days (Mon-Sat)

# Official Public / School Holidays (Gujarat / Shreyas Foundation) — skipped during slot allocation
PUBLIC_HOLIDAYS = {
    date(2026, 12, 25): "Christmas (નાતાલ)",
    date(2027, 1, 14): "Makar Sankranti / Uttarayan (ઉત્તરાયણ)",
    date(2027, 1, 15): "Vasi Uttarayan (વાસી ઉત્તરાયણ)",
    date(2027, 1, 26): "Republic Day (પ્રજાસત્તાક દિન)",
    date(2027, 3, 6): "Maha Shivratri (મહા શિવરાત્રી)",
    date(2027, 3, 22): "Holi / Dhuleti (ધૂળેટી)",
    date(2027, 4, 14): "Dr. Ambedkar Jayanti / Ram Navami",
    date(2027, 4, 18): "Mahavir Jayanti",
}


def is_interaction_holiday(check_date: date) -> tuple[bool, str]:
    """
    Returns (True, reason) if check_date is a Sunday or a listed Public/School Holiday.
    Also checks annual fixed-date holidays (25 Dec, 14 Jan, 26 Jan, 15 Aug, 2 Oct).
    """
    if check_date.weekday() == 6:
        return True, "Sunday (રવિવાર)"

    if check_date in PUBLIC_HOLIDAYS:
        return True, PUBLIC_HOLIDAYS[check_date]

    # Fixed annual calendar holidays regardless of year
    fixed_annual = {
        (12, 25): "Christmas (નાતાલ)",
        (1, 14): "Makar Sankranti / Uttarayan (ઉત્તરાયણ)",
        (1, 15): "Vasi Uttarayan (વાસી ઉત્તરાયણ)",
        (1, 26): "Republic Day (પ્રજાસત્તાક દિન)",
        (8, 15): "Independence Day (સ્વાતંત્ર્ય દિન)",
        (10, 2): "Gandhi Jayanti (ગાંધી જયંતિ)",
    }
    if (check_date.month, check_date.day) in fixed_annual:
        return True, fixed_annual[(check_date.month, check_date.day)]

    return False, ""

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
    Finds the earliest available interaction slot starting from November 26, 2026:
    - Starting Date: November 26, 2026 (Working days Mon-Sat, no fixed end date)
    - Default Capacity: 10 children per slot (Auto-allocation strictly caps at 10).
    - Admin buffer: Admin can manually place up to 3 extra children (up to 13 per slot).
    - Sundays are skipped automatically.
    - First-come, first-served allocation on admission form completion.
    """
    current_date = reference_date or INTERACTION_START_DATE
    if current_date < INTERACTION_START_DATE:
        current_date = INTERACTION_START_DATE

    # Fetch all booked slot counts from current_date onwards in a single DB query
    from django.db.models import Count
    qs = AdmissionApplication.objects.filter(
        interaction_date__gte=current_date
    ).exclude(
        admission_status__in=['CANCELLED', 'REJECTED']
    )
    if INTERACTION_END_DATE:
        qs = qs.filter(interaction_date__lte=INTERACTION_END_DATE)

    slot_rows = qs.values('interaction_date', 'interaction_slot_no').annotate(total=Count('id'))
    booked_map = {(r['interaction_date'], r['interaction_slot_no']): r['total'] for r in slot_rows}

    # Search forward day by day starting from Nov 26 (no fixed end date cutoff)
    while INTERACTION_END_DATE is None or current_date <= INTERACTION_END_DATE:
        # Skip Sundays and Official Public/School Holidays (e.g. 25 Dec, 14 Jan, 26 Jan, etc.)
        is_holiday, holiday_name = is_interaction_holiday(current_date)
        if is_holiday:
            logger.debug(f"[SLOT SKIP] Skipping {current_date} ({holiday_name})")
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

    raise RuntimeError("No available interaction slots found.")


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

    # Check if Sunday or Public Holiday
    is_holiday, holiday_name = is_interaction_holiday(chosen_date)
    if is_holiday:
        raise ValueError(
            f"Selected date {chosen_date.strftime('%d-%m-%Y')} is a holiday ({holiday_name}). "
            f"Interaction slots cannot be scheduled on Sundays or public holidays."
        )

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

