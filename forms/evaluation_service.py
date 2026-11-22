"""
Evaluation, Scoring, Merit Ranking and Cutoff Engine
Implements:
- PART X (20 Marks): Alak Malak (+5/+10), Parent MCQ (+7 max), Sibling VK opted (+3 if receipt attached)
- PART Y (30 Marks): Educator Assessment (M, MI, Cognitive 5+5, Affective 10, Psychomotor 10)
- Total Score: Part X (20) + Part Y (30) = 50 Marks
- First 80 Seats Cutoff & Automatic Waitlist Rollover on Cancellation
"""
import logging
from django.db.models import F
from .models import AdmissionApplication

logger = logging.getLogger(__name__)


def calculate_part_x_score(application: AdmissionApplication, save: bool = True) -> float:
    """
    Calculates PART X Score (Total 20 Marks):
    1. Alak Malak (Pre-toddler program):
       - 1 to 6 months completed (+5)
       - 6 to 12 months completed (+10) [or two 6-month certificates]
    2. Parents MCQ:
       - Up to 7 marks from submission.score
    3. Sibling VK program:
       - +3 marks if sibling opted for VK1 / VK2 / VK3 AND receipt is present.
       - 0 if not opted or no receipt.
    """
    # 1. Alak Malak score
    # Check if certificate uploaded
    alak_score = 0.0
    if application.alak_malak_certificate:
        months = application.alak_malak_months or 6
        if months >= 12:
            alak_score = 10.0
        elif months >= 6:
            alak_score = 5.0
        else:
            alak_score = 5.0  # default base bonus for uploaded certificate
    application.alak_malak_score = alak_score

    # 2. Parent MCQ score (out of 7)
    mcq_score = 0.0
    if application.submission:
        sub_score = float(application.submission.score or 0)
        # If score is from new 50-mark questionnaire (or 30-mark MCQ), map to 7 marks:
        # e.g., (sub_score / 30.0) * 7.0 or if raw MCQ answers exist
        from .questions_data import score_parent_questionnaire
        if application.submission.answers:
            res = score_parent_questionnaire(application.submission.answers)
            mcq_score = float(res.get("mcq_scaled_to_7", 0.0))
        elif sub_score > 7:
            # Map score out of 30 or 50 to 7 marks
            mcq_score = round(min((sub_score / 30.0) * 7.0, 7.0), 2)
        else:
            mcq_score = float(min(sub_score, 7.0))
    application.mcq_score_part_x = mcq_score

    # 3. Sibling VK benefit (+3 marks as per official chart, or 0)
    sibling_score = 0.0
    has_receipt = bool(
        application.sibling_vikas_kendra_1_receipt or
        application.sibling_vikas_kendra_2_receipt
    )
    is_vk_opted = bool(
        str(application.sibling_vikas_kendra_1_opted).lower() in ['yes', 'true', '1'] or
        str(application.sibling_vikas_kendra_2_opted).lower() in ['yes', 'true', '1'] or
        application.sibling_opted_vk
    )

    # Check multiple siblings list if present
    if hasattr(application, 'siblings') and isinstance(application.siblings, list) and len(application.siblings) > 0:
        for sib in application.siblings:
            r1 = sib.get('vk1_receipt') or sib.get('sibling_vikas_kendra_1_receipt')
            r2 = sib.get('vk2_receipt') or sib.get('sibling_vikas_kendra_2_receipt')
            r3 = sib.get('vk3_receipt') or sib.get('sibling_vikas_kendra_3_receipt')
            o1 = bool(sib.get('vk1_opted') or sib.get('sibling_vikas_kendra_1_opted'))
            o2 = bool(sib.get('vk2_opted') or sib.get('sibling_vikas_kendra_2_opted'))
            o3 = bool(sib.get('vk3_opted') or sib.get('sibling_vikas_kendra_3_opted'))

            if (o1 and r1) or (o2 and r2) or (o3 and r3):
                is_vk_opted = True
                has_receipt = True
                break

    if application.has_sibling_in_shreyas and is_vk_opted and has_receipt:
        sibling_score = 3.0
        application.sibling_opted_vk = True
    else:
        sibling_score = 0.0
        application.sibling_opted_vk = False
    application.sibling_score = sibling_score

    total_x = min(alak_score + mcq_score + sibling_score, 20.0)
    application.total_part_x_score = total_x

    # Recalculate aggregate
    application.final_aggregate_score = round(total_x + (application.total_part_y_score or 0.0), 2)

    if save:
        application.save(update_fields=[
            'alak_malak_score',
            'mcq_score_part_x',
            'sibling_opted_vk',
            'sibling_score',
            'total_part_x_score',
            'final_aggregate_score'
        ])

    return total_x


def save_part_y_evaluation(
    application: AdmissionApplication,
    evaluator_user,
    m_tag: str,
    mi_tag: str,
    cognitive_1: float,
    cognitive_2: float,
    affective: float,
    psychomotor: float
) -> float:
    """
    Saves PART Y Assessment (Total 30 Marks):
    - Cognitive Assessment 1: max 5
    - Cognitive Assessment 2: max 5
    - Affective Assessment: max 10
    - Psychomotor Assessment: max 10
    """
    cog1 = min(max(float(cognitive_1 or 0), 0.0), 5.0)
    cog2 = min(max(float(cognitive_2 or 0), 0.0), 5.0)
    aff = min(max(float(affective or 0), 0.0), 10.0)
    psy = min(max(float(psychomotor or 0), 0.0), 10.0)

    total_y = round(cog1 + cog2 + aff + psy, 2)

    application.evaluator = evaluator_user
    application.evaluator_m = m_tag or ""
    application.evaluator_mi = mi_tag or ""
    application.cognitive_assessment_1 = cog1
    application.cognitive_assessment_2 = cog2
    application.affective_assessment = aff
    application.psychomotor_assessment = psy
    application.total_part_y_score = total_y

    # Calculate Part X if not already done
    if not application.total_part_x_score:
        calculate_part_x_score(application, save=False)

    application.final_aggregate_score = round(application.total_part_x_score + total_y, 2)
    if application.admission_status in ['SUBMITTED', 'SCHEDULED']:
        application.admission_status = 'EVALUATED'

    application.save()

    logger.info(
        f"[EVALUATION SAVED] App {application.application_number}: Part X={application.total_part_x_score}/20, "
        f"Part Y={total_y}/30, Total={application.final_aggregate_score}/50"
    )

    return application.final_aggregate_score


def recompute_merit_rank_and_cutoff(standard: str, medium: str, cutoff_seats: int = 80) -> dict:
    """
    Ranks applicants by final_aggregate_score descending for given Standard x Medium.
    - Top 'cutoff_seats' (default 80) become 'SELECTED'.
    - Below cutoff become 'WAITLISTED'.
    - Skips 'CANCELLED' or 'REJECTED' candidates.
    - If a candidate is cancelled, calling this automatically promotes the next waitlisted applicant!
    """
    qs = AdmissionApplication.objects.filter(
        standard__iexact=standard,
        medium__iexact=medium
    ).exclude(admission_status__in=['CANCELLED', 'REJECTED']).order_by('-final_aggregate_score', 'created_at')

    rank = 1
    selected_count = 0
    waitlisted_count = 0
    to_update = []

    for app in qs:
        new_rank = rank
        new_status = app.admission_status
        if rank <= cutoff_seats and (app.total_part_y_score > 0 or app.final_aggregate_score > 0):
            new_status = 'SELECTED'
            selected_count += 1
        elif rank > cutoff_seats:
            new_status = 'WAITLISTED'
            waitlisted_count += 1

        if app.merit_rank != new_rank or app.admission_status != new_status:
            app.merit_rank = new_rank
            app.admission_status = new_status
            to_update.append(app)
        rank += 1

    if to_update:
        AdmissionApplication.objects.bulk_update(to_update, ['merit_rank', 'admission_status'])

    return {
        "standard": standard,
        "medium": medium,
        "total_ranked": rank - 1,
        "selected_seats": selected_count,
        "waitlisted": waitlisted_count,
        "cutoff_seats": cutoff_seats
    }

