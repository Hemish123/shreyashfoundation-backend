from django.db import models

class QuizSubmission(models.Model):
    VIBHAG_CHOICES = [
        ('Shishu', 'Shishu (શિશુ)'),
        ('Arun', 'Arun (અરુણ)'),
        ('Kesari', 'Kesari (કેસરી)'),
        ('Kishore', 'Kishore (કિશોર)'),
    ]

    parent_name = models.CharField(max_length=255, blank=True)
    child_name = models.CharField(max_length=255, blank=True)
    whatsapp_number = models.CharField(max_length=20, blank=True, help_text="WhatsApp number to receive the form link")
    email = models.EmailField(blank=True, help_text="User email address")
    vibhag = models.CharField(
        max_length=20,
        choices=VIBHAG_CHOICES,
        default='Shishu',
        help_text="Vibhag: Shishu / Arun / Kesari / Kishore (Fixed)"
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
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.parent_name or 'Anonymous'} ({self.vibhag}) - Score: {self.score}/7 ({self.tag}) - Pay: {self.payment_status}"


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

    created_at = models.DateTimeField(auto_now_add=True)

    def save(self, *args, **kwargs):
        if not self.application_number:
            import uuid
            self.application_number = f"SF-{uuid.uuid4().hex[:8].upper()}"
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.application_number} - {self.child_first_name} {self.child_surname}"
