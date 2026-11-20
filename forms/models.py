from django.db import models

class QuizSubmission(models.Model):
    STANDARD_CHOICES = [
        ('Playgroup', 'Playgroup'),
        ('Nursery', 'Nursery'),
        ('Jr.KG', 'Jr.KG'),
        ('Sr.KG', 'Sr.KG'),
        ('1', 'Standard 1'),
        ('2', 'Standard 2'),
        ('3', 'Standard 3'),
        ('4', 'Standard 4'),
        ('5', 'Standard 5'),
        ('6', 'Standard 6'),
        ('7', 'Standard 7'),
        ('8', 'Standard 8'),
        ('9', 'Standard 9'),
        ('10', 'Standard 10'),
        ('11-Science', 'Standard 11 (Science)'),
        ('11-Commerce', 'Standard 11 (Commerce)'),
        ('11-Arts', 'Standard 11 (Arts)'),
        ('12-Science', 'Standard 12 (Science)'),
        ('12-Commerce', 'Standard 12 (Commerce)'),
        ('12-Arts', 'Standard 12 (Arts)'),
    ]

    VIBHAG_CHOICES = [
        ('Shishu', 'Shishu (શિશુ)'),
        ('Arun', 'Arun (અરુણ)'),
        ('Kesari', 'Kesari (કેસરી)'),
        ('Kishore', 'Kishore (કિશોર)'),
    ]

    standard = models.CharField(
        max_length=50,
        choices=STANDARD_CHOICES,
        default='Nursery',
        help_text="Standard (Nursery to 12)"
    )
    medium = models.CharField(
        max_length=20,
        choices=[('Gujarati', 'Gujarati'), ('English', 'English')],
        default='Gujarati',
        help_text="Medium of Instruction (Gujarati / English)"
    )

    parent_name = models.CharField(max_length=255, blank=True, help_text="Parent Name / Father Name")
    child_name = models.CharField(max_length=255, blank=True, help_text="Student Name")
    surname = models.CharField(max_length=100, blank=True, help_text="SURNAME (Mandatory)")
    whatsapp_number = models.CharField(max_length=20, blank=True, help_text="WhatsApp number to receive the form link")
    email = models.EmailField(blank=True, help_text="User email address")
    vibhag = models.CharField(
        max_length=20,
        choices=VIBHAG_CHOICES,
        default='Shishu',
        help_text="Vibhag: Shishu / Arun / Kesari / Kishore"
    )
    language = models.CharField(
        max_length=10,
        choices=[('en', 'English'), ('gu', 'Gujarati')],
        default='en',
        blank=True
    )
    answers = models.JSONField(default=dict, blank=True, help_text="User submitted answers as {q_id: selected_option}")
    score = models.IntegerField(default=0)
    tag = models.CharField(max_length=50, blank=True, default="")
    is_eligible = models.BooleanField(default=False)
    qa_status = models.CharField(
        max_length=20,
        choices=[('PENDING', 'Pending'), ('PASSED', 'Passed'), ('FAILED', 'Failed')],
        default='PENDING',
        help_text="QA status: PENDING (not attended yet) / PASSED (score >= 4) / FAILED (score < 4)"
    )
    
    # Payment status tracking
    payment_status = models.CharField(
        max_length=20,
        choices=[('PENDING', 'Pending'), ('RECEIVED', 'Received'), ('FAILED', 'Failed')],
        default='PENDING'
    )
    payment_transaction_id = models.CharField(max_length=100, blank=True)
    whatsapp_link_sent = models.BooleanField(default=False)
    reminder_sent_at = models.DateTimeField(null=True, blank=True, help_text="Timestamp when one-time 7:00 AM WhatsApp reminder was sent")
    quiz_completed_at = models.DateTimeField(null=True, blank=True, help_text="Timestamp when parent completed the quiz")

    # Auto-save Drafts (Quiz questionnaire & Admission form)
    quiz_draft_answers = models.JSONField(default=dict, blank=True, help_text="Auto-saved draft answers for parental questionnaire")
    admission_form_draft = models.JSONField(default=dict, blank=True, help_text="Auto-saved draft state for admission form")
    last_draft_saved_at = models.DateTimeField(null=True, blank=True, help_text="Timestamp when draft was last autosaved")

    created_by = models.ForeignKey(
        'AdminUser',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_enquiries",
        help_text="Admin/Staff user who added this enquiry"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"[ID: #{self.id}] {self.child_name or self.parent_name or 'Anonymous'} ({self.vibhag}) - Score: {self.score}/7 ({self.tag}) - Pay: {self.payment_status}"


class AdmissionApplication(models.Model):
    submission = models.OneToOneField(
        QuizSubmission,
        on_delete=models.CASCADE,
        related_name="admission_application",
        null=True,
        blank=True
    )
    application_number = models.CharField(max_length=50, unique=True, blank=True)
    date = models.DateField(null=True, blank=True)
    standard = models.CharField(max_length=50, blank=True, default='Nursery')
    medium = models.CharField(max_length=20, blank=True, default='Gujarati')
    vibhag = models.CharField(
        max_length=20,
        choices=QuizSubmission.VIBHAG_CHOICES,
        default='Shishu',
        help_text="Fixed from Quiz Selection: Shishu / Arun / Kesari / Kishore"
    )

    # 1. Child Details
    child_surname = models.CharField(max_length=100, blank=True)
    child_first_name = models.CharField(max_length=100)
    child_father_name = models.CharField(max_length=100, blank=True)

    # 2. Mother's Details
    has_parents = models.BooleanField(
        default=True,
        help_text="True if parents exist, False if orphan/raised by Guardian"
    )
    mother_surname = models.CharField(max_length=100, blank=True)
    mother_name = models.CharField(max_length=100, blank=True)
    mother_father_or_husband_name = models.CharField(max_length=100, blank=True)
    mother_address_residence = models.TextField(blank=True)
    mother_address_office = models.TextField(blank=True)
    mother_phone_residence = models.CharField(max_length=20, blank=True)
    mother_phone_office = models.CharField(max_length=20, blank=True)
    mother_whatsapp = models.CharField(max_length=20, blank=True)
    mother_email = models.EmailField(blank=True)
    mother_nationality = models.CharField(max_length=50, blank=True)
    mother_education = models.CharField(max_length=150, blank=True)
    mother_occupation = models.CharField(max_length=150, blank=True)
    mother_annual_income = models.CharField(max_length=50, blank=True)

    # 3. Father's Details
    father_surname = models.CharField(max_length=100, blank=True)
    father_name = models.CharField(max_length=100, blank=True)
    father_father_name = models.CharField(max_length=100, blank=True)
    father_address_residence = models.TextField(blank=True)
    father_address_office = models.TextField(blank=True)
    father_phone_residence = models.CharField(max_length=20, blank=True)
    father_phone_office = models.CharField(max_length=20, blank=True)
    father_whatsapp = models.CharField(max_length=20, blank=True)
    father_email = models.EmailField(blank=True)
    father_nationality = models.CharField(max_length=50, blank=True)
    father_education = models.CharField(max_length=150, blank=True)
    father_occupation = models.CharField(max_length=150, blank=True)
    father_annual_income = models.CharField(max_length=50, blank=True)

    # 4. Guardian's Details (Optional)
    guardian_surname = models.CharField(max_length=100, blank=True)
    guardian_name = models.CharField(max_length=100, blank=True)
    guardian_father_name = models.CharField(max_length=100, blank=True)
    guardian_address_residence = models.TextField(blank=True)
    guardian_address_office = models.TextField(blank=True)
    guardian_phone_residence = models.CharField(max_length=20, blank=True)
    guardian_phone_office = models.CharField(max_length=20, blank=True)
    guardian_whatsapp = models.CharField(max_length=20, blank=True)
    guardian_email = models.EmailField(blank=True)
    guardian_nationality = models.CharField(max_length=50, blank=True)
    guardian_education = models.CharField(max_length=150, blank=True)
    guardian_occupation = models.CharField(max_length=150, blank=True)
    guardian_annual_income = models.CharField(max_length=50, blank=True)
    guardians = models.JSONField(
        default=list,
        blank=True,
        help_text="Stores multiple guardians as a list of dictionaries"
    )

    # 5. Birth Place
    birth_place_city = models.CharField(max_length=100, blank=True)
    birth_place_taluka = models.CharField(max_length=100, blank=True)
    birth_place_district = models.CharField(max_length=100, blank=True)

    # 6. Date of Birth & Age
    date_of_birth = models.DateField(null=True, blank=True)
    present_age_year = models.IntegerField(null=True, blank=True)
    present_age_month = models.IntegerField(null=True, blank=True)
    present_age_day = models.IntegerField(null=True, blank=True)

    # 7. Developmental Milestones
    age_started_talking_year = models.CharField(max_length=20, blank=True)
    age_started_walking_year = models.CharField(max_length=20, blank=True)

    # 8 & 9. Language Details
    mother_tongue_mother = models.CharField(max_length=50, blank=True)
    mother_tongue_father = models.CharField(max_length=50, blank=True)
    languages_known = models.JSONField(
        default=dict,
        blank=True,
        help_text="Stores language proficiency for Mother and Father"
    )

    # Health
    child_height_cm = models.CharField(max_length=20, blank=True)
    child_weight_kg = models.CharField(max_length=20, blank=True)
    past_illness = models.TextField(blank=True)
    specific_care_needed = models.TextField(blank=True)

    # 10. Siblings in Shreyas
    has_sibling_in_shreyas = models.BooleanField(default=False)
    sibling_name = models.CharField(max_length=150, blank=True)
    sibling_class = models.CharField(max_length=50, blank=True)
    sibling_vikas_kendra_1_opted = models.CharField(max_length=10, blank=True)
    sibling_vikas_kendra_1_receipt = models.CharField(max_length=100, blank=True)
    sibling_vikas_kendra_2_opted = models.CharField(max_length=10, blank=True)
    sibling_vikas_kendra_2_receipt = models.CharField(max_length=100, blank=True)
    siblings = models.JSONField(
        default=list,
        blank=True,
        help_text="Stores multiple siblings in Shreyas: [{name, class, vk1_opted, vk1_receipt, vk2_opted, vk2_receipt, vk3_opted, vk3_receipt}, ...]"
    )

    # 11. Present School
    current_school_name = models.CharField(max_length=255, blank=True)
    current_school_place = models.CharField(max_length=100, blank=True)
    current_school_year = models.CharField(max_length=20, blank=True)
    current_school_std = models.CharField(max_length=50, blank=True)
    current_school_medium = models.CharField(max_length=50, blank=True)

    # 12 & 13. Declarations
    agrees_discourage_private_tuitions = models.BooleanField(default=False)
    open_to_natural_medium_schooling = models.BooleanField(default=False)

    # 14. Vikas Kendra Activities Selection
    vikas_kendra_1_activities = models.JSONField(default=list, blank=True, help_text="Opt any 5")
    vikas_kendra_2_activities = models.JSONField(default=list, blank=True, help_text="Opt any 3")
    vikas_kendra_3_activities = models.JSONField(default=list, blank=True, help_text="Opt multiple")

    # Terms & Conditions & Signatures (Page 4, 5, 11 of PDF)
    agreed_to_rules = models.BooleanField(
        default=False,
        help_text="Consent to adhere to Shreyas rules and regulations"
    )
    terms_agreed_date = models.DateField(null=True, blank=True)
    terms_agreed_timestamp = models.DateTimeField(null=True, blank=True, help_text="Timestamp when terms & conditions were accepted")
    mother_signature = models.ImageField(upload_to='signatures/mother/', null=True, blank=True)
    father_signature = models.ImageField(upload_to='signatures/father/', null=True, blank=True)
    guardian_signature = models.ImageField(upload_to='signatures/guardian/', null=True, blank=True)

    # Documents Upload (Required / Photo copies as per Page 1)
    photograph_child = models.ImageField(upload_to='documents/photographs/', null=True, blank=True, help_text="Child Passport Size Photograph")
    health_certificate = models.FileField(upload_to='documents/health_certificates/', null=True, blank=True, help_text="Health Certificate from Doctor")
    eye_checkup_certificate = models.FileField(upload_to='documents/eye_certificates/', null=True, blank=True, help_text="Eye Checkup Certificate")
    birth_certificate = models.FileField(upload_to='documents/birth_certificates/', null=True, blank=True, help_text="Birth Certificate (In English) Photo Copy")
    child_aadhar_card = models.FileField(upload_to='documents/child_aadhar/', null=True, blank=True, help_text="Child's Aadhar Card Photo Copy")
    caste_certificate = models.FileField(upload_to='documents/caste_certificates/', null=True, blank=True, help_text="Caste Certificate (if applicable) Photo Copy")
    parent_aadhar_card = models.FileField(upload_to='documents/parent_aadhar/', null=True, blank=True, help_text="Parent's Aadhar Card Photo Copy")
    previous_report_card = models.FileField(upload_to='documents/report_cards/', null=True, blank=True, help_text="Previous year's report card (For non-nursery applicants)")
    alak_malak_certificate = models.FileField(upload_to='documents/alak_malak/', null=True, blank=True, help_text="Alak Malak Certificate (For Nursery applicants)")

    # Interaction Slot Details (Auto allocated: 10 children/slot, 4 slots/day, Mon-Sat)
    interaction_date = models.DateField(null=True, blank=True, help_text="Scheduled interaction date")
    interaction_slot_no = models.IntegerField(null=True, blank=True, help_text="Slot number (1 to 4)")
    interaction_time = models.CharField(max_length=50, blank=True, help_text="e.g. 9:00 AM TO 10:00 AM")
    interaction_venue = models.CharField(max_length=150, default="Shreyas Foundation – Main Office", blank=True)
    interaction_whatsapp_sent = models.BooleanField(default=False)
    parent_confirmed_slot = models.BooleanField(default=False, help_text="Parent confirmed slot via WhatsApp/Portal")

    # Assessment & Scores (PART X + PART Y)
    # PART X (Total: 20 Marks)
    has_attended_alak_malak = models.BooleanField(
        default=False,
        help_text="Whether child attended Alak Malak (Yes/No)"
    )
    alak_malak_months = models.IntegerField(default=0, help_text="0, 6 (5 marks), or 12 (10 marks)")
    alak_malak_score = models.FloatField(default=0.0, help_text="0, 5, or 10")
    mcq_score_part_x = models.FloatField(default=0.0, help_text="Score out of 7 from Parent MCQ")
    sibling_opted_vk = models.BooleanField(default=False, help_text="Sibling opted for VK1/VK2/VK3 (+3 marks if receipt uploaded)")
    sibling_score = models.FloatField(default=0.0, help_text="0 or 3 marks")
    total_part_x_score = models.FloatField(default=0.0, help_text="Part X score out of 20")

    # PART Y (Total: 30 Marks)
    evaluator = models.ForeignKey(
        'AdminUser',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="evaluations_done",
        help_text="Educator/Academician who evaluated"
    )
    evaluator_m = models.CharField(max_length=100, blank=True, help_text="M assessment tag/notes")
    evaluator_mi = models.CharField(max_length=100, blank=True, help_text="MI assessment tag/notes")
    cognitive_assessment_1 = models.FloatField(default=0.0, help_text="Cognitive Part 1 (out of 5)")
    cognitive_assessment_2 = models.FloatField(default=0.0, help_text="Cognitive Part 2 (out of 5)")
    affective_assessment = models.FloatField(default=0.0, help_text="Affective (out of 10)")
    psychomotor_assessment = models.FloatField(default=0.0, help_text="Psychomotor (out of 10)")
    total_part_y_score = models.FloatField(default=0.0, help_text="Part Y score out of 30")

    # Aggregate & Merit
    final_aggregate_score = models.FloatField(default=0.0, help_text="Part X (20) + Part Y (30) = Total 50")
    merit_rank = models.IntegerField(null=True, blank=True, help_text="Rank within Medium x Standard")
    admission_status = models.CharField(
        max_length=20,
        choices=[
            ('SUBMITTED', 'Submitted'),
            ('SCHEDULED', 'Scheduled'),
            ('EVALUATED', 'Evaluated'),
            ('SELECTED', 'Selected'),
            ('WAITLISTED', 'Waitlisted'),
            ('CANCELLED', 'Cancelled'),
            ('REJECTED', 'Rejected'),
        ],
        default='SCHEDULED',
        help_text="First 80 are SELECTED; beyond cutoff are WAITLISTED; cancellations pass to next"
    )

    form_submitted_at = models.DateTimeField(null=True, blank=True, help_text="Timestamp when complete admission form was submitted")
    created_at = models.DateTimeField(auto_now_add=True)

    def save(self, *args, **kwargs):
        if not self.application_number:
            import uuid
            self.application_number = f"SF-{uuid.uuid4().hex[:8].upper()}"
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.application_number} - {self.child_first_name} {self.child_surname}"


class AdminUser(models.Model):
    """
    Flow A: Step 1 & 2
    Admin User with secure PIN and Role-based access.
    """
    ROLE_CHOICES = [
        ('ADMIN', 'Admin'),
        ('STAFF', 'Staff'),
        ('ACADEMICIAN', 'Academician'),
    ]

    username = models.CharField(max_length=100, unique=True, help_text="e.g. admin.shreyas")
    full_name = models.CharField(max_length=200, blank=True)
    email = models.EmailField(unique=True, help_text="Registered email for Forgot PIN OTP")
    pin_hash = models.CharField(max_length=256, help_text="Hashed PIN")
    role = models.CharField(max_length=30, choices=ROLE_CHOICES, default='ADMIN')
    is_active = models.BooleanField(default=True)
    
    # Forgot PIN OTP support
    otp_code = models.CharField(max_length=10, blank=True, null=True)
    otp_expires_at = models.DateTimeField(null=True, blank=True)
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def set_pin(self, raw_pin: str):
        from django.contrib.auth.hashers import make_password
        self.pin_hash = make_password(str(raw_pin))

    def check_pin(self, raw_pin: str) -> bool:
        from django.contrib.auth.hashers import check_password
        return check_password(str(raw_pin), self.pin_hash)

    def __str__(self):
        return f"{self.username} ({self.role})"


class AuditLog(models.Model):
    """
    Audit log capturing API actions across the system.
    Only accessible by Admin.
    """
    actor_username = models.CharField(max_length=150, blank=True, default="Anonymous")
    actor_email = models.CharField(max_length=150, blank=True, default="")
    actor_role = models.CharField(max_length=50, blank=True, default="GUEST")
    
    method = models.CharField(max_length=10)  # GET, POST, PATCH, DELETE
    endpoint = models.CharField(max_length=255)
    query_params = models.TextField(blank=True, default="")
    request_body = models.TextField(blank=True, default="")
    
    response_status = models.IntegerField(default=200)
    ip_address = models.CharField(max_length=50, blank=True)
    user_agent = models.CharField(max_length=255, blank=True)
    
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"[{self.created_at}] {self.actor_username} ({self.actor_role}) - {self.method} {self.endpoint} [{self.response_status}]"


