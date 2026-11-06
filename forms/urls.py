from django.urls import path
from .views import (
    SubmitQuizView,
    VerifyPaymentAndSendWhatsAppView,
    AdmissionFormEligibilityCheckView,
    AdmissionApplicationView
)

urlpatterns = [
    path('submit-quiz/', SubmitQuizView.as_view(), name='submit-quiz'),
    path('verify-payment/', VerifyPaymentAndSendWhatsAppView.as_view(), name='verify-payment'),
    path('admission-form/check-eligibility/', AdmissionFormEligibilityCheckView.as_view(), name='check-form-eligibility'),
    path('admission-form/', AdmissionApplicationView.as_view(), name='admission-form'),
]
