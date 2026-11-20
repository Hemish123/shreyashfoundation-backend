from django.urls import path
from .views import (
    SubmitQuizView,
    VerifyPaymentAndSendWhatsAppView,
    AdmissionFormEligibilityCheckView,
    AdmissionFormAutoSaveDraftView,
    AdmissionApplicationView,
    AdminUserManagementView,
    AdminLoginView,
    AdminVerifyLoginOTPView,
    ForgotPinRequestOTPView,
    ForgotPinVerifyAndResetView,
    AdminInteractionCalendarView,
    AdminExportDataView,
    AdminAuditLogView,
    AdminEvaluationAssessmentView,
    AdminMeritBoardView,
    AdminMeritCancellationView,
    ParentConfirmInteractionSlotView,
    AdminPrintPhysicalFormView,
    AdminAcademicYearRollView,
    AdminProxyDownloadView,
    WhatsAppWebhookView
)

urlpatterns = [
    # Admin User Management (Admin adds/edits/deletes staff/users role-wise)
    path('admin/users/', AdminUserManagementView.as_view(), name='admin-users'),
    path('admin/users/<int:user_id>/', AdminUserManagementView.as_view(), name='admin-user-detail'),

    # Admin Login (2-Step Verification)
    path('admin/login/', AdminLoginView.as_view(), name='admin-login'),
    path('admin/verify-login-otp/', AdminVerifyLoginOTPView.as_view(), name='admin-verify-login-otp'),
    path('admin/forgot-pin/request-otp/', ForgotPinRequestOTPView.as_view(), name='admin-forgot-pin-otp'),
    path('admin/forgot-pin/reset/', ForgotPinVerifyAndResetView.as_view(), name='admin-forgot-pin-reset'),

    # Step 1: Interaction Calendar for Admin
    path('admin/interaction-calendar/', AdminInteractionCalendarView.as_view(), name='admin-interaction-calendar'),

    # Step 2 & 3: Digital Assessment Form for Academicians & Educators
    path('admin/evaluation/<int:application_id>/', AdminEvaluationAssessmentView.as_view(), name='admin-evaluation-assessment'),

    # Step 5 & 6: Merit Board, Rating & Cutoff (First 80 selected, waitlist rollover on cancellation)
    path('admin/merit-board/', AdminMeritBoardView.as_view(), name='admin-merit-board'),
    path('admin/merit-board/cancel/<int:application_id>/', AdminMeritCancellationView.as_view(), name='admin-merit-cancel'),

    # Step 7: Print Physical Form Layout
    path('admin/applications/<int:application_id>/print/', AdminPrintPhysicalFormView.as_view(), name='admin-print-application'),
    path('admin/applications/print-bulk/', AdminPrintPhysicalFormView.as_view(), name='admin-print-bulk'),

    # Step 8: Academic Year Archival & Rollover
    path('admin/academic-year/rollover/', AdminAcademicYearRollView.as_view(), name='admin-academic-year-roll'),

    # Parent Slot Confirmation (via WhatsApp or Portal)
    path('admission-form/confirm-slot/', ParentConfirmInteractionSlotView.as_view(), name='parent-confirm-slot'),

    # Export Data API (Excel & CSV for Enquiries, Submitted Apps, Pending Forms)
    path('admin/export/', AdminExportDataView.as_view(), name='admin-export'),

    # Admin Audit Logs (Only accessible by Admin)
    path('admin/audit-logs/', AdminAuditLogView.as_view(), name='admin-audit-logs'),

    # Admin Proxy File Download (Resolves Azure Blob CORS on direct downloads)
    path('admin/download-file/', AdminProxyDownloadView.as_view(), name='admin-download-file'),

    # Enquiries & Quiz
    path('submit-quiz/', SubmitQuizView.as_view(), name='submit-quiz'),
    path('enquiries/', SubmitQuizView.as_view(), name='enquiries'),
    path('add-enquiry/', SubmitQuizView.as_view(), name='add-enquiry'),
    path('verify-payment/', VerifyPaymentAndSendWhatsAppView.as_view(), name='verify-payment'),

    # Admission Form & Auto-save Draft
    path('admission-form/check-eligibility/', AdmissionFormEligibilityCheckView.as_view(), name='check-form-eligibility'),
    path('admission-form/autosave/', AdmissionFormAutoSaveDraftView.as_view(), name='admission-form-autosave'),
    path('admission-form/', AdmissionApplicationView.as_view(), name='admission-form'),

    # WhatsApp SaaS & Meta Two-Way Webhook (Single Universal URL)
    path('whatsapp/webhook/', WhatsAppWebhookView.as_view(), name='whatsapp-webhook'),
]
