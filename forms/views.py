import logging
import json
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.parsers import MultiPartParser, FormParser, JSONParser
from django.shortcuts import get_object_or_404
from django.conf import settings

from .models import QuizSubmission, AdmissionApplication
from .questions_data import ALIGNED_ANSWERS
from .serializers import SubmitQuizSerializer, QuizSubmissionListSerializer, AdmissionApplicationSerializer
from .whatsapp_service import generate_whatsapp_link
from .whatsapp import send_whatsapp_template_message

logger = logging.getLogger(__name__)

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
    GET: List all quiz submissions (Enquiries) for Admin Dashboard
    POST: Submits user answers, calculates score & assigns tag:
    - If score >= 4: is_eligible = True. Proceeds to payment verification.
    """
    def get(self, request):
        """
        Fetch all enquiries / quiz submissions for Admin Dashboard.
        Supports query params:
        - ?id=<id>
        - ?is_eligible=true/false
        - ?vibhag=Shishu
        """
        sub_id = request.query_params.get("id")
        if sub_id:
            submission = get_object_or_404(QuizSubmission, id=sub_id)
            serializer = QuizSubmissionListSerializer(submission)
            return Response({"status": "success", "data": serializer.data}, status=status.HTTP_200_OK)

        queryset = QuizSubmission.objects.all().order_by("-created_at")

        # Optional filters
        vibhag = request.query_params.get("vibhag")
        if vibhag:
            queryset = queryset.filter(vibhag__iexact=vibhag)

        is_eligible = request.query_params.get("is_eligible")
        if is_eligible is not None:
            queryset = queryset.filter(is_eligible=(is_eligible.lower() == 'true'))

        payment_status = request.query_params.get("payment_status")
        if payment_status:
            queryset = queryset.filter(payment_status__iexact=payment_status)

        serializer = QuizSubmissionListSerializer(queryset, many=True)
        return Response({
            "status": "success",
            "total_enquiries": queryset.count(),
            "qa_passed": queryset.filter(qa_status="PASSED").count(),
            "qa_failed": queryset.filter(qa_status="FAILED").count(),
            "qa_pending": queryset.filter(qa_status="PENDING").count(),
            "fees_collected": queryset.filter(payment_status="RECEIVED").count(),
            "data": serializer.data
        }, status=status.HTTP_200_OK)

    def post(self, request):
        logger.info(f"[POST /api/submit-quiz/] Request Data: {request.data}")
        serializer = SubmitQuizSerializer(data=request.data)
        if not serializer.is_valid():
            logger.error(f"[POST /api/submit-quiz/] Validation Failed: {serializer.errors}")
            return Response({
                "status": "error",
                "errors": serializer.errors
            }, status=status.HTTP_400_BAD_REQUEST)

        data = serializer.validated_data
        submission_id = data.get("submission_id")
        parent_name = data.get("parent_name", "")
        child_name = data.get("child_name", "")
        vibhag = data.get("vibhag", "Shishu")
        language = data.get("language", "en") or "en"
        whatsapp_number = data.get("whatsapp_number", "")
        email = data.get("email", "")
        answers = data.get("answers", {})

        # If submission_id is provided, parent is submitting the quiz for an existing enquiry
        if submission_id:
            submission = QuizSubmission.objects.filter(id=submission_id).first()
            if not submission:
                return Response({
                    "status": "error",
                    "message": f"Enquiry with ID {submission_id} not found."
                }, status=status.HTTP_404_NOT_FOUND)

            # Check if already submitted admission form
            existing_app = AdmissionApplication.objects.filter(submission=submission).first()
            if existing_app:
                return Response({
                    "status": "error",
                    "already_submitted": True,
                    "application_number": existing_app.application_number,
                    "message": f"Admission form already submitted (App No: {existing_app.application_number})."
                }, status=status.HTTP_400_BAD_REQUEST)

            # Check if quiz has already been submitted for this enquiry
            if submission.qa_status in ["PASSED", "FAILED"]:
                return Response({
                    "status": "error",
                    "already_completed": True,
                    "submission_id": submission.id,
                    "score": submission.score,
                    "tag": submission.tag,
                    "message": f"Quiz has already been completed for this student with score {submission.score}/7 ({submission.tag}). Re-attempting the quiz is not allowed."
                }, status=status.HTTP_400_BAD_REQUEST)

            # Grade the submitted answers
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
            qa_status = "PASSED" if is_eligible else "FAILED"
            tag = get_performance_tag(score)

            submission.answers = answers
            submission.score = score
            submission.is_eligible = is_eligible
            submission.qa_status = qa_status
            submission.tag = tag
            submission.save()

            logger.info(f"[POST /api/submit-quiz/] Existing Submission {submission.id} graded: Score {score}/7 ({tag}), QA: {qa_status}")

            return Response({
                "status": "success",
                "submission_id": submission.id,
                "vibhag": submission.vibhag,
                "qa_status": qa_status,
                "score": score,
                "total": len(ALIGNED_ANSWERS),
                "tag": tag,
                "can_fill_form": True,
                "payment_status": submission.payment_status,
                "message": f"Quiz completed! Score: {score}/7 ({tag}). You can now proceed to fill the admission form.",
                "breakdown": breakdown
            }, status=status.HTTP_200_OK)

        # Otherwise, creating a new enquiry (Admin side)
        clean_phone = "".join(filter(str.isdigit, str(whatsapp_number)))
        clean_phone_10 = clean_phone[-10:] if len(clean_phone) >= 10 else clean_phone

        # 1. First check if an application has already been submitted for this child/phone
        existing_app = AdmissionApplication.objects.filter(
            submission__whatsapp_number__endswith=clean_phone_10,
            child_first_name__iexact=child_name.strip()
        ).first()

        if existing_app:
            logger.warning(f"[POST /api/submit-quiz/] Form already submitted (App No: {existing_app.application_number}) for child '{child_name}'.")
            return Response({
                "status": "error",
                "already_submitted": True,
                "submission_id": existing_app.submission_id,
                "application_number": existing_app.application_number,
                "message": f"Admission application for '{child_name}' has already been submitted (App No: {existing_app.application_number}). You cannot re-take the quiz."
            }, status=status.HTTP_400_BAD_REQUEST)

        # 2. Check if the child has already passed the quiz (is_eligible=True)
        existing_passed_quiz = QuizSubmission.objects.filter(
            whatsapp_number__endswith=clean_phone_10,
            child_name__iexact=child_name.strip(),
            is_eligible=True
        ).order_by('-id').first()

        if existing_passed_quiz:
            logger.warning(f"[POST /api/submit-quiz/] Child '{child_name}' already passed quiz (Submission {existing_passed_quiz.id}).")
            return Response({
                "status": "error",
                "already_passed": True,
                "submission_id": existing_passed_quiz.id,
                "score": existing_passed_quiz.score,
                "tag": existing_passed_quiz.tag,
                "message": f"Quiz has already been completed for '{child_name}' with score {existing_passed_quiz.score}/7 ({existing_passed_quiz.tag}). Please fill the admission form using the link sent to your WhatsApp."
            }, status=status.HTTP_400_BAD_REQUEST)

        # Determine if answers were submitted (QA attempted)
        has_answers = bool(answers and len(answers) > 0)
        
        score = 0
        breakdown = {}

        if has_answers:
            for q_id, correct_ans in ALIGNED_ANSWERS.items():
                user_ans = answers.get(str(q_id)) or answers.get(q_id)
                if user_ans and str(user_ans).strip().upper() == correct_ans:
                    score += 1
                    breakdown[q_id] = {"selected": user_ans, "matched": True}
                else:
                    breakdown[q_id] = {"selected": user_ans, "matched": False}

            is_eligible = score >= 4
            qa_status = "PASSED" if is_eligible else "FAILED"
            tag = get_performance_tag(score)
        else:
            # Enquiry created without taking QA yet (Pending QA)
            score = 0
            is_eligible = False
            qa_status = "PENDING"
            tag = "Pending QA"

        submission = QuizSubmission.objects.create(
            parent_name=parent_name,
            child_name=child_name,
            vibhag=vibhag,
            language=language,
            whatsapp_number=whatsapp_number,
            email=email,
            answers=answers,
            score=score,
            tag=tag,
            qa_status=qa_status,
            is_eligible=is_eligible,
            payment_status='RECEIVED'
        )

        logger.info(f"[POST /api/submit-quiz/] Created Submission ID: {submission.id}, QA Status: {qa_status}, Score: {score}/7 ({tag}), Payment: RECEIVED")

        if not has_answers:
            message = f"Enquiry created for {child_name}. QA is pending."
        elif is_eligible:
            message = (
                f"Congratulations! You scored {score}/7 ({tag}) for Vibhag: {vibhag}. "
                "You are eligible to fill the admission form."
            )
        else:
            message = (
                f"You have scored {score}/7 ({tag}). Minimum 4 aligned answers are required to be eligible for admission."
            )

        return Response({
            "status": "success",
            "submission_id": submission.id,
            "vibhag": vibhag,
            "qa_status": qa_status,
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
    """
    def post(self, request):
        logger.info(f"[POST /api/verify-payment/] Request Data: {request.data}")
        submission_id = request.data.get("submission_id")
        payment_status_input = str(request.data.get("payment_status", "PENDING")).upper()
        transaction_id = request.data.get("transaction_id", "")
        frontend_base_url = request.data.get("frontend_base_url", "")

        if not submission_id:
            logger.error("[POST /api/verify-payment/] Missing submission_id")
            return Response({
                "status": "error",
                "message": "submission_id is required."
            }, status=status.HTTP_400_BAD_REQUEST)

        try:
            submission = QuizSubmission.objects.get(id=submission_id)
        except QuizSubmission.DoesNotExist:
            logger.error(f"[POST /api/verify-payment/] Submission ID {submission_id} does not exist.")
            return Response({
                "status": "error",
                "message": f"Quiz submission with ID {submission_id} not found."
            }, status=status.HTTP_404_NOT_FOUND)

        submission.payment_transaction_id = transaction_id

        # Note: All candidates can proceed to payment & form filling; score is recorded for Admin dashboard
        if payment_status_input != "RECEIVED":
            logger.warning(f"[POST /api/verify-payment/] Payment status is {payment_status_input}, not RECEIVED.")
            submission.payment_status = payment_status_input
            submission.save()
            return Response({
                "status": "error",
                "submission_id": submission.id,
                "payment_status": submission.payment_status,
                "whatsapp_link_sent": False,
                "message": "Payment not received! Admission form link will not be sent to WhatsApp."
            }, status=status.HTTP_400_BAD_REQUEST)
            logger.warning(f"[POST /api/verify-payment/] Payment status is {payment_status_input}, not RECEIVED.")
            submission.payment_status = payment_status_input
            submission.save()
            return Response({
                "status": "error",
                "submission_id": submission.id,
                "payment_status": submission.payment_status,
                "whatsapp_link_sent": False,
                "message": "Payment not received! Admission form link will not be sent to WhatsApp."
            }, status=status.HTTP_400_BAD_REQUEST)

        # Save Payment Status directly to database first
        submission.payment_status = "RECEIVED"
        submission.save(update_fields=['payment_status', 'payment_transaction_id'])
        logger.info(f"[POST /api/verify-payment/] Successfully updated DB: submission {submission.id} payment_status is now RECEIVED.")

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

        logger.info(f"[POST /api/verify-payment/] Triggering WhatsApp Template 'shreyas_admission' to {submission.whatsapp_number}...")
        wa_result = send_whatsapp_template_message(submission.whatsapp_number, submission.id)
        submission.whatsapp_link_sent = wa_result.get("success", False)
        submission.save(update_fields=['whatsapp_link_sent'])

        if not wa_result.get("success"):
            logger.error(f"[POST /api/verify-payment/] WhatsApp Template API Failed: {wa_result.get('error')}")
            return Response({
                "status": "error",
                "submission_id": submission.id,
                "vibhag": submission.vibhag,
                "payment_status": "RECEIVED",
                "whatsapp_link_sent": False,
                "whatsapp_error": wa_result.get("error"),
                "message": "Payment received, but WhatsApp template message delivery failed."
            }, status=status.HTTP_502_BAD_GATEWAY)

        logger.info(f"[POST /api/verify-payment/] WhatsApp message delivered successfully to {submission.whatsapp_number}!")
        return Response({
            "status": "success",
            "submission_id": submission.id,
            "vibhag": submission.vibhag,
            "payment_status": "RECEIVED",
            "whatsapp_link_sent": True,
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
        logger.info(f"[GET /api/admission-form/check-eligibility/] checking submission_id: {submission_id}")

        if not submission_id:
            logger.error("[GET /api/admission-form/check-eligibility/] Missing submission_id param.")
            return Response({
                "status": "error",
                "message": "submission_id query parameter is required."
            }, status=status.HTTP_400_BAD_REQUEST)

        try:
            submission = QuizSubmission.objects.get(id=submission_id)
        except QuizSubmission.DoesNotExist:
            logger.error(f"[GET /api/admission-form/check-eligibility/] ID {submission_id} not found.")
            return Response({
                "status": "error",
                "message": f"Quiz submission {submission_id} does not exist."
            }, status=status.HTTP_404_NOT_FOUND)

        # Quiz score is logged for admin reporting, all parents can fill the form once quiz is completed or enquiry registered
        if submission.payment_status != "RECEIVED":
            logger.warning(f"[GET /api/admission-form/check-eligibility/] Submission {submission_id} payment is {submission.payment_status}.")
            return Response({
                "status": "error",
                "can_fill_form": False,
                "payment_status": submission.payment_status,
                "message": "Payment is not yet received. Please complete fee payment to unlock the admission form."
            }, status=status.HTTP_402_PAYMENT_REQUIRED)

        existing_application = AdmissionApplication.objects.filter(submission=submission).first()
        if existing_application:
            logger.warning(f"[GET /api/admission-form/check-eligibility/] Form already submitted with Application No: {existing_application.application_number}")
            return Response({
                "status": "already_submitted",
                "can_fill_form": False,
                "already_submitted": True,
                "application_number": existing_application.application_number,
                "submitted_at": existing_application.created_at,
                "message": f"This admission form has already been submitted (Application No: {existing_application.application_number}). You cannot submit it again."
            }, status=status.HTTP_200_OK)

        logger.info(f"[GET /api/admission-form/check-eligibility/] Submission {submission_id} is VALID and ELIGIBLE to open form.")
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
    GET: List all submitted admission forms
    POST: Submit completed admission form with signatures and documents
    PATCH: Admin endpoint to edit/update details (name, phone, email, vibhag, etc.)
    """
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    def get(self, request):
        app_id = request.query_params.get("id")
        submission_id = request.query_params.get("submission_id")
        logger.info(f"[GET /api/admission-form/] params id={app_id}, submission_id={submission_id}")

        if app_id:
            application = AdmissionApplication.objects.filter(id=app_id).first()
            if not application:
                return Response({
                    "status": "error",
                    "message": f"Admission application with ID {app_id} not found."
                }, status=status.HTTP_404_NOT_FOUND)
            serializer = AdmissionApplicationSerializer(application)
            return Response({"status": "success", "data": serializer.data}, status=status.HTTP_200_OK)

        if submission_id:
            application = AdmissionApplication.objects.filter(submission_id=submission_id).first()
            if not application:
                return Response({
                    "status": "error",
                    "message": f"Admission application for submission_id {submission_id} not found. (The form may not have been submitted yet)."
                }, status=status.HTTP_404_NOT_FOUND)
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
        logger.info(f"[POST /api/admission-form/] Incoming request data keys: {list(request.data.keys())}")
        logger.info(f"[POST /api/admission-form/] Files uploaded: {list(request.FILES.keys())}")

        submission_id = request.data.get("submission_id")
        if not submission_id:
            logger.error("[POST /api/admission-form/] Missing submission_id")
            return Response({
                "status": "error",
                "message": "submission_id is required to verify quiz & payment eligibility."
            }, status=status.HTTP_400_BAD_REQUEST)

        try:
            submission = QuizSubmission.objects.get(id=submission_id)
        except QuizSubmission.DoesNotExist:
            logger.error(f"[POST /api/admission-form/] Submission {submission_id} does not exist.")
            return Response({
                "status": "error",
                "message": f"Quiz submission {submission_id} not found."
            }, status=status.HTTP_404_NOT_FOUND)

        if submission.payment_status != "RECEIVED":
            logger.error(f"[POST /api/admission-form/] Submission {submission_id} payment status is {submission.payment_status}.")
            return Response({
                "status": "error",
                "message": "Cannot submit form! Payment has not been received."
            }, status=status.HTTP_402_PAYMENT_REQUIRED)

        existing_app = AdmissionApplication.objects.filter(submission=submission).first()
        if existing_app:
            logger.warning(f"[POST /api/admission-form/] Form already submitted for submission_id {submission_id} with App No: {existing_app.application_number}")
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
            logger.info(f"[POST /api/admission-form/] Form SUBMITTED SUCCESSFULLY! Application Number: {application.application_number}")
            return Response({
                "status": "success",
                "message": "Admission form submitted and stored successfully!",
                "application_id": application.id,
                "application_number": application.application_number,
                "vibhag": application.vibhag,
                "data": serializer.data
            }, status=status.HTTP_201_CREATED)

        logger.error(f"[POST /api/admission-form/] VALIDATION FAILED! Errors: {serializer.errors}")
        return Response({
            "status": "error",
            "errors": serializer.errors
        }, status=status.HTTP_400_BAD_REQUEST)

    def patch(self, request):
        """
        ADMIN ONLY ENDPOINT:
        Allows Admin Dashboard to edit/update any application fields:
        Child Name, Father Name, Mother Name, Phone numbers, Email, Vibhag, etc.
        """
        app_id = request.data.get("id") or request.query_params.get("id")
        submission_id = request.data.get("submission_id") or request.query_params.get("submission_id")

        if not app_id and not submission_id:
            return Response({
                "status": "error",
                "message": "Either 'id' or 'submission_id' is required to update an application."
            }, status=status.HTTP_400_BAD_REQUEST)

        if app_id:
            application = get_object_or_404(AdmissionApplication, id=app_id)
        else:
            application = get_object_or_404(AdmissionApplication, submission_id=submission_id)

        # Allow partial updates
        serializer = AdmissionApplicationSerializer(
            application,
            data=request.data,
            partial=True,
            context={'request': request, 'is_admin_update': True}
        )
        if serializer.is_valid():
            updated_app = serializer.save()
            logger.info(f"[PATCH /api/admission-form/] Application {updated_app.application_number} updated by Admin.")

            # Check if phone number was updated (father_mobile, mother_mobile, or whatsapp_number)
            new_number = request.data.get("whatsapp_number") or request.data.get("father_mobile") or request.data.get("mother_mobile")
            whatsapp_sent = False
            if new_number:
                clean_num = "".join(filter(str.isdigit, str(new_number)))
                if len(clean_num) >= 10:
                    # Update QuizSubmission whatsapp_number as well to keep them in sync
                    if updated_app.submission:
                        updated_app.submission.whatsapp_number = clean_num
                        updated_app.submission.save(update_fields=['whatsapp_number'])

                    res = send_whatsapp_template_message(clean_num, updated_app.submission_id)
                    if res.get("success"):
                        whatsapp_sent = True
                        if updated_app.submission:
                            updated_app.submission.whatsapp_link_sent = True
                            updated_app.submission.save(update_fields=['whatsapp_link_sent'])
                        logger.info(f"[PATCH /api/admission-form/] Resent WhatsApp to {clean_num} for submission {updated_app.submission_id}!")

            return Response({
                "status": "success",
                "message": f"Application {updated_app.application_number} updated successfully. WhatsApp message resent to {new_number}." if whatsapp_sent else f"Application {updated_app.application_number} updated successfully.",
                "whatsapp_link_sent": whatsapp_sent,
                "data": serializer.data
            }, status=status.HTTP_200_OK)

        return Response({
            "status": "error",
            "errors": serializer.errors
        }, status=status.HTTP_400_BAD_REQUEST)

    def delete(self, request):
        """
        ADMIN ONLY ENDPOINT:
        Allows Admin Dashboard to delete an application OR an enquiry.
        DELETE /api/admission-form/?id=8
        or
        DELETE /api/admission-form/?submission_id=41
        """
        app_id = request.query_params.get("id")
        submission_id = request.query_params.get("submission_id")

        if not app_id and not submission_id:
            return Response({
                "status": "error",
                "message": "Either 'id' or 'submission_id' query parameter is required to delete."
            }, status=status.HTTP_400_BAD_REQUEST)

        # 1. If deleting by Admission Application ID
        if app_id:
            application = AdmissionApplication.objects.filter(id=app_id).first()
            if application:
                app_number = application.application_number
                application_id = application.id
                sub_id = application.submission_id
                application.delete()
                # Also delete associated quiz submission
                QuizSubmission.objects.filter(id=sub_id).delete()
                logger.info(f"[DELETE /api/admission-form/] Application {app_number} and Submission {sub_id} deleted by Admin.")
                return Response({
                    "status": "success",
                    "message": f"Application {app_number} and associated enquiry deleted successfully.",
                    "deleted_id": application_id,
                    "application_number": app_number
                }, status=status.HTTP_200_OK)
            else:
                return Response({
                    "status": "error",
                    "message": f"Admission application with ID {app_id} not found."
                }, status=status.HTTP_404_NOT_FOUND)

        # 2. If deleting by submission_id
        if submission_id:
            application = AdmissionApplication.objects.filter(submission_id=submission_id).first()
            submission = QuizSubmission.objects.filter(id=submission_id).first()

            if not application and not submission:
                return Response({
                    "status": "error",
                    "message": f"No application or enquiry found for submission_id {submission_id}."
                }, status=status.HTTP_404_NOT_FOUND)

            app_number = application.application_number if application else None
            child_name = submission.child_name if submission else (application.child_first_name if application else "")

            if application:
                application.delete()
            if submission:
                submission.delete()

            logger.info(f"[DELETE /api/admission-form/] Successfully deleted submission_id {submission_id} (Child: {child_name}, App: {app_number}).")
            return Response({
                "status": "success",
                "message": f"Successfully deleted enquiry/application for submission_id {submission_id} ({child_name}).",
                "submission_id": int(submission_id),
                "application_number": app_number
            }, status=status.HTTP_200_OK)
