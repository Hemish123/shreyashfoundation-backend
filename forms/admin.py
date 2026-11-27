from django import forms
from django.contrib import admin
from django.utils.html import format_html
from .models import QuizSubmission, AdmissionApplication, AdminUser, AuditLog, SurveyResponse, SurveyInvite


class AdminUserForm(forms.ModelForm):
    pin = forms.CharField(
        widget=forms.PasswordInput(render_value=False),
        required=False,
        help_text="Enter a 4-6 digit numeric PIN. (Leave blank if you don't want to change existing PIN)"
    )

    class Meta:
        model = AdminUser
        fields = ('username', 'full_name', 'email', 'role', 'is_active', 'pin')

    def clean_pin(self):
        pin = self.cleaned_data.get('pin')
        if not self.instance.pk and not pin:
            raise forms.ValidationError("PIN is required when creating a new Admin User.")
        if pin:
            if not pin.isdigit():
                raise forms.ValidationError("PIN must contain digits only.")
            if len(pin) < 4 or len(pin) > 6:
                raise forms.ValidationError("PIN must be between 4 and 6 digits.")
        return pin

    def save(self, commit=True):
        user = super().save(commit=False)
        pin = self.cleaned_data.get('pin')
        if pin:
            user.set_pin(pin)
        if commit:
            user.save()
        return user


@admin.register(AdminUser)
class AdminUserAdmin(admin.ModelAdmin):
    form = AdminUserForm
    list_display = ('id', 'username', 'full_name', 'email', 'role', 'is_active', 'has_pin_display', 'created_at', 'updated_at')
    list_filter = ('role', 'is_active', 'created_at')
    search_fields = ('username', 'full_name', 'email')
    readonly_fields = ('pin_hash', 'otp_code', 'otp_expires_at', 'created_at', 'updated_at')
    fields = ('username', 'full_name', 'email', 'role', 'is_active', 'pin', 'pin_hash', 'otp_code', 'otp_expires_at', 'created_at', 'updated_at')

    def has_pin_display(self, obj):
        if obj.pin_hash:
            return format_html('<span style="color: green; font-weight: bold;">&#10004; Set</span>')
        return format_html('<span style="color: red; font-weight: bold;">&#10008; Not Set</span>')
    has_pin_display.short_description = "PIN Status"


@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    list_display = ('id', 'actor_username', 'actor_email', 'actor_role', 'method', 'endpoint', 'response_status', 'ip_address', 'created_at')
    list_filter = ('method', 'response_status', 'actor_role', 'created_at')
    search_fields = ('actor_username', 'actor_email', 'endpoint', 'ip_address')
    readonly_fields = ('actor_username', 'actor_email', 'actor_role', 'method', 'endpoint', 'query_params', 'request_body', 'response_status', 'ip_address', 'user_agent', 'created_at')

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(QuizSubmission)
class QuizSubmissionAdmin(admin.ModelAdmin):
    list_display = (
        'id',
        'child_name',
        'surname',
        'parent_name',
        'standard',
        'medium',
        'vibhag',
        'whatsapp_number',
        'score',
        'tag',
        'qa_status',
        'payment_status',
        'added_by_display',
        'whatsapp_link_sent',
        'repeated_badge_display',
        'created_at'
    )
    list_filter = ('created_by', 'vibhag', 'standard', 'medium', 'payment_status', 'qa_status', 'tag', 'whatsapp_link_sent', 'created_at')
    search_fields = ('child_name', 'surname', 'parent_name', 'whatsapp_number', 'email', 'created_by__username', 'created_by__full_name')
    readonly_fields = ('created_at', 'quiz_completed_at', 'created_by')

    def added_by_display(self, obj):
        if obj.created_by:
            return obj.created_by.full_name or obj.created_by.username
        return "Online / Self"
    added_by_display.short_description = "Added By"

    def repeated_badge_display(self, obj):
        clean = "".join(filter(str.isdigit, str(obj.whatsapp_number)))
        phone_10 = clean[-10:] if len(clean) >= 10 else clean
        count = QuizSubmission.objects.filter(
            whatsapp_number__endswith=phone_10,
            child_name__iexact=obj.child_name.strip()
        ).count()
        if count > 1:
            return format_html('<span style="color: #b78103; font-weight: bold; background: #fff3cd; padding: 2px 6px; border-radius: 4px;">Repeated Enquiry</span>')
        return format_html('<span style="color: #198754;">Unique</span>')
    repeated_badge_display.short_description = "Enquiry Badge"


@admin.register(AdmissionApplication)
class AdmissionApplicationAdmin(admin.ModelAdmin):
    list_display = (
        'application_number',
        'submission_id_display',
        'child_first_name',
        'child_surname',
        'standard',
        'medium',
        'vibhag',
        'interaction_date',
        'interaction_slot_no',
        'interaction_time',
        'interaction_whatsapp_sent',
        'father_name',
        'father_whatsapp',
        'created_at'
    )
    search_fields = (
        'application_number',
        'submission__id',
        'child_first_name',
        'child_surname',
        'father_name',
        'mother_name',
        'father_whatsapp',
        'mother_whatsapp',
        'father_email'
    )
    list_filter = (
        'interaction_date',
        'interaction_slot_no',
        'interaction_whatsapp_sent',
        'vibhag',
        'standard',
        'medium',
        'agreed_to_rules',
        'created_at'
    )
    readonly_fields = (
        'application_number',
        'submission_id_display',
        'created_at',
        'form_submitted_at',
        'terms_agreed_timestamp',
        'interaction_date',
        'interaction_slot_no',
        'interaction_time',
        'interaction_venue',
        'interaction_whatsapp_sent'
    )

    def submission_id_display(self, obj):
        if obj.submission_id:
            return format_html('<strong>#{}</strong>', obj.submission_id)
        return "-"
    submission_id_display.short_description = "Submission ID"


@admin.register(SurveyResponse)
class SurveyResponseAdmin(admin.ModelAdmin):
    list_display = ('id', 'language', 'department_value', 'created_at')
    list_filter = ('language', 'department_value', 'created_at')
    search_fields = ('department_value',)
    readonly_fields = ('language', 'department_value', 'answers', 'created_at')


@admin.register(SurveyInvite)
class SurveyInviteAdmin(admin.ModelAdmin):
    list_display = ('id', 'name', 'phone', 'send_count', 'whatsapp_status', 'sent_at', 'last_sent_at')
    list_filter = ('whatsapp_status', 'sent_at', 'last_sent_at')
    search_fields = ('name', 'phone')
    readonly_fields = ('sent_at',)

