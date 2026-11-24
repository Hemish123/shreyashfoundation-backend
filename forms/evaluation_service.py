"""
Evaluation, Scoring, Merit Ranking and Cutoff Engine
Implements:
- PART X (50 Marks): Parental Screening Quiz Total Score (out of 50: 30 MCQs + 20 Written)
- PART Y (30 Marks): Admin / Educator Assessment from Admin Panel (Direct score out of 30, or Cognitive 5+5, Affective 10, Psychomotor 10)
- REWARD / BONUS MARKS (13 Marks Max):
    * Alak Malak Pre-Toddler Bonus: +5 (6 months) or +10 (12 months) with certificate
    * Sibling in Shreyas Vikas Kendra (VK) Bonus: +3 if sibling opted for VK1/VK2/VK3 with receipt
- Final Aggregate Merit Score: Part X (50) + Part Y (30) + Reward Marks (13) = Total out of 93 Marks
- Top 80 Seats Cutoff (SELECTED) & Automatic Waitlist Rollover on Cancellation
"""
import logging
from typing import Optional
from .models import AdmissionApplication

logger = logging.getLogger(__name__)


def calculate_part_x_score(application: AdmissionApplication, save: bool = True) -> float:
    """
    Calculates PART X Score (Quiz Total out of 50 Marks) & REWARD / BONUS Marks (Alak Malak +10 & Sibling VK +3 = 13 Marks):
    1. PART X (Max 50 Marks):
       - Parental Quiz Total Score out of 50 (from submission.score or re-scored from submission.answers).
    2. REWARD / BONUS MARKS (Max 13 Marks):
       - Alak Malak Pre-toddler program: +5 (6 months) or +10 (12 months) if certificate uploaded.
       - Sibling in Shreyas VK program: +3 marks if sibling opted for VK1/VK2/VK3 AND receipt is present.
    3. Final Aggregate Score:
       - Part X (out of 50) + Part Y (out of 30) + Reward Marks (out of 13) = Total out of 93 Marks.
    """
    # 1. Alak Malak Reward Bonus (0, 5, or 10 marks)
    alak_score = 0.0
    if application.alak_malak_certificate or application.has_attended_alak_malak:
        if application.alak_malak_certificate:
            months = application.alak_malak_months or 6
            if months >= 12:
                alak_score = 10.0
            elif months >= 6:
                alak_score = 5.0
            else:
                alak_score = 5.0
    application.alak_malak_score = alak_score

    # 2. Sibling in Shreyas VK Reward Bonus (+3 marks or 0)
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

    reward_marks = round(min(alak_score + sibling_score, 13.0), 2)

    # 3. PART X: Quiz Total Score (out of 50 Marks)
    quiz_total_50 = 0.0
    if application.submission:
        sub_score = float(application.submission.score or 0.0)
        if sub_score > 0:
            quiz_total_50 = min(sub_score, 50.0)
        elif application.submission.answers:
            from .questions_data import score_parent_questionnaire
            res = score_parent_questionnaire(application.submission.answers, use_ai=False)
            quiz_total_50 = min(float(res.get("total_score", 0.0)), 50.0)

    quiz_total_50 = round(quiz_total_50, 2)
    application.mcq_score_part_x = quiz_total_50
    application.total_part_x_score = quiz_total_50

    # 4. Final Aggregate Score = Part X (50) + Part Y (30) + Reward Marks (13)
    part_y = float(application.total_part_y_score or 0.0)
    application.final_aggregate_score = round(quiz_total_50 + part_y + reward_marks, 2)

    if save:
        application.save(update_fields=[
            'alak_malak_score',
            'mcq_score_part_x',
            'sibling_opted_vk',
            'sibling_score',
            'total_part_x_score',
            'final_aggregate_score'
        ])

    return quiz_total_50


def save_part_y_evaluation(
    application: AdmissionApplication,
    evaluator_user,
    m_tag: str = "",
    mi_tag: str = "",
    cognitive_1: float = 0.0,
    cognitive_2: float = 0.0,
    affective: float = 0.0,
    psychomotor: float = 0.0,
    direct_part_y_score: Optional[float] = None
) -> float:
    """
    Saves PARAMETERS OF ASSESSMENT – PART Y (Total 30 Marks) from Admin Panel:
    Parameters as per Shreyas Assessment Chart:
      - M (evaluator_m): String note / tag
      - MI (evaluator_mi): String note / tag
      - Cognitive Assessment 1: 0 to 5 Marks
      - Cognitive Assessment 2: 0 to 5 Marks
      - Affective Assessment: 0 to 10 Marks
      - Psychomotor Assessment: 0 to 10 Marks
    Total Part Y: 30 Marks Max (5 + 5 + 10 + 10 = 30)

    Strict Validation:
      - Enforces domain bounds and raises ValueError on invalid inputs.
    """
    # Strict bounds validation
    cog1 = float(cognitive_1 or 0.0)
    cog2 = float(cognitive_2 or 0.0)
    aff = float(affective or 0.0)
    psy = float(psychomotor or 0.0)

    if not (0.0 <= cog1 <= 5.0):
        raise ValueError(f"Cognitive Assessment 1 must be between 0 and 5 marks (provided: {cog1})")
    if not (0.0 <= cog2 <= 5.0):
        raise ValueError(f"Cognitive Assessment 2 must be between 0 and 5 marks (provided: {cog2})")
    if not (0.0 <= aff <= 10.0):
        raise ValueError(f"Affective Assessment must be between 0 and 10 marks (provided: {aff})")
    if not (0.0 <= psy <= 10.0):
        raise ValueError(f"Psychomotor Assessment must be between 0 and 10 marks (provided: {psy})")

    if direct_part_y_score is not None:
        direct_val = float(direct_part_y_score)
        if not (0.0 <= direct_val <= 30.0):
            raise ValueError(f"Direct Part Y Score must be between 0 and 30 marks (provided: {direct_val})")
        total_y = round(direct_val, 2)
    else:
        total_y = round(cog1 + cog2 + aff + psy, 2)
        if total_y > 30.0:
            raise ValueError(f"Sum of assessment domains cannot exceed 30 marks (sum: {total_y}/30)")

    application.evaluator = evaluator_user
    if m_tag is not None:
        application.evaluator_m = str(m_tag).strip()
    if mi_tag is not None:
        application.evaluator_mi = str(mi_tag).strip()

    application.cognitive_assessment_1 = round(cog1, 2)
    application.cognitive_assessment_2 = round(cog2, 2)
    application.affective_assessment = round(aff, 2)
    application.psychomotor_assessment = round(psy, 2)
    application.total_part_y_score = total_y

    # Ensure Part X (Quiz out of 50) and Reward Marks (out of 13) are up-to-date
    calculate_part_x_score(application, save=False)

    reward_marks = round((application.alak_malak_score or 0.0) + (application.sibling_score or 0.0), 2)
    application.final_aggregate_score = round((application.total_part_x_score or 0.0) + total_y + reward_marks, 2)

    if application.admission_status in ['SUBMITTED', 'SCHEDULED']:
        application.admission_status = 'EVALUATED'

    application.save()

    # Automatically recompute merit rank & top 80 cutoff for this Standard x Medium
    if application.standard and application.medium:
        recompute_merit_rank_and_cutoff(standard=application.standard, medium=application.medium, cutoff_seats=80)
        application.refresh_from_db(fields=['merit_rank', 'admission_status'])

    logger.info(
        f"[EVALUATION SAVED] App {application.application_number}: Part X (Quiz)={application.total_part_x_score}/50, "
        f"Part Y={total_y}/30, Rewards={reward_marks}/13, Final Total={application.final_aggregate_score}/93, "
        f"Rank=#{application.merit_rank} ({application.admission_status})"
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

