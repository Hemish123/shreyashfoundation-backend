from django.contrib import admin
from .models import QuizSubmission, AdmissionApplication

@admin.register(QuizSubmission)
class QuizSubmissionAdmin(admin.ModelAdmin):
    list_display = ('id', 'parent_name', 'child_name', 'vibhag', 'whatsapp_number', 'score', 'tag', 'payment_status', 'is_eligible', 'whatsapp_link_sent', 'created_at')
    list_filter = ('vibhag', 'payment_status', 'tag', 'is_eligible', 'whatsapp_link_sent', 'created_at')
    search_fields = ('parent_name', 'child_name', 'whatsapp_number', 'email')

@admin.register(AdmissionApplication)
class AdmissionApplicationAdmin(admin.ModelAdmin):
    list_display = ('application_number', 'vibhag', 'child_first_name', 'child_surname', 'father_name', 'mother_name', 'created_at')
    search_fields = ('application_number', 'child_first_name', 'child_surname', 'father_name', 'mother_name')
    list_filter = ('vibhag', 'created_at', 'agreed_to_rules')
