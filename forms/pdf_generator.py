import io
import os
import re
import logging
import requests
from PIL import Image
import PyPDF2

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, Image as RLImage, PageBreak
)
from reportlab.pdfgen import canvas

logger = logging.getLogger(__name__)

PRIMARY_GREEN = colors.HexColor("#0f3d2e")
HEADER_BG = colors.HexColor("#1b4d3e")
ACCENT_MAROON = colors.HexColor("#8b1e28")
LIGHT_BG = colors.HexColor("#f8fafc")
BORDER_COLOR = colors.HexColor("#cbd5e1")
TEXT_DARK = colors.HexColor("#0f172a")
SHREYAS_LOGO_URL = "https://shreyasfoundation.blob.core.windows.net/media/shreyas_logo.png"


def esc(val, default="-"):
    if val is None or str(val).strip() in ["", "None", "null"]:
        return default
    return str(val).strip()


class NumberedCanvas(canvas.Canvas):
    """Adds a clean header rule and footer with page number to each generated ReportLab page."""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._saved_page_states = []

    def showPage(self):
        self._saved_page_states.append(dict(self.__dict__))
        self._startPage()

    def save(self):
        num_pages = len(self._saved_page_states)
        for state in self._saved_page_states:
            self.__dict__.update(state)
            self.draw_page_decorations(num_pages)
            super().showPage()
        super().save()

    def draw_page_decorations(self, page_count):
        self.saveState()
        self.setFont("Helvetica", 7.5)
        self.setFillColor(colors.HexColor("#64748b"))
        self.setStrokeColor(PRIMARY_GREEN)
        self.setLineWidth(0.8)
        self.line(22, 22, A4[0] - 22, 22)
        self.drawString(22, 12, "Shreyas Foundation — Official Admission Application & Parental Quiz Record")
        self.drawRightString(A4[0] - 22, 12, f"Page {self._pageNumber} of {page_count}")
        self.restoreState()


def _fetch_file_bytes(file_field_or_url):
    """Download image or PDF file bytes from Azure Blob URL or FieldFile."""
    if not file_field_or_url:
        return None
    try:
        if hasattr(file_field_or_url, 'read'):
            try:
                file_field_or_url.open('rb')
                content = file_field_or_url.read()
                file_field_or_url.close()
                return content
            except Exception:
                pass

        url = getattr(file_field_or_url, 'url', None) or str(file_field_or_url)
        if str(url).startswith('http://') or str(url).startswith('https://'):
            res = requests.get(url, timeout=12)
            if res.status_code == 200:
                return res.content
        elif os.path.isfile(url):
            with open(url, 'rb') as f:
                return f.read()
    except Exception as e:
        logger.warning(f"Failed to fetch file bytes for {file_field_or_url}: {e}")
    return None


def _build_header(logo_bytes, student_photo_bytes, badge_title, academic_session="2026 - 2027"):
    """Build the exact school header with logo, title text, and student photograph box."""
    styles = getSampleStyleSheet()
    trust_style = ParagraphStyle('TrustTitle', parent=styles['Normal'], fontName='Helvetica-Bold', fontSize=12, leading=15, alignment=1, textColor=PRIMARY_GREEN)
    school_style = ParagraphStyle('SchoolTitle', parent=styles['Normal'], fontName='Helvetica-Bold', fontSize=9.5, leading=12, alignment=1, textColor=ACCENT_MAROON)
    sub_style = ParagraphStyle('SubTitle', parent=styles['Normal'], fontName='Helvetica', fontSize=7.5, leading=9.5, alignment=1, textColor=colors.HexColor("#475569"))
    badge_style = ParagraphStyle('BadgeStyle', parent=styles['Normal'], fontName='Helvetica-Bold', fontSize=8, leading=10, alignment=1, textColor=colors.HexColor("#0f3d2e"))
    session_style = ParagraphStyle('SessionStyle', parent=styles['Normal'], fontName='Helvetica-Bold', fontSize=7.5, leading=9.5, alignment=1, textColor=colors.HexColor("#1e293b"))

    # Logo
    logo_flowable = ""
    if logo_bytes:
        try:
            logo_flowable = RLImage(io.BytesIO(logo_bytes), width=48, height=48)
        except Exception:
            logo_flowable = ""

    # Photo Box
    photo_flowable = ""
    if student_photo_bytes:
        try:
            photo_flowable = RLImage(io.BytesIO(student_photo_bytes), width=50, height=60)
        except Exception:
            photo_flowable = Paragraph("PHOTO", sub_style)
    else:
        photo_flowable = Paragraph("<font size=6.5 color='#94a3b8'>AFFIX RECENT<br/>PASSPORT PHOTO<br/>(35mm x 45mm)</font>", sub_style)

    header_text = [
        Paragraph("SHREYAS FOUNDATION", trust_style),
        Paragraph("VIKAS KENDRA", school_style),
        Paragraph("Post Box No. 4070, Near Manekbaug Hall, Ambawadi, Ahmedabad – 380015", sub_style),
        Spacer(1, 2),
        Paragraph(f"<b>{badge_title.upper()}</b>", badge_style),
        Paragraph(f"ACADEMIC SESSION: {academic_session}", session_style),
    ]

    header_table = Table([[logo_flowable, header_text, photo_flowable]], colWidths=[52, A4[0] - 44 - 52 - 58, 58])
    header_table.setStyle(TableStyle([
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('ALIGN', (0, 0), (0, 0), 'CENTER'),
        ('ALIGN', (2, 0), (2, 0), 'CENTER'),
        ('BOX', (2, 0), (2, 0), 1, colors.HexColor("#0f3d2e")),
        ('BACKGROUND', (2, 0), (2, 0), colors.HexColor("#fafaf9")),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
        ('TOPPADDING', (0, 0), (-1, -1), 1),
        ('LINEBELOW', (0, 0), (1, 0), 1.2, PRIMARY_GREEN),
    ]))
    return header_table


def _build_admission_form_flowables(app, form_data, logo_bytes, student_photo_bytes):
    """Build Page 1: Official Admission Form flowables matching the exact HTML layout."""
    styles = getSampleStyleSheet()
    lbl_style = ParagraphStyle('Lbl', fontName='Helvetica-Bold', fontSize=7, leading=9, textColor=colors.HexColor("#334155"))
    val_style = ParagraphStyle('Val', fontName='Helvetica', fontSize=7.5, leading=9.5, textColor=TEXT_DARK)
    sec_style = ParagraphStyle('Sec', fontName='Helvetica-Bold', fontSize=7.5, leading=9, textColor=colors.white)

    story = []
    # 1. School Header
    story.append(_build_header(logo_bytes, student_photo_bytes, "Official Admission Application & Assessment Record"))
    story.append(Spacer(1, 4))

    # 2. Meta Strip
    fd = form_data.get("form_details", {})
    sp = form_data.get("student_profile", {})
    pp = form_data.get("parent_profile", {})
    quiz = form_data.get("quiz_result", {})
    inter = form_data.get("interaction_schedule", {})

    app_no = esc(fd.get("application_number") or app.application_number or f"SF-APP-{app.id}")
    sub_id = esc(fd.get("submission_id") or app.submission_id or "-")
    sub_date = esc(fd.get("date_of_submission") or "-")
    std_med = f"{esc(fd.get('standard') or app.standard)} ({esc(fd.get('medium') or app.medium)})"
    vibhag = esc(fd.get("vibhag") or app.vibhag)
    merit_rank = f"#{esc(fd.get('merit_rank') or app.merit_rank)}"
    adm_status = esc(fd.get("admission_status") or app.admission_status or "SUBMITTED")
    quiz_score = f"{quiz.get('total_score', 0)}/50 ({quiz.get('qa_status', 'PENDING')} - {quiz.get('tag', '-')})"
    slot_info = f"{esc(inter.get('date'))} ({esc(inter.get('time'))})"

    meta_rows = [
        [
            Paragraph("<b>Application No:</b>", lbl_style), Paragraph(f"<b><font color='#8b1e28'>{app_no}</font></b>", val_style),
            Paragraph("<b>Submission ID:</b>", lbl_style), Paragraph(f"#{sub_id} ({sub_date})", val_style),
            Paragraph("<b>Standard & Medium:</b>", lbl_style), Paragraph(std_med, val_style),
            Paragraph("<b>Vibhag:</b>", lbl_style), Paragraph(vibhag, val_style),
        ],
        [
            Paragraph("<b>Merit Rank:</b>", lbl_style), Paragraph(f"<b>{merit_rank}</b>", val_style),
            Paragraph("<b>Status:</b>", lbl_style), Paragraph(f"<b><font color='#166534'>{adm_status}</font></b>", val_style),
            Paragraph("<b>Quiz Result:</b>", lbl_style), Paragraph(quiz_score, val_style),
            Paragraph("<b>Interaction Slot:</b>", lbl_style), Paragraph(slot_info, val_style),
        ]
    ]
    meta_table = Table(meta_rows, colWidths=[65, 75, 65, 85, 75, 80, 50, 56])
    meta_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, -1), LIGHT_BG),
        ('BOX', (0, 0), (-1, -1), 0.8, BORDER_COLOR),
        ('INNERGRID', (0, 0), (-1, -1), 0.4, BORDER_COLOR),
        ('TOPPADDING', (0, 0), (-1, -1), 2),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 2),
    ]))
    story.append(meta_table)
    story.append(Spacer(1, 4))

    def add_sec(title):
        t = Table([[Paragraph(f"&nbsp;{title}", sec_style)]], colWidths=[A4[0] - 44])
        t.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, -1), PRIMARY_GREEN),
            ('TOPPADDING', (0, 0), (-1, -1), 2),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 2),
            ('LEFTPADDING', (0, 0), (-1, -1), 3),
        ]))
        return t

    # SECTION 1: Student Particulars, Milestones & Health
    story.append(add_sec("1. STUDENT PARTICULARS, MILESTONES & HEALTH DETAILS"))
    full_name = f"{esc(sp.get('first_name'))} {esc(sp.get('father_name'))} {esc(sp.get('surname'))}"
    dob_full = f"{esc(sp.get('date_of_birth') or sp.get('dob'))} (Age: {esc(sp.get('age_summary'))})"
    birth_place = esc(sp.get("birth_place_full") or sp.get("birth_place"))
    ht_wt = f"Height: {esc(sp.get('height_cm'))} cm | Weight: {esc(sp.get('weight_kg'))} kg"
    mother_tongues = f"Mother: {esc(sp.get('mother_tongue_mother') or sp.get('mother_tongue'))} | Father: {esc(sp.get('mother_tongue_father'))}"
    milestones = f"Started Talking: {esc(sp.get('age_started_talking'))} | Walking: {esc(sp.get('age_started_walking'))}"
    langs = esc(sp.get("languages_known_str") or ", ".join(sp.get("languages_known", [])))
    past_ill = esc(sp.get("past_illness"))
    spec_care = esc(sp.get("specific_care_needed"))

    s1_rows = [
        [
            Paragraph("Student's Full Name:", lbl_style), Paragraph(f"<b>{full_name}</b>", val_style),
            Paragraph("Date of Birth & Age:", lbl_style), Paragraph(dob_full, val_style),
        ],
        [
            Paragraph("Birth Place (City/Taluka):", lbl_style), Paragraph(birth_place, val_style),
            Paragraph("Height & Weight:", lbl_style), Paragraph(ht_wt, val_style),
        ],
        [
            Paragraph("Mother Tongue (M / F):", lbl_style), Paragraph(mother_tongues, val_style),
            Paragraph("Developmental Milestones:", lbl_style), Paragraph(milestones, val_style),
        ],
        [
            Paragraph("Languages Known:", lbl_style), Paragraph(langs, val_style),
            Paragraph("Aadhar / Blood Group:", lbl_style), Paragraph(f"{esc(sp.get('aadhar_number'))} | {esc(sp.get('blood_group'))}", val_style),
        ],
        [
            Paragraph("Past Illness / Medical:", lbl_style), Paragraph(past_ill, val_style),
            Paragraph("Specific Care Needed:", lbl_style), Paragraph(spec_care, val_style),
        ],
    ]
    s1_table = Table(s1_rows, colWidths=[105, 170, 105, 171])
    s1_table.setStyle(TableStyle([
        ('BOX', (0, 0), (-1, -1), 0.5, BORDER_COLOR),
        ('INNERGRID', (0, 0), (-1, -1), 0.4, BORDER_COLOR),
        ('BACKGROUND', (0, 0), (0, -1), LIGHT_BG),
        ('BACKGROUND', (2, 0), (2, -1), LIGHT_BG),
        ('TOPPADDING', (0, 0), (-1, -1), 2),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 2),
    ]))
    story.append(s1_table)
    story.append(Spacer(1, 4))

    # SECTION 2: Parents Particulars (Father & Mother)
    story.append(add_sec("2. PARENTS PARTICULARS (FATHER & MOTHER)"))
    father_full = f"{esc(pp.get('father_name'))} {esc(pp.get('father_father_name'))} {esc(pp.get('father_surname'))}".strip()
    mother_full = f"{esc(pp.get('mother_name'))} {esc(pp.get('mother_father_or_husband_name'))} {esc(pp.get('mother_surname'))}".strip()

    f_occ_inc = f"{esc(pp.get('father_occupation'))} (Income: Rs. {esc(pp.get('father_annual_income'))})"
    m_occ_inc = f"{esc(pp.get('mother_occupation'))} (Income: Rs. {esc(pp.get('mother_annual_income'))})"

    f_phones = f"WA: {esc(pp.get('father_mobile'))} | Res: {esc(pp.get('father_phone_residence'))} | Off: {esc(pp.get('father_phone_office'))}"
    m_phones = f"WA: {esc(pp.get('mother_mobile'))} | Res: {esc(pp.get('mother_phone_residence'))} | Off: {esc(pp.get('mother_phone_office'))}"

    f_em_nat = f"{esc(pp.get('father_email'))} ({esc(pp.get('father_nationality', 'Indian'))})"
    m_em_nat = f"{esc(pp.get('mother_email'))} ({esc(pp.get('mother_nationality', 'Indian'))})"

    f_res_addr = esc(pp.get('father_address_residence') or pp.get('residential_address'))
    m_res_addr = esc(pp.get('mother_address_residence') or pp.get('residential_address'))

    f_off_addr = esc(pp.get('father_address_office'))
    m_off_addr = esc(pp.get('mother_address_office'))

    s2_rows = [
        [Paragraph("<b>Particulars</b>", lbl_style), Paragraph("<b>Father's Details</b>", lbl_style), Paragraph("<b>Mother's Details</b>", lbl_style)],
        [Paragraph("Full Name:", lbl_style), Paragraph(father_full, val_style), Paragraph(mother_full, val_style)],
        [Paragraph("Qualification:", lbl_style), Paragraph(esc(pp.get('father_qualification')), val_style), Paragraph(esc(pp.get('mother_qualification')), val_style)],
        [Paragraph("Occupation & Income:", lbl_style), Paragraph(f_occ_inc, val_style), Paragraph(m_occ_inc, val_style)],
        [Paragraph("Phone / WhatsApp:", lbl_style), Paragraph(f_phones, val_style), Paragraph(m_phones, val_style)],
        [Paragraph("Email & Nationality:", lbl_style), Paragraph(f_em_nat, val_style), Paragraph(m_em_nat, val_style)],
        [Paragraph("Residential Address:", lbl_style), Paragraph(f_res_addr, val_style), Paragraph(m_res_addr, val_style)],
        [Paragraph("Office Address:", lbl_style), Paragraph(f_off_addr, val_style), Paragraph(m_off_addr, val_style)],
    ]
    s2_table = Table(s2_rows, colWidths=[95, 228, 228])
    s2_table.setStyle(TableStyle([
        ('BOX', (0, 0), (-1, -1), 0.5, BORDER_COLOR),
        ('INNERGRID', (0, 0), (-1, -1), 0.4, BORDER_COLOR),
        ('BACKGROUND', (0, 0), (0, -1), LIGHT_BG),
        ('BACKGROUND', (0, 0), (-1, 0), LIGHT_BG),
        ('TOPPADDING', (0, 0), (-1, -1), 1.8),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 1.8),
    ]))
    story.append(s2_table)
    story.append(Spacer(1, 4))

    # SECTION 3: Siblings, Previous School & Vikas Kendra
    story.append(add_sec("3. SIBLINGS IN SHREYAS, PREVIOUS SCHOOL & VIKAS KENDRA ACTIVITIES"))
    ps_info = form_data.get("previous_school", {})
    sib_info = form_data.get("siblings_info", {})
    vk_info = form_data.get("vikas_kendra_activities", {})
    sigs = form_data.get("signatures_and_declarations", {})
    px = form_data.get("assessment_scores", {}).get("part_x", {})

    sib_summary = f"{sib_info.get('sibling_name', '')} (Std: {sib_info.get('sibling_class', '-')})" if sib_info.get("has_sibling_in_shreyas") else "No Sibling in Shreyas"
    prev_school = f"{esc(ps_info.get('school_name'))} ({esc(ps_info.get('school_place'))})"
    prev_std_med = f"Std: {esc(ps_info.get('school_std'))} | Med: {esc(ps_info.get('school_medium'))} | Year: {esc(ps_info.get('school_year'))}"
    alak_malak = f"{'Yes' if px.get('has_attended_alak_malak') else 'No'} (Duration: {esc(px.get('alak_malak_months', 0))} Months)"
    decls = f"No Private Tuitions: {'Yes' if sigs.get('agrees_discourage_private_tuitions') else 'No'} | Natural Medium: {'Yes' if sigs.get('open_to_natural_medium_schooling') else 'No'}"

    vk1_acts = ", ".join(str(x) for x in (vk_info.get("vk1") or [])) or "-"
    vk2_acts = ", ".join(str(x) for x in (vk_info.get("vk2") or [])) or "-"
    vk3_acts = ", ".join(str(x) for x in (vk_info.get("vk3") or [])) or "-"

    s3_rows = [
        [Paragraph("Siblings in Shreyas:", lbl_style), Paragraph(sib_summary, val_style), Paragraph("Attended Alak Malak?:", lbl_style), Paragraph(alak_malak, val_style)],
        [Paragraph("Previous School:", lbl_style), Paragraph(prev_school, val_style), Paragraph("Previous Std / Med:", lbl_style), Paragraph(prev_std_med, val_style)],
        [Paragraph("Schooling Declarations:", lbl_style), Paragraph(decls, val_style), Paragraph("VK 1 Activities:", lbl_style), Paragraph(vk1_acts, val_style)],
        [Paragraph("VK 2 Activities:", lbl_style), Paragraph(vk2_acts, val_style), Paragraph("VK 3 Activities:", lbl_style), Paragraph(vk3_acts, val_style)],
    ]
    s3_table = Table(s3_rows, colWidths=[105, 170, 105, 171])
    s3_table.setStyle(TableStyle([
        ('BOX', (0, 0), (-1, -1), 0.5, BORDER_COLOR),
        ('INNERGRID', (0, 0), (-1, -1), 0.4, BORDER_COLOR),
        ('BACKGROUND', (0, 0), (0, -1), LIGHT_BG),
        ('BACKGROUND', (2, 0), (2, -1), LIGHT_BG),
        ('TOPPADDING', (0, 0), (-1, -1), 1.8),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 1.8),
    ]))
    story.append(s3_table)
    story.append(Spacer(1, 4))

    # SECTION 4: Declaration & Signatures
    story.append(add_sec("4. DECLARATIONS & SIGNATURES"))
    terms_date = esc(sigs.get("terms_agreed_date"))
    decl_p = Paragraph(f"<b>Undertaking (Agreed to Rules: {'Yes' if sigs.get('agreed_to_rules') else 'No'} | Date: {terms_date}):</b><br/>"
                       f"I/We hereby declare that all information furnished in this admission application is true and complete to the best of my/our knowledge. "
                       f"I/We agree to abide by the philosophy, rules, and code of conduct of Shreyas Foundation Vikas Kendra.", ParagraphStyle('D', fontName='Helvetica', fontSize=6.8, leading=8.5, textColor=TEXT_DARK))
    story.append(decl_p)
    story.append(Spacer(1, 6))

    # Signatures
    father_sig_bytes = _fetch_file_bytes(sigs.get("father_signature_url"))
    mother_sig_bytes = _fetch_file_bytes(sigs.get("mother_signature_url"))
    guardian_sig_bytes = _fetch_file_bytes(sigs.get("guardian_signature_url"))

    def get_sig_flowable(raw_bytes, title):
        if raw_bytes:
            try:
                return [RLImage(io.BytesIO(raw_bytes), width=75, height=22), Paragraph(f"<b>{title}</b>", lbl_style)]
            except Exception:
                pass
        return [Spacer(1, 14), Paragraph(f"___________________<br/><b>{title}</b>", ParagraphStyle('SL', fontName='Helvetica', fontSize=7, leading=9, alignment=1))]

    sig_data = [[
        get_sig_flowable(father_sig_bytes, "Father's Signature"),
        get_sig_flowable(mother_sig_bytes, "Mother's Signature"),
        get_sig_flowable(guardian_sig_bytes, "Guardian's Signature"),
        [Spacer(1, 14), Paragraph("___________________<br/><b>Head of School / Director</b>", ParagraphStyle('SL2', fontName='Helvetica', fontSize=7, leading=9, alignment=1))]
    ]]
    sig_table = Table(sig_data, colWidths=[137, 137, 137, 140])
    sig_table.setStyle(TableStyle([
        ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
        ('VALIGN', (0, 0), (-1, -1), 'BOTTOM'),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 2),
    ]))
    story.append(sig_table)
    return story


def _build_quiz_report_flowables(app, form_data, logo_bytes):
    """Build Page 2: Parental Quiz Scorecard & Breakdown flowables matching HTML."""
    styles = getSampleStyleSheet()
    lbl_style = ParagraphStyle('QLbl', fontName='Helvetica-Bold', fontSize=7, leading=9, textColor=colors.HexColor("#334155"))
    val_style = ParagraphStyle('QVal', fontName='Helvetica', fontSize=7.5, leading=9.5, textColor=TEXT_DARK)
    sec_style = ParagraphStyle('QSec', fontName='Helvetica-Bold', fontSize=7.5, leading=9, textColor=colors.white)

    story = []
    # Header
    story.append(_build_header(logo_bytes, None, "Parental Interaction & Quiz Evaluation Record"))
    story.append(Spacer(1, 4))

    quiz = form_data.get("quiz_result", {})
    scores = form_data.get("assessment_scores", {})
    px = scores.get("part_x", {})

    total_quiz = quiz.get("total_score", 0)
    tag_str = quiz.get("tag", "-")
    status_str = "ELIGIBLE (Passed)" if quiz.get("is_eligible") else "INELIGIBLE"

    score_banner = [
        [
            Paragraph("<b>Submission ID:</b>", lbl_style), Paragraph(str(quiz.get("submission_id") or "-"), val_style),
            Paragraph("<b>Parent MCQ Score:</b>", lbl_style), Paragraph(f"<b>{quiz.get('mcq_score', 0)} / 30</b>", val_style),
            Paragraph("<b>Written Score:</b>", lbl_style), Paragraph(f"<b>{quiz.get('written_score', 0)} / 20</b>", val_style),
        ],
        [
            Paragraph("<b>Total Part X:</b>", lbl_style), Paragraph(f"<b>{total_quiz} / 50</b>", val_style),
            Paragraph("<b>Result Status:</b>", lbl_style), Paragraph(f"<b>{status_str}</b>", val_style),
            Paragraph("<b>Evaluation Tag:</b>", lbl_style), Paragraph(f"<b>{tag_str}</b>", val_style),
        ]
    ]
    banner_table = Table(score_banner, colWidths=[70, 100, 80, 110, 85, 106])
    banner_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, -1), LIGHT_BG),
        ('BOX', (0, 0), (-1, -1), 0.8, BORDER_COLOR),
        ('INNERGRID', (0, 0), (-1, -1), 0.4, BORDER_COLOR),
        ('TOPPADDING', (0, 0), (-1, -1), 2),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 2),
    ]))
    story.append(banner_table)
    story.append(Spacer(1, 4))

    # MCQ Breakdown Table (Q1 - Q10)
    def add_sec(title):
        t = Table([[Paragraph(f"&nbsp;{title}", sec_style)]], colWidths=[A4[0] - 44])
        t.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, -1), PRIMARY_GREEN),
            ('TOPPADDING', (0, 0), (-1, -1), 2),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 2),
        ]))
        return t

    story.append(add_sec("SECTION A: MULTIPLE CHOICE QUESTIONS (1 TO 10)"))

    mcq_table_data = [
        [
            Paragraph("<b>#</b>", lbl_style),
            Paragraph("<b>Question Topic & Focus</b>", lbl_style),
            Paragraph("<b>Parent Selected Option</b>", lbl_style),
            Paragraph("<b>Marks</b>", lbl_style)
        ]
    ]

    for q in quiz.get("mcq_questions", []):
        q_id = q.get("q_id") or q.get("id")
        q_focus = esc(q.get("focus") or f"Question {q_id}")
        q_en = esc(q.get("question_en") or "")
        sel_key = esc(q.get("selected_key") or "-")
        sel_text = esc(q.get("selected_text_en") or q.get("selected_option_text") or "-")
        marks = f"{q.get('earned_marks', q.get('marks_earned', 0))} / {q.get('max_marks', 3)}"

        q_cell = [Paragraph(f"<b><font color='#8b1e28'>{q_focus}</font></b>", ParagraphStyle('F', fontName='Helvetica-Bold', fontSize=6.5, leading=8)),
                  Paragraph(q_en, ParagraphStyle('QE', fontName='Helvetica', fontSize=7, leading=8.5, textColor=TEXT_DARK))]
        opt_cell = [Paragraph(f"<b>Option {sel_key}:</b> {sel_text}", ParagraphStyle('OE', fontName='Helvetica', fontSize=7, leading=8.5, textColor=TEXT_DARK))]

        mcq_table_data.append([
            Paragraph(f"Q{q_id}", ParagraphStyle('QNum', fontName='Helvetica-Bold', fontSize=7, alignment=1)),
            q_cell,
            opt_cell,
            Paragraph(marks, ParagraphStyle('QMrk', fontName='Helvetica-Bold', fontSize=7, alignment=1, textColor=PRIMARY_GREEN))
        ])

    mcq_table = Table(mcq_table_data, colWidths=[24, 250, 220, 57])
    mcq_table.setStyle(TableStyle([
        ('BOX', (0, 0), (-1, -1), 0.5, BORDER_COLOR),
        ('INNERGRID', (0, 0), (-1, -1), 0.4, BORDER_COLOR),
        ('BACKGROUND', (0, 0), (-1, 0), LIGHT_BG),
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('TOPPADDING', (0, 0), (-1, -1), 1.6),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 1.6),
    ]))
    story.append(mcq_table)
    story.append(Spacer(1, 4))

    # Written Questions (Q11 - Q14)
    story.append(add_sec("SECTION B: WRITTEN REFLECTIONS & AI EVALUATION (11 TO 14)"))

    wq_table_data = [
        [
            Paragraph("<b>#</b>", lbl_style),
            Paragraph("<b>Question Topic & Focus</b>", lbl_style),
            Paragraph("<b>Parent's Response</b>", lbl_style),
            Paragraph("<b>AI Feedback & Score</b>", lbl_style)
        ]
    ]

    for q in quiz.get("written_questions", []):
        q_id = q.get("q_id") or q.get("id")
        q_focus = esc(q.get("focus") or f"Question {q_id}")
        q_en = esc(q.get("question_en") or "")
        ans_text = esc(q.get("parent_answer") or q.get("answer") or "No answer provided")
        feedback = esc(q.get("ai_feedback") or "Evaluated")
        earned = q.get("earned_marks", q.get("score", 0))

        q_cell = [Paragraph(f"<b><font color='#8b1e28'>{q_focus}</font></b>", ParagraphStyle('WF', fontName='Helvetica-Bold', fontSize=6.5, leading=8)),
                  Paragraph(q_en, ParagraphStyle('WQE', fontName='Helvetica', fontSize=7, leading=8.5, textColor=TEXT_DARK))]
        ans_cell = [Paragraph(ans_text, ParagraphStyle('WA', fontName='Helvetica', fontSize=7, leading=8.5, textColor=TEXT_DARK))]
        score_cell = [Paragraph(f"<b>Score: {earned} / 5</b>", ParagraphStyle('WS', fontName='Helvetica-Bold', fontSize=7, textColor=PRIMARY_GREEN)),
                      Paragraph(f"<font color='#475569'>{feedback}</font>", ParagraphStyle('WFB', fontName='Helvetica-Oblique', fontSize=6.5, leading=8))]

        wq_table_data.append([
            Paragraph(f"Q{q_id}", ParagraphStyle('WQNum', fontName='Helvetica-Bold', fontSize=7, alignment=1)),
            q_cell,
            ans_cell,
            score_cell
        ])

    wq_table = Table(wq_table_data, colWidths=[24, 185, 222, 120])
    wq_table.setStyle(TableStyle([
        ('BOX', (0, 0), (-1, -1), 0.5, BORDER_COLOR),
        ('INNERGRID', (0, 0), (-1, -1), 0.4, BORDER_COLOR),
        ('BACKGROUND', (0, 0), (-1, 0), LIGHT_BG),
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('TOPPADDING', (0, 0), (-1, -1), 1.8),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 1.8),
    ]))
    story.append(wq_table)
    return story


def _convert_image_to_a4_pdf_bytes(img_bytes, doc_name, app_no, student_name):
    """Render an uploaded document image onto a dedicated A4 page with school header and footer."""
    try:
        pil_img = Image.open(io.BytesIO(img_bytes))
        if pil_img.mode != "RGB":
            pil_img = pil_img.convert("RGB")

        page_w, page_h = A4
        margin = 22
        header_h = 42
        footer_h = 24
        avail_w = page_w - (2 * margin)
        avail_h = page_h - header_h - footer_h - (2 * margin)

        img_w, img_h = pil_img.size
        ratio = min(avail_w / img_w, avail_h / img_h)
        target_w = img_w * ratio
        target_h = img_h * ratio

        x = margin + (avail_w - target_w) / 2
        y = margin + footer_h + (avail_h - target_h) / 2

        out_pdf_io = io.BytesIO()
        c = canvas.Canvas(out_pdf_io, pagesize=A4)

        # Header Title
        c.setFont("Helvetica-Bold", 10)
        c.setFillColor(PRIMARY_GREEN)
        c.drawString(margin, page_h - margin - 12, "SHREYAS FOUNDATION — ATTACHED VERIFICATION DOCUMENT")

        c.setFont("Helvetica-Bold", 8.5)
        c.setFillColor(ACCENT_MAROON)
        c.drawString(margin, page_h - margin - 24, doc_name.upper())

        c.setFont("Helvetica", 7.5)
        c.setFillColor(colors.HexColor("#64748b"))
        c.drawRightString(page_w - margin, page_h - margin - 18, f"App No: {app_no} | Student: {student_name}")

        c.setStrokeColor(PRIMARY_GREEN)
        c.setLineWidth(0.8)
        c.line(margin, page_h - margin - 30, page_w - margin, page_h - margin - 30)

        # Draw image
        img_temp_io = io.BytesIO()
        pil_img.save(img_temp_io, format="JPEG", quality=92)
        img_temp_io.seek(0)
        c.drawImage(RLImage(img_temp_io, width=target_w, height=target_h).filename or img_temp_io, x, y, width=target_w, height=target_h)

        # Footer
        c.line(margin, 22, page_w - margin, 22)
        c.setFont("Helvetica", 7.5)
        c.setFillColor(colors.HexColor("#64748b"))
        c.drawString(margin, 12, f"Document: {doc_name} | Application No: {app_no}")

        c.showPage()
        c.save()
        return out_pdf_io.getvalue()
    except Exception as e:
        logger.error(f"Error converting image to A4 PDF for {doc_name}: {e}")
        return None


def generate_single_student_complete_pdf_stream(app, form_data):
    """
    Generate a complete, high-quality native PDF byte stream for a single student:
    - Page 1: Official Admission Application Form
    - Page 2: Parental Interaction & Quiz Scorecard
    - Page 3+: Each uploaded certificate, ID card, and health record on its own dedicated A4 page.
    """
    logo_bytes = _fetch_file_bytes(SHREYAS_LOGO_URL)
    photo_bytes = _fetch_file_bytes(app.photograph_child)

    # 1. Generate Pages 1 & 2 using ReportLab
    rl_pdf_io = io.BytesIO()
    doc = SimpleDocTemplate(
        rl_pdf_io,
        pagesize=A4,
        leftMargin=22,
        rightMargin=22,
        topMargin=22,
        bottomMargin=28
    )

    story = []
    # Page 1: Admission Form
    story.extend(_build_admission_form_flowables(app, form_data, logo_bytes, photo_bytes))
    story.append(PageBreak())

    # Page 2: Quiz Scorecard
    story.extend(_build_quiz_report_flowables(app, form_data, logo_bytes))

    # Build the first two pages
    doc.build(story, canvasmaker=NumberedCanvas)
    rl_pdf_bytes = rl_pdf_io.getvalue()

    # 2. Collect uploaded documents
    doc_fields = [
        ("photograph_child", "Child Passport Photograph"),
        ("birth_certificate", "Birth Certificate"),
        ("child_aadhar_card", "Child's Aadhar Card"),
        ("parent_aadhar_card", "Parent's / Guardian's Aadhar Card"),
        ("health_certificate", "Doctor's Health Certificate"),
        ("eye_checkup_certificate", "Eye Checkup Certificate"),
        ("caste_certificate", "Caste Certificate"),
        ("previous_report_card", "Previous Year's Report Card"),
        ("alak_malak_certificate", "Alak Malak Certificate"),
    ]

    student_name = f"{app.child_first_name} {app.child_surname}".strip()
    app_no = app.application_number or f"SF-APP-{app.id}"

    annexure_pdfs = []
    for field_name, display_name in doc_fields:
        field_val = getattr(app, field_name, None)
        if not field_val:
            continue

        raw_bytes = _fetch_file_bytes(field_val)
        if not raw_bytes:
            continue

        if raw_bytes[:4] == b'%PDF':
            annexure_pdfs.append((display_name, raw_bytes, True))
        else:
            img_pdf = _convert_image_to_a4_pdf_bytes(raw_bytes, display_name, app_no, student_name)
            if img_pdf:
                annexure_pdfs.append((display_name, img_pdf, False))

    # 3. Merge Pages 1 & 2 with all uploaded Annexure Document PDFs using PyPDF2
    merger = PyPDF2.PdfMerger()
    merger.append(io.BytesIO(rl_pdf_bytes))

    for doc_title, pdf_data, is_raw_pdf in annexure_pdfs:
        try:
            merger.append(io.BytesIO(pdf_data))
        except Exception as e:
            logger.warning(f"Could not merge annexure {doc_title}: {e}")

    final_pdf_io = io.BytesIO()
    merger.write(final_pdf_io)
    merger.close()

    return final_pdf_io.getvalue()
