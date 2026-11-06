from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.parsers import MultiPartParser, FormParser, JSONParser
from django.shortcuts import get_object_or_404

from .models import QuizSubmission, AdmissionApplication
from .questions_data import ALIGNED_ANSWERS
from .serializers import SubmitQuizSerializer, AdmissionApplicationSerializer
from .whatsapp_service import generate_whatsapp_link
from .whatsapp_cloud import send_whatsapp_cloud_api

def get_performance_tag(score):
    if score == 7:
        return "Outstanding"
    elif score == 6:
        return "Excellent"
    elif score == 5:
        return "Very Good"
    elif score == 4:
        return "Good"
    else:
        return "Needs Improvement"


class SubmitQuizView(APIView):
    """
    Submits user answers, calculates score & assigns tag:
    - If score >= 4: is_eligible = True. Proceeds to payment verification.
    - WhatsApp link is NOT sent here yet, it will only be sent once payment is received!
    """
    def post(self, request):
        serializer = SubmitQuizSerializer(data=request.data)
        if not serializer.is_valid():
            return Response({
                "status": "error",
                "errors": serializer.errors
            }, status=status.HTTP_400_BAD_REQUEST)

        data = serializer.validated_data
        parent_name = data.get("parent_name", "")
        child_name = data.get("child_name", "")
        vibhag = data.get("vibhag", "Shishu")
        whatsapp_number = data.get("whatsapp_number", "")
        email = data.get("email", "")
        answers = data.get("answers", {})

        score = 0
        breakdown = {}

        for q_id, correct_ans in ALIGNED_ANSWERS.items():
            user_ans = answers.get(str(q_id)) or answers.get(q_id)
            if user_ans and str(user_ans).strip().upper() == correct_ans:
                score += 1
                breakdown[q_id] = {"selected": user_ans, "matched": True}
            else:
                breakdown[q_id] = {"selected": user_ans, "matched": False}

        is_eligible = score >= 4
        tag = get_performance_tag(score)

        submission = QuizSubmission.objects.create(
            parent_name=parent_name,
            child_name=child_name,
            vibhag=vibhag,
            whatsapp_number=whatsapp_number,
            email=email,
            answers=answers,
            score=score,
            tag=tag,
            is_eligible=is_eligible,
            payment_status='PENDING'
        )

        if is_eligible:
            message = (
                f"Congratulations! You scored {score}/7 ({tag}) for Vibhag: {vibhag}. "
                "Please proceed to fee payment. The admission form link will be sent to your WhatsApp once payment is received."
            )
        else:
            message = (
                f"You have scored {score}/7 ({tag}). Minimum 4 aligned answers are required to be eligible for admission."
            )

        return Response({
            "status": "success",
            "submission_id": submission.id,
            "vibhag": vibhag,
            "score": score,
            "total": len(ALIGNED_ANSWERS),
            "tag": tag,
            "can_fill_form": is_eligible,
            "payment_status": submission.payment_status,
            "requires_payment": is_eligible,
            "whatsapp_link_sent": False,
            "message": message,
            "breakdown": breakdown
        }, status=status.HTTP_200_OK)


class VerifyPaymentAndSendWhatsAppView(APIView):
    """
    Called after payment gateway callback or manual confirmation.
    POST /api/verify-payment/
    Body:
    {
        "submission_id": 1,
        "payment_status": "RECEIVED",  // or "FAILED"
        "transaction_id": "TXN123456",
        "frontend_base_url": "http://localhost:3000" // optional
    }
    If payment_status == 'RECEIVED' and score >= 4:
       -> Sends WhatsApp message with form link!
    If payment_status != 'RECEIVED':
       -> WhatsApp link is NOT sent.
    """
    def post(self, request):
        submission_id = request.data.get("submission_id")
        payment_status_input = request.data.get("payment_status", "PENDING").upper()
        transaction_id = request.data.get("transaction_id", "")
        frontend_base_url = request.data.get("frontend_base_url", "")

        if not submission_id:
            return Response({
                "status": "error",
                "message": "submission_id is required."
            }, status=status.HTTP_400_BAD_REQUEST)

        submission = get_object_or_404(QuizSubmission, id=submission_id)

        # Update payment transaction info
        submission.payment_transaction_id = transaction_id

        if not submission.is_eligible:
            submission.payment_status = "FAILED"
            submission.save()
            return Response({
                "status": "error",
                "message": "Candidate is not eligible based on quiz score. Payment cannot unlock form."
            }, status=status.HTTP_400_BAD_REQUEST)

        if payment_status_input != "RECEIVED":
            submission.payment_status = payment_status_input
            submission.save()
            return Response({
                "status": "error",
                "submission_id": submission.id,
                "payment_status": submission.payment_status,
                "whatsapp_link_sent": False,
                "message": "Payment not received! Admission form link will not be sent to WhatsApp."
            }, status=status.HTTP_400_BAD_REQUEST)

        # Payment received successfully!
        submission.payment_status = "RECEIVED"

        # Build Form URL with submission_id
        base_url = frontend_base_url.rstrip("/") if frontend_base_url else request.build_absolute_uri('/')[:-1]
        form_direct_url = f"{base_url}/admission-form/?submission_id={submission.id}"

        wa_message = (
            f"Namaste {submission.parent_name or 'Parent'},\n\n"
            f"Your payment has been successfully received for Vibhag: {submission.vibhag}.\n"
            f"Quiz Score: {submission.score}/7 ({submission.tag})\n\n"
            f"Please click the link below to fill the Admission Application Form for {submission.child_name or 'your child'}:\n"
            f"{form_direct_url}\n\n"
            f"Best regards,\nShreyas Foundation"
        )

        whatsapp_click_url = generate_whatsapp_link(submission.whatsapp_number, wa_message)

        # Send Live WhatsApp Message
        wa_result = send_whatsapp_cloud_api(submission.whatsapp_number, wa_message)
        submission.whatsapp_link_sent = wa_result.get("success", False)
        submission.save()

        return Response({
            "status": "success",
            "submission_id": submission.id,
            "vibhag": submission.vibhag,
            "payment_status": "RECEIVED",
            "whatsapp_link_sent": submission.whatsapp_link_sent,
            "whatsapp_number": submission.whatsapp_number,
            "form_url": form_direct_url,
            "whatsapp_redirect_url": whatsapp_click_url,
            "message": f"Payment received! Admission form link sent to WhatsApp ({submission.whatsapp_number})."
        }, status=status.HTTP_200_OK)


class AdmissionFormEligibilityCheckView(APIView):
    """
    Check if a submission_id is eligible, paid, and hasn't already submitted before opening form.
    GET /api/admission-form/check-eligibility/?submission_id=1
    """
    def get(self, request):
        submission_id = request.query_params.get("submission_id")
        if not submission_id:
            return Response({
                "status": "error",
                "message": "submission_id query parameter is required."
            }, status=status.HTTP_400_BAD_REQUEST)

        submission = get_object_or_404(QuizSubmission, id=submission_id)

        # 1. Check Quiz Score
        if not submission.is_eligible:
            return Response({
                "status": "error",
                "can_fill_form": False,
                "score": submission.score,
                "tag": submission.tag,
                "message": "You scored less than 4 in the quiz. Admission form is locked."
            }, status=status.HTTP_403_FORBIDDEN)

        # 2. Check Payment Status
        if submission.payment_status != "RECEIVED":
            return Response({
                "status": "error",
                "can_fill_form": False,
                "payment_status": submission.payment_status,
                "message": "Payment is not yet received. Please complete fee payment to unlock the admission form."
            }, status=status.HTTP_402_PAYMENT_REQUIRED)

        # 3. Check if already submitted
        existing_application = AdmissionApplication.objects.filter(submission=submission).first()
        if existing_application:
            return Response({
                "status": "already_submitted",
                "can_fill_form": False,
                "already_submitted": True,
                "application_number": existing_application.application_number,
                "submitted_at": existing_application.created_at,
                "message": f"This admission form has already been submitted (Application No: {existing_application.application_number}). You cannot submit it again."
            }, status=status.HTTP_200_OK)

        return Response({
            "status": "success",
            "can_fill_form": True,
            "already_submitted": False,
            "submission_id": submission.id,
            "score": submission.score,
            "tag": submission.tag,
            "vibhag": submission.vibhag,
            "message": "Eligible to fill admission form.",
            "prefilled_data": {
                "vibhag": submission.vibhag,
                "parent_name": submission.parent_name,
                "child_name": submission.child_name,
                "whatsapp_number": submission.whatsapp_number,
                "email": submission.email,
                "is_locked_for_user": True
            }
        }, status=status.HTTP_200_OK)


class AdmissionApplicationView(APIView):
    """
    GET: List all submitted admission forms or a specific one with ?id=
    POST: Submit the completed admission form (Only allowed if score >= 4 AND payment_status == RECEIVED)
    """
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    def get(self, request):
        app_id = request.query_params.get("id")
        submission_id = request.query_params.get("submission_id")

        if app_id:
            application = get_object_or_404(AdmissionApplication, id=app_id)
            serializer = AdmissionApplicationSerializer(application)
            return Response({"status": "success", "data": serializer.data}, status=status.HTTP_200_OK)

        if submission_id:
            application = get_object_or_404(AdmissionApplication, submission_id=submission_id)
            serializer = AdmissionApplicationSerializer(application)
            return Response({"status": "success", "data": serializer.data}, status=status.HTTP_200_OK)

        applications = AdmissionApplication.objects.all().order_by("-created_at")
        serializer = AdmissionApplicationSerializer(applications, many=True)
        return Response({
            "status": "success",
            "total_applications": applications.count(),
            "data": serializer.data
        }, status=status.HTTP_200_OK)

    def post(self, request):
        submission_id = request.data.get("submission_id")
        if not submission_id:
            return Response({
                "status": "error",
                "message": "submission_id is required to verify quiz & payment eligibility."
            }, status=status.HTTP_400_BAD_REQUEST)

        submission = get_object_or_404(QuizSubmission, id=submission_id)

        # Check Quiz Eligibility
        if not submission.is_eligible:
            return Response({
                "status": "error",
                "message": "Not eligible to submit form! Your quiz score is less than 4."
            }, status=status.HTTP_403_FORBIDDEN)

        # Check Payment Status
        if submission.payment_status != "RECEIVED":
            return Response({
                "status": "error",
                "message": "Cannot submit form! Payment has not been received."
            }, status=status.HTTP_402_PAYMENT_REQUIRED)

        # Check if already submitted
        existing_app = AdmissionApplication.objects.filter(submission=submission).first()
        if existing_app:
            return Response({
                "status": "error",
                "can_fill_form": False,
                "already_submitted": True,
                "application_number": existing_app.application_number,
                "message": f"Form already submitted with Application No: {existing_app.application_number}. Multiple submissions are not allowed."
            }, status=status.HTTP_400_BAD_REQUEST)

        serializer = AdmissionApplicationSerializer(data=request.data, context={'request': request})
        if serializer.is_valid():
            application = serializer.save(submission=submission)
            return Response({
                "status": "success",
                "message": "Admission form submitted and stored successfully!",
                "application_id": application.id,
                "application_number": application.application_number,
                "vibhag": application.vibhag,
                "data": serializer.data
            }, status=status.HTTP_201_CREATED)

        return Response({
            "status": "error",
            "errors": serializer.errors
        }, status=status.HTTP_400_BAD_REQUEST)
