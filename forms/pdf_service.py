import io
import os
import logging
import requests
from PIL import Image as PILImage
try:
    import PyPDF2
except ImportError:
    try:
        import pypdf as PyPDF2
    except ImportError:
        PyPDF2 = None

from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak, Image as RLImage, KeepTogether
)
from reportlab.pdfgen import canvas

logger = logging.getLogger(__name__)

OFFICIAL_LOGO_URL = "https://shreyasfoundation.blob.core.windows.net/media/shreyas_logo.png"
_CACHED_LOGO_BYTES = None

def get_logo_bytes():
    global _CACHED_LOGO_BYTES
    if _CACHED_LOGO_BYTES:
        return _CACHED_LOGO_BYTES
    try:
        resp = requests.get(OFFICIAL_LOGO_URL, timeout=10)
        if resp.status_code == 200:
            _CACHED_LOGO_BYTES = resp.content
            return _CACHED_LOGO_BYTES
    except Exception as e:
        logger.error(f"Error fetching logo: {e}")
    return None

def download_file_bytes(url):
    if not url:
        return None, None
    try:
        resp = requests.get(url, timeout=15)
        if resp.status_code == 200:
            content_type = resp.headers.get('content-type', '').lower()
            return resp.content, content_type
    except Exception as e:
        logger.error(f"Error downloading file {url}: {e}")
    return None, None

class NumberedCanvas(canvas.Canvas):
    """
    Two-pass canvas to dynamically compute and draw total page numbers and footer.
    """
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
        self.setFont("Helvetica", 8)
        self.setFillColor(colors.HexColor("#64748B"))
        # Header line (pages 2+)
        if self._pageNumber > 1:
            self.setStrokeColor(colors.HexColor("#CBD5E1"))
            self.setLineWidth(0.5)
            self.line(36, 806, 559, 806)
            self.drawString(36, 812, "Shreyas Foundation — Official Application Record")

        # Footer
        self.setStrokeColor(colors.HexColor("#CBD5E1"))
        self.setLineWidth(0.5)
        self.line(36, 35, 559, 35)
        self.drawString(36, 24, "Shreyas Foundation, Near Manekbaug Hall, Ambawadi, Ahmedabad - 380015")
        page_str = f"Page {self._pageNumber} of {page_count}"
        self.drawRightString(559, 24, page_str)
        self.restoreState()


def build_application_pdf(form_data):
    """
    Builds a professional multi-page native binary PDF:
    Page 1: Complete Admission Application Form
    Page 2: Parental Quiz Result with MCQ & Written evaluations
    Page 3+: Dedicated full A4 page for each uploaded document/certificate.
    Returns: bytes of the final assembled PDF.
    """
    fd = form_data.get("form_details", {})
    sp = form_data.get("student_profile", {})
    pp = form_data.get("parent_profile", {})
    gp = form_data.get("guardian_profile", {})
    sib = form_data.get("siblings_info", {})
    ps = form_data.get("previous_school", {})
    sch = form_data.get("interaction_schedule", {})
    scores = form_data.get("assessment_scores", {})
    px = scores.get("part_x", {})
    py = scores.get("part_y", {})
    quiz = form_data.get("quiz_result", {})
    docs = form_data.get("documents_uploaded", {})

    pdf_buffer = io.BytesIO()
    # A4: 595.27 x 841.89 points. Margins: 36 pt (0.5 in) left/right, 40 pt top/bottom.
    doc = SimpleDocTemplate(
        pdf_buffer,
        pagesize=A4,
        leftMargin=36,
        rightMargin=36,
        topMargin=40,
        bottomMargin=45
    )

    styles = getSampleStyleSheet()
    
    # Custom styles
    primary_color = colors.HexColor("#1E3A8A")   # Navy 900
    sub_color = colors.HexColor("#0D9488")       # Teal 600
    border_color = colors.HexColor("#CBD5E1")    # Slate 300
    bg_header = colors.HexColor("#F8FAFC")       # Slate 50
    bg_sub = colors.HexColor("#F1F5F9")          # Slate 100
    dark_text = colors.HexColor("#0F172A")       # Slate 900
    muted_text = colors.HexColor("#475569")      # Slate 600

    title_style = ParagraphStyle(
        'DocTitle',
        parent=styles['Normal'],
        fontName='Helvetica-Bold',
        fontSize=15,
        leading=18,
        textColor=primary_color,
        alignment=1 # Center
    )
    subtitle_style = ParagraphStyle(
        'DocSub',
        parent=styles['Normal'],
        fontName='Helvetica',
        fontSize=9,
        leading=12,
        textColor=muted_text,
        alignment=1
    )
    badge_style = ParagraphStyle(
        'Badge',
        parent=styles['Normal'],
        fontName='Helvetica-Bold',
        fontSize=9,
        leading=11,
        textColor=colors.white,
        alignment=1
    )
    sec_heading_style = ParagraphStyle(
        'SecHead',
        parent=styles['Normal'],
        fontName='Helvetica-Bold',
        fontSize=9,
        leading=11,
        textColor=primary_color
    )
    cell_label = ParagraphStyle(
        'CellLabel',
        parent=styles['Normal'],
        fontName='Helvetica-Bold',
        fontSize=7.5,
        leading=9.5,
        textColor=muted_text
    )
    cell_val = ParagraphStyle(
        'CellVal',
        parent=styles['Normal'],
        fontName='Helvetica',
        fontSize=8,
        leading=10,
        textColor=dark_text
    )
    cell_val_bold = ParagraphStyle(
        'CellValB',
        parent=styles['Normal'],
        fontName='Helvetica-Bold',
        fontSize=8,
        leading=10,
        textColor=dark_text
    )

    story = []

    # -------------------------------------------------------------
    # PAGE 1: ADMISSION APPLICATION FORM
    # -------------------------------------------------------------
    logo_bytes = get_logo_bytes()
    header_data = []
    logo_img = None
    if logo_bytes:
        try:
            logo_io = io.BytesIO(logo_bytes)
            logo_img = RLImage(logo_io, width=54, height=54)
        except Exception:
            logo_img = None

    title_cell = [
        Paragraph("SHREYAS FOUNDATION", title_style),
        Paragraph("Near Manekbaug Hall, Ambawadi, Ahmedabad - 380015 | Tel: 079-26601338", subtitle_style),
        Spacer(1, 2),
        Paragraph("<b>OFFICIAL ADMISSION APPLICATION FORM</b> — Academic Session 2026-2027", ParagraphStyle('TSub', parent=subtitle_style, fontName='Helvetica-Bold', textColor=colors.HexColor('#0F172A'), fontSize=9.5))
    ]

    # Status / App Number Box
    app_no = fd.get("application_number") or f"SUB-{quiz.get('submission_id', 'NA')}"
    status_str = fd.get("admission_status") or "PENDING"
    status_bg = colors.HexColor("#16A34A") if status_str == "SELECTED" else (colors.HexColor("#EAB308") if status_str == "WAITLISTED" else colors.HexColor("#2563EB"))
    
    status_box = [
        Paragraph(f"<b>Application No:</b><br/>{app_no}", ParagraphStyle('SB1', parent=cell_val_bold, fontSize=8.5, leading=11, alignment=1)),
        Spacer(1, 2),
        Paragraph(f"<font color='white'><b>{status_str}</b></font>", ParagraphStyle('SB2', parent=badge_style, fontSize=8, leading=10, alignment=1))
    ]

    header_table = Table(
        [[logo_img if logo_img else "", title_cell, status_box]],
        colWidths=[60, 363, 100]
    )
    header_table.setStyle(TableStyle([
        ('VALIGN', (0,0), (-1,-1), 'MIDDLE'),
        ('ALIGN', (2,0), (2,0), 'CENTER'),
        ('BACKGROUND', (2,0), (2,0), bg_header),
        ('BOX', (2,0), (2,0), 1, border_color),
        ('BOTTOMPADDING', (0,0), (-1,-1), 4),
        ('TOPPADDING', (0,0), (-1,-1), 4),
    ]))
    story.append(header_table)
    story.append(Spacer(1, 6))

    # Basic admission info bar
    info_bar_data = [
        [
            Paragraph("<b>Standard Applied:</b>", cell_label),
            Paragraph(str(fd.get("standard") or "-"), cell_val_bold),
            Paragraph("<b>Medium:</b>", cell_label),
            Paragraph(str(fd.get("medium") or "-"), cell_val_bold),
            Paragraph("<b>Vibhag:</b>", cell_label),
            Paragraph(str(fd.get("vibhag") or "-"), cell_val_bold),
            Paragraph("<b>Applied Date:</b>", cell_label),
            Paragraph(str(fd.get("submission_date") or "-"), cell_val)
        ]
    ]
    info_table = Table(info_bar_data, colWidths=[75, 55, 50, 65, 50, 65, 75, 88])
    info_table.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,-1), bg_sub),
        ('BOX', (0,0), (-1,-1), 0.5, border_color),
        ('VALIGN', (0,0), (-1,-1), 'MIDDLE'),
        ('TOPPADDING', (0,0), (-1,-1), 3),
        ('BOTTOMPADDING', (0,0), (-1,-1), 3),
    ]))
    story.append(info_table)
    story.append(Spacer(1, 6))

    def make_sec_header(title):
        t = Table([[Paragraph(f"<b>{title}</b>", sec_heading_style)]], colWidths=[523])
        t.setStyle(TableStyle([
            ('BACKGROUND', (0,0), (-1,-1), bg_header),
            ('BOX', (0,0), (-1,-1), 0.5, border_color),
            ('BOTTOMPADDING', (0,0), (-1,-1), 2),
            ('TOPPADDING', (0,0), (-1,-1), 2),
        ]))
        return t

    # SECTION 1: CANDIDATE (STUDENT) PROFILE
    story.append(make_sec_header("1. CANDIDATE (STUDENT) INFORMATION"))
    student_rows = [
        [
            Paragraph("<b>Full Name:</b>", cell_label),
            Paragraph(f"{sp.get('first_name', '')} {sp.get('father_name', '')} {sp.get('surname', '')}".strip() or "-", cell_val_bold),
            Paragraph("<b>Gender / DOB:</b>", cell_label),
            Paragraph(f"{sp.get('gender') or '-'} | {sp.get('dob') or '-'}", cell_val),
            Paragraph("<b>Age:</b>", cell_label),
            Paragraph(str(sp.get("age") or "-"), cell_val)
        ],
        [
            Paragraph("<b>Birth Place:</b>", cell_label),
            Paragraph(f"{sp.get('birth_place') or '-'}, {sp.get('birth_taluka') or '-'}", cell_val),
            Paragraph("<b>Religion / Caste:</b>", cell_label),
            Paragraph(f"{sp.get('religion') or '-'} | {sp.get('caste') or '-'} ({sp.get('category') or '-'})", cell_val),
            Paragraph("<b>Aadhar No:</b>", cell_label),
            Paragraph(str(sp.get("aadhar_number") or "-"), cell_val)
        ],
        [
            Paragraph("<b>Blood Group:</b>", cell_label),
            Paragraph(str(sp.get("blood_group") or "-"), cell_val),
            Paragraph("<b>Mother Tongue:</b>", cell_label),
            Paragraph(str(sp.get("mother_tongue") or "-"), cell_val),
            Paragraph("<b>Nationality:</b>", cell_label),
            Paragraph(str(sp.get("nationality") or "Indian"), cell_val)
        ]
    ]
    st_table = Table(student_rows, colWidths=[70, 140, 80, 110, 45, 78])
    st_table.setStyle(TableStyle([
        ('GRID', (0,0), (-1,-1), 0.5, border_color),
        ('VALIGN', (0,0), (-1,-1), 'MIDDLE'),
        ('TOPPADDING', (0,0), (-1,-1), 2.5),
        ('BOTTOMPADDING', (0,0), (-1,-1), 2.5),
    ]))
    story.append(st_table)
    story.append(Spacer(1, 6))

    # SECTION 2: PARENT & GUARDIAN PROFILE
    story.append(make_sec_header("2. PARENT & GUARDIAN DETAILS"))
    parent_rows = [
        [
            Paragraph("<b>Father's Name:</b>", cell_label),
            Paragraph(f"{pp.get('father_name', '')} {pp.get('father_surname', '')}".strip() or "-", cell_val_bold),
            Paragraph("<b>Mother's Name:</b>", cell_label),
            Paragraph(f"{pp.get('mother_name', '')} {pp.get('mother_surname', '')}".strip() or "-", cell_val_bold)
        ],
        [
            Paragraph("<b>Father Edu / Job:</b>", cell_label),
            Paragraph(f"{pp.get('father_education') or '-'} | {pp.get('father_occupation') or '-'}", cell_val),
            Paragraph("<b>Mother Edu / Job:</b>", cell_label),
            Paragraph(f"{pp.get('mother_education') or '-'} | {pp.get('mother_occupation') or '-'}", cell_val)
        ],
        [
            Paragraph("<b>Father Contact:</b>", cell_label),
            Paragraph(f"Mob: {pp.get('father_mobile') or '-'} | WA: {pp.get('father_whatsapp') or '-'}", cell_val),
            Paragraph("<b>Mother Contact:</b>", cell_label),
            Paragraph(f"Mob: {pp.get('mother_mobile') or '-'} | WA: {pp.get('mother_whatsapp') or '-'}", cell_val)
        ],
        [
            Paragraph("<b>Father Email:</b>", cell_label),
            Paragraph(str(pp.get("father_email") or "-"), cell_val),
            Paragraph("<b>Mother Email:</b>", cell_label),
            Paragraph(str(pp.get("mother_email") or "-"), cell_val)
        ],
        [
            Paragraph("<b>Residential Addr:</b>", cell_label),
            Paragraph(f"{pp.get('residence_address') or '-'}; {pp.get('city') or '-'}, {pp.get('state') or '-'} - {pp.get('pincode') or '-'}", cell_val),
            Paragraph("<b>Annual Income:</b>", cell_label),
            Paragraph(f"₹ {pp.get('annual_income') or '-'}", cell_val)
        ]
    ]
    pr_table = Table(parent_rows, colWidths=[90, 171, 90, 172])
    pr_table.setStyle(TableStyle([
        ('GRID', (0,0), (-1,-1), 0.5, border_color),
        ('VALIGN', (0,0), (-1,-1), 'MIDDLE'),
        ('TOPPADDING', (0,0), (-1,-1), 2.5),
        ('BOTTOMPADDING', (0,0), (-1,-1), 2.5),
    ]))
    story.append(pr_table)
    story.append(Spacer(1, 6))

    # SECTION 3: SIBLING & PREVIOUS SCHOOL DETAILS
    story.append(make_sec_header("3. SIBLINGS & PREVIOUS ACADEMIC RECORD"))
    has_sib = "Yes" if sib.get("has_sibling_in_shreyas") else "No"
    sib_rows = [
        [
            Paragraph("<b>Sibling in Shreyas?</b>", cell_label),
            Paragraph(f"{has_sib} (Name: {sib.get('sibling_name') or '-'}, Class: {sib.get('sibling_class') or '-'})", cell_val),
            Paragraph("<b>VK Opted / Rec:</b>", cell_label),
            Paragraph(f"VK1: {sib.get('vk1_opted') or '-'} ({sib.get('vk1_receipt') or '-'}) | VK2: {sib.get('vk2_opted') or '-'}", cell_val)
        ],
        [
            Paragraph("<b>Previous School:</b>", cell_label),
            Paragraph(f"{ps.get('school_name') or '-'} ({ps.get('school_place') or '-'})", cell_val),
            Paragraph("<b>Past Std / Med:</b>", cell_label),
            Paragraph(f"Std: {ps.get('school_std') or '-'} | Medium: {ps.get('school_medium') or '-'} ({ps.get('school_year') or '-'})", cell_val)
        ]
    ]
    sb_table = Table(sib_rows, colWidths=[105, 156, 95, 167])
    sb_table.setStyle(TableStyle([
        ('GRID', (0,0), (-1,-1), 0.5, border_color),
        ('VALIGN', (0,0), (-1,-1), 'MIDDLE'),
        ('TOPPADDING', (0,0), (-1,-1), 2.5),
        ('BOTTOMPADDING', (0,0), (-1,-1), 2.5),
    ]))
    story.append(sb_table)
    story.append(Spacer(1, 6))

    # SECTION 4: INTERACTION SCHEDULE & DECLARATION
    story.append(make_sec_header("4. INTERACTION SCHEDULE & PARENT DECLARATION"))
    sch_rows = [
        [
            Paragraph("<b>Allocated Date:</b>", cell_label),
            Paragraph(str(sch.get("date") or "-"), cell_val_bold),
            Paragraph("<b>Slot & Time:</b>", cell_label),
            Paragraph(f"Slot #{sch.get('slot_no') or '-'} | {sch.get('time') or '-'}", cell_val_bold),
            Paragraph("<b>Venue:</b>", cell_label),
            Paragraph(str(sch.get("venue") or "Shreyas Foundation Campus"), cell_val)
        ]
    ]
    sc_table = Table(sch_rows, colWidths=[80, 85, 75, 100, 45, 138])
    sc_table.setStyle(TableStyle([
        ('GRID', (0,0), (-1,-1), 0.5, border_color),
        ('VALIGN', (0,0), (-1,-1), 'MIDDLE'),
        ('TOPPADDING', (0,0), (-1,-1), 2.5),
        ('BOTTOMPADDING', (0,0), (-1,-1), 2.5),
    ]))
    story.append(sc_table)
    story.append(Spacer(1, 8))

    # Signatures
    sig_table = Table([
        [
            Paragraph("____________________________<br/><b>Father's Signature</b>", ParagraphStyle('SF', parent=cell_val, alignment=1)),
            Paragraph("____________________________<br/><b>Mother's Signature</b>", ParagraphStyle('SM', parent=cell_val, alignment=1)),
            Paragraph("____________________________<br/><b>Authorized Scrutinizer</b>", ParagraphStyle('SA', parent=cell_val, alignment=1))
        ]
    ], colWidths=[174, 174, 175])
    sig_table.setStyle(TableStyle([
        ('ALIGN', (0,0), (-1,-1), 'CENTER'),
        ('TOPPADDING', (0,0), (-1,-1), 10),
        ('BOTTOMPADDING', (0,0), (-1,-1), 4),
    ]))
    story.append(sig_table)

    # -------------------------------------------------------------
    # PAGE 2: PARENTAL QUIZ RESULT & QUESTIONNAIRE
    # -------------------------------------------------------------
    story.append(PageBreak())
    
    quiz_header_table = Table(
        [[
            logo_img if logo_img else "",
            [
                Paragraph("SHREYAS FOUNDATION — PARENTAL QUESTIONNAIRE", title_style),
                Paragraph("Evaluation & Assessment Report — Total Marks: 50 | Minimum Required: 25", subtitle_style)
            ],
            [
                Paragraph(f"<b>Quiz Score</b><br/><font size=12 color='#1E3A8A'><b>{quiz.get('total_score', 0)} / 50</b></font>", ParagraphStyle('QS', parent=cell_val_bold, alignment=1)),
                Paragraph(f"<b>Status:</b> {quiz.get('qa_status', 'PASSED')}", ParagraphStyle('QS2', parent=cell_val, alignment=1))
            ]
        ]],
        colWidths=[60, 363, 100]
    )
    quiz_header_table.setStyle(TableStyle([
        ('VALIGN', (0,0), (-1,-1), 'MIDDLE'),
        ('BOX', (2,0), (2,0), 1, border_color),
        ('BACKGROUND', (2,0), (2,0), bg_header),
        ('BOTTOMPADDING', (0,0), (-1,-1), 4),
        ('TOPPADDING', (0,0), (-1,-1), 4),
    ]))
    story.append(quiz_header_table)
    story.append(Spacer(1, 6))

    # Quiz Summary bar
    quiz_bar = [
        [
            Paragraph("<b>Student Name:</b>", cell_label),
            Paragraph(f"{sp.get('first_name', '')} {sp.get('surname', '')}".strip() or "-", cell_val_bold),
            Paragraph("<b>MCQ Score:</b>", cell_label),
            Paragraph(f"{quiz.get('mcq_score', 0)} / 30", cell_val_bold),
            Paragraph("<b>Written Score:</b>", cell_label),
            Paragraph(f"{quiz.get('written_score', 0)} / 20", cell_val_bold),
            Paragraph("<b>Category Tag:</b>", cell_label),
            Paragraph(str(quiz.get('tag') or "-"), cell_val_bold)
        ]
    ]
    q_bar_table = Table(quiz_bar, colWidths=[75, 115, 65, 55, 75, 55, 70, 73])
    q_bar_table.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,-1), bg_sub),
        ('BOX', (0,0), (-1,-1), 0.5, border_color),
        ('VALIGN', (0,0), (-1,-1), 'MIDDLE'),
        ('TOPPADDING', (0,0), (-1,-1), 3),
        ('BOTTOMPADDING', (0,0), (-1,-1), 3),
    ]))
    story.append(q_bar_table)
    story.append(Spacer(1, 6))

    # MCQ Questions List (1-10)
    story.append(make_sec_header("PART A: OBJECTIVE MULTIPLE CHOICE QUESTIONS (MCQ 1 to 10)"))
    mcq_table_data = [
        [
            Paragraph("<b>#</b>", cell_label),
            Paragraph("<b>Question</b>", cell_label),
            Paragraph("<b>Selected Answer</b>", cell_label),
            Paragraph("<b>Marks</b>", cell_label)
        ]
    ]
    for q in quiz.get("mcq_questions", []):
        mcq_table_data.append([
            Paragraph(str(q.get("id")), cell_val_bold),
            Paragraph(f"{q.get('question_en', '')}<br/><font color='#475569'>{q.get('question_gu', '')}</font>", cell_val),
            Paragraph(f"<b>[{q.get('selected_key') or '-'}]</b> {q.get('selected_text') or '-'}", cell_val),
            Paragraph(f"<b>{q.get('marks_earned', 0)}</b> / {q.get('max_marks', 3)}", cell_val_bold)
        ])
    
    mcq_tbl = Table(mcq_table_data, colWidths=[20, 240, 215, 48])
    mcq_tbl.setStyle(TableStyle([
        ('GRID', (0,0), (-1,-1), 0.5, border_color),
        ('BACKGROUND', (0,0), (-1,0), bg_header),
        ('VALIGN', (0,0), (-1,-1), 'MIDDLE'),
        ('TOPPADDING', (0,0), (-1,-1), 2),
        ('BOTTOMPADDING', (0,0), (-1,-1), 2),
    ]))
    story.append(mcq_tbl)
    story.append(Spacer(1, 6))

    # Written Questions List (11-14)
    story.append(make_sec_header("PART B: SUBJECTIVE & THOUGHT-BASED QUESTIONS (11 to 14)"))
    wr_table_data = [
        [
            Paragraph("<b>#</b>", cell_label),
            Paragraph("<b>Question & Parent Response</b>", cell_label),
            Paragraph("<b>Evaluator Score & Feedback</b>", cell_label)
        ]
    ]
    for q in quiz.get("written_questions", []):
        ans_text = q.get('answer_text') or 'No answer submitted'
        wr_table_data.append([
            Paragraph(str(q.get("id")), cell_val_bold),
            Paragraph(f"<b>{q.get('question_en', '')}</b><br/><font color='#475569'>{q.get('question_gu', '')}</font><br/><font color='#1E3A8A'><b>Response:</b> {ans_text}</font>", cell_val),
            Paragraph(f"<b>Marks:</b> {q.get('marks_earned', 0)} / {q.get('max_marks', 5)}<br/><b>Notes:</b> {q.get('ai_notes') or '-'}", cell_val)
        ])
    
    wr_tbl = Table(wr_table_data, colWidths=[20, 350, 153])
    wr_tbl.setStyle(TableStyle([
        ('GRID', (0,0), (-1,-1), 0.5, border_color),
        ('BACKGROUND', (0,0), (-1,0), bg_header),
        ('VALIGN', (0,0), (-1,-1), 'TOP'),
        ('TOPPADDING', (0,0), (-1,-1), 2.5),
        ('BOTTOMPADDING', (0,0), (-1,-1), 2.5),
    ]))
    story.append(wr_tbl)

    # Build ReportLab Document (Pages 1 & 2)
    doc.build(story, canvasmaker=NumberedCanvas)
    pdf_buffer.seek(0)

    # -------------------------------------------------------------
    # PAGE 3+: DEDICATED FULL A4 PAGE PER UPLOADED DOCUMENT
    # -------------------------------------------------------------
    doc_labels = [
        ("Student Photograph", docs.get("photograph_child")),
        ("Birth Certificate", docs.get("birth_certificate")),
        ("Child Aadhar Card", docs.get("child_aadhar_card")),
        ("Parent Aadhar Card", docs.get("parent_aadhar_card")),
        ("Health & Fitness Certificate", docs.get("health_certificate")),
        ("Eye Checkup Certificate", docs.get("eye_checkup_certificate")),
        ("Caste Certificate", docs.get("caste_certificate")),
        ("Previous School Report Card", docs.get("previous_report_card")),
        ("Alak Malak Certificate", docs.get("alak_malak_certificate"))
    ]

    # Combine Base PDF with Document Pages using PyPDF2
    writer = PyPDF2.PdfWriter()
    base_reader = PyPDF2.PdfReader(pdf_buffer)
    for p in base_reader.pages:
        writer.add_page(p)

    for label, url in doc_labels:
        if not url:
            continue
        try:
            file_bytes, content_type = download_file_bytes(url)
            if not file_bytes:
                continue

            # Case A: If uploaded file is a PDF, import pages directly
            if (content_type and 'pdf' in content_type) or url.lower().endswith('.pdf'):
                try:
                    doc_reader = PyPDF2.PdfReader(io.BytesIO(file_bytes))
                    for doc_page in doc_reader.pages:
                        writer.add_page(doc_page)
                    continue
                except Exception as pe:
                    logger.warning(f"Could not read embedded PDF {url}: {pe}")

            # Case B: If uploaded file is an Image, render on dedicated full A4 page
            img_buffer = io.BytesIO()
            img_can = canvas.Canvas(img_buffer, pagesize=A4)
            page_w, page_h = A4

            # Top header bar
            img_can.setFont("Helvetica-Bold", 12)
            img_can.setFillColor(colors.HexColor("#1E3A8A"))
            img_can.drawString(36, page_h - 35, f"SHREYAS FOUNDATION — {label.upper()}")
            img_can.setFont("Helvetica", 8)
            img_can.setFillColor(colors.HexColor("#64748B"))
            img_can.drawString(36, page_h - 48, f"Candidate: {sp.get('first_name', '')} {sp.get('surname', '')} | App No: {app_no}")
            img_can.setStrokeColor(colors.HexColor("#CBD5E1"))
            img_can.setLineWidth(0.5)
            img_can.line(36, page_h - 54, page_w - 36, page_h - 54)

            # Footer
            img_can.line(36, 35, page_w - 36, 35)
            img_can.drawString(36, 24, "Verified Official Document Attachment")

            # Fit image within printable area (margins 36 left/right, 60 top, 45 bottom)
            max_img_w = page_w - 72   # 523 pt
            max_img_h = page_h - 110  # 731 pt

            pil_img = PILImage.open(io.BytesIO(file_bytes))
            if pil_img.mode in ("RGBA", "P"):
                pil_img = pil_img.convert("RGB")
            
            orig_w, orig_h = pil_img.size
            scale = min(max_img_w / orig_w, max_img_h / orig_h)
            draw_w = orig_w * scale
            draw_h = orig_h * scale
            x_pos = 36 + (max_img_w - draw_w) / 2
            y_pos = 45 + (max_img_h - draw_h) / 2

            # Save clean JPEG for canvas
            clean_img_io = io.BytesIO()
            pil_img.save(clean_img_io, format='JPEG', quality=90)
            clean_img_io.seek(0)

            img_can.drawImage(RLImage(clean_img_io, width=draw_w, height=draw_h).filename, x_pos, y_pos, width=draw_w, height=draw_h)
            img_can.showPage()
            img_can.save()
            img_buffer.seek(0)

            doc_reader = PyPDF2.PdfReader(img_buffer)
            for doc_page in doc_reader.pages:
                writer.add_page(doc_page)

        except Exception as e:
            logger.error(f"Error appending document {label} ({url}): {e}")

    final_pdf_stream = io.BytesIO()
    writer.write(final_pdf_stream)
    final_pdf_stream.seek(0)
    return final_pdf_stream.getvalue()
