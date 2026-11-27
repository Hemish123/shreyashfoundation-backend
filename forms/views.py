import logging
import json
import random
import re
import os
import requests
import threading
from datetime import datetime, date, timedelta

from django.shortcuts import get_object_or_404
from django.conf import settings
from django.db import models
from django.db.models import Q, Count, Case, When, IntegerField
from django.utils import timezone
from django.core.mail import send_mail
from typing import Optional

from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.parsers import MultiPartParser, FormParser, JSONParser

from .models import QuizSubmission, AdmissionApplication, AdminUser, AuditLog
from .jwt_auth import generate_jwt_token, decode_jwt_token, get_admin_user_from_token
from .questions_data import (
    ALIGNED_ANSWERS,
    PARENT_MCQ_SCORING_MATRIX,
    QUESTIONNAIRE_QUESTIONS,
    score_parent_questionnaire,
    TOTAL_QUESTIONNAIRE_MARKS,
    TOTAL_MCQ_MARKS
)
from .serializers import (
    SubmitQuizSerializer,
    QuizSubmissionListSerializer,
    AdmissionApplicationSerializer
)
from .whatsapp import (
    generate_whatsapp_link,
    send_whatsapp_template_message,
    send_whatsapp_booklet_template,
    send_payment_received_whatsapp_notifications,
    normalize_phone_number,
    WHATSAPP_CRM_WEBHOOK_URL,
    WHATSAPP_STATUS_TRACKER,
    record_whatsapp_message_status
)

logger = logging.getLogger(__name__)


# ==============================================================================
# Helper Functions
# ==============================================================================

def get_performance_tag(score: int, max_score: int = 50) -> str:
    """
    Performance tag based on percentage of score.
    Supports both 50-mark questionnaire and legacy 7-mark screening.
    """
    if max_score > 0:
        pct = (score / float(max_score)) * 100.0
    else:
        pct = 0.0

    if pct >= 85:
        return "Outstanding"
    elif pct >= 70:
        return "Excellent"
    elif pct >= 55:
        return "Very Good"
    elif pct >= 40:
        return "Good"
    else:
        return "Needs Improvement"


def get_authenticated_admin_user(request) -> tuple[bool, Optional[AdminUser], str]:
    """
    Strict Token Authentication for Admin/Staff endpoints.
    Requires 'Authorization: Bearer <token>' in request headers or '?token=<token>' in query params.
    Returns (is_authenticated, admin_user_obj, role)
    """
    auth_header = request.headers.get('Authorization') or request.META.get('HTTP_AUTHORIZATION') or ""
    if auth_header and auth_header.lower().startswith('bearer '):
        user = get_admin_user_from_token(auth_header)
        if user:
            return True, user, user.role

    query_token = (
        request.query_params.get('token')
        or request.query_params.get('access_token')
        or request.GET.get('token')
        or ""
    ).strip()
    if query_token:
        user = get_admin_user_from_token(query_token)
        if user:
            return True, user, user.role

    # Fallback only for Django superuser session in admin site
    if hasattr(request, 'user') and request.user.is_authenticated and request.user.is_staff:
        return True, None, "ADMIN"

    return False, None, "GUEST"


def enforce_admin_token(request, required_role: Optional[str] = None):
    """
    Helper to mandate valid Bearer Token for admin APIs.
    If invalid or missing token, returns Response with 401/403. Otherwise returns None.
    """
    is_auth, user, role = get_authenticated_admin_user(request)
    if not is_auth:
        return Response({
            "status": "error",
            "message": "Authentication required. Please provide a valid 'Authorization: Bearer <token>' header."
        }, status=status.HTTP_401_UNAUTHORIZED)

    if required_role and role != required_role:
        return Response({
            "status": "error",
            "message": f"Access restricted. Only users with role '{required_role}' are allowed."
        }, status=status.HTTP_403_FORBIDDEN)

    return None


# ==============================================================================
# Admin Authentication & User Management
# ==============================================================================

class AdminLoginView(APIView):
    """
    Two-Step Verification: Step 1 - Verify Username/Email & PIN, then send OTP to registered email.
    POST /api/admin/login/
    Body: {"email": "admin@shreyasfoundation.in", "pin": "1234"}
    """
    def post(self, request):
        identifier = (request.data.get("email") or request.data.get("username") or "").strip()
        pin = str(request.data.get("pin", "")).strip()

        if not identifier or not pin:
            return Response({
                "status": "error",
                "message": "Email/Username and PIN are required."
            }, status=status.HTTP_400_BAD_REQUEST)

        admin_user = AdminUser.objects.filter(
            Q(email__iexact=identifier) | Q(username__iexact=identifier),
            is_active=True
        ).first()

        if not admin_user or not admin_user.check_pin(pin):
            return Response({
                "status": "error",
                "message": "Invalid email/username or PIN."
            }, status=status.HTTP_401_UNAUTHORIZED)

        # Generate 6-digit OTP for 2-step verification
        otp = f"{random.randint(100000, 999999)}"
        admin_user.otp_code = otp
        admin_user.otp_expires_at = timezone.now() + timedelta(minutes=10)
        admin_user.save(update_fields=['otp_code', 'otp_expires_at'])

        subject = "Shreyas Foundation Admin - 2-Step Verification Login OTP"
        body = (
            f"Namaste {admin_user.full_name or admin_user.username},\n\n"
            f"Your 2-Step Verification OTP for logging into Shreyas Foundation Portal is: {otp}\n\n"
            f"If you did not attempt this login, please contact technical support immediately.\n\n"
            f"Shreyas Foundation Technical Team"
        )
        try:
            from .email_service import generate_otp_email_html
            sender = getattr(settings, 'DEFAULT_FROM_EMAIL', 'JMS Tech Solution <anand@jmstech.co>')
            html_message = generate_otp_email_html(
                recipient_name=admin_user.full_name or admin_user.username,
                otp_code=otp,
                purpose="login"
            )
            send_mail(
                subject=subject,
                message=body,
                from_email=sender,
                recipient_list=[admin_user.email],
                html_message=html_message,
                fail_silently=True
            )
            logger.info(f"2-Step verification OTP sent to {admin_user.email}")
        except Exception as e:
            logger.error(f"Failed to send 2-Step verification email OTP: {e}")

        # Mask email for privacy (e.g. pr***11@gmail.com)
        email_parts = admin_user.email.split('@')
        if len(email_parts) == 2 and len(email_parts[0]) > 2:
            masked_email = f"{email_parts[0][:2]}***{email_parts[0][-2:]}@{email_parts[1]}"
        else:
            masked_email = admin_user.email

        return Response({
            "status": "otp_required",
            "message": f"Credentials verified. 2-Step verification OTP has been sent to your email ({masked_email}).",
            "email": admin_user.email,
            "masked_email": masked_email,
            **({"debug_otp": otp} if settings.DEBUG else {})
        }, status=status.HTTP_200_OK)


class AdminVerifyLoginOTPView(APIView):
    """
    Two-Step Verification: Step 2 - Verify OTP & complete login, returns JWT permanent token.
    POST /api/admin/verify-login-otp/
    Body: {"email": "admin@shreyasfoundation.in", "otp": "123456"}
    """
    def post(self, request):
        identifier = (request.data.get("email") or request.data.get("username") or "").strip()
        otp = str(request.data.get("otp", "")).strip()

        if not identifier or not otp:
            return Response({
                "status": "error",
                "message": "Email/Username and OTP are required."
            }, status=status.HTTP_400_BAD_REQUEST)

        admin_user = AdminUser.objects.filter(
            Q(email__iexact=identifier) | Q(username__iexact=identifier),
            is_active=True
        ).first()

        if not admin_user:
            return Response({
                "status": "error",
                "message": "Admin user not found."
            }, status=status.HTTP_404_NOT_FOUND)

        if not admin_user.otp_code or admin_user.otp_code != otp:
            return Response({
                "status": "error",
                "message": "Invalid OTP code. Please check your email and try again."
            }, status=status.HTTP_400_BAD_REQUEST)

        if admin_user.otp_expires_at and admin_user.otp_expires_at < timezone.now():
            return Response({
                "status": "error",
                "message": "OTP has expired. Please log in again to receive a fresh OTP."
            }, status=status.HTTP_400_BAD_REQUEST)

        # Clear OTP after successful verification
        admin_user.otp_code = None
        admin_user.otp_expires_at = None
        admin_user.save(update_fields=['otp_code', 'otp_expires_at'])

        # Generate permanent JWT token
        token = generate_jwt_token(admin_user)

        logger.info(f"Admin 2-Step login successful for {admin_user.username} ({admin_user.email})")

        return Response({
            "status": "success",
            "message": "2-Step verification successful. Login completed.",
            "token": token,
            "token_type": "Bearer",
            "is_permanent": True,
            "user": {
                "id": admin_user.id,
                "username": admin_user.username,
                "full_name": admin_user.full_name,
                "email": admin_user.email,
                "role": admin_user.role
            }
        }, status=status.HTTP_200_OK)


class AdminUserManagementView(APIView):
    """
    User Management: Only Admin can create, list, edit (PUT/PATCH), and delete users.
    GET    /api/admin/users/             - List all users or get single user (?id=...)
    POST   /api/admin/users/             - Create new user (Role: ADMIN / STAFF / ACADEMICIAN)
    PATCH  /api/admin/users/?id=<id>     - Edit user details (full_name, email, role, is_active, pin)
    DELETE /api/admin/users/?id=<id>     - Delete a user (Cannot delete yourself)
    """
    def get(self, request, user_id=None):
        denied = enforce_admin_token(request, required_role='ADMIN')
        if denied:
            return denied

        target_id = user_id or request.query_params.get("id")
        if target_id:
            user = get_object_or_404(AdminUser, id=target_id)
            return Response({
                "status": "success",
                "user": {
                    "id": user.id,
                    "username": user.username,
                    "full_name": user.full_name,
                    "email": user.email,
                    "role": user.role,
                    "is_active": user.is_active,
                    "created_at": user.created_at,
                    "updated_at": user.updated_at
                }
            }, status=status.HTTP_200_OK)

        users = AdminUser.objects.all().order_by('-created_at')
        data = [{
            "id": u.id,
            "username": u.username,
            "full_name": u.full_name,
            "email": u.email,
            "role": u.role,
            "is_active": u.is_active,
            "created_at": u.created_at,
            "updated_at": u.updated_at
        } for u in users]
        return Response({"status": "success", "count": len(data), "users": data}, status=status.HTTP_200_OK)

    def post(self, request):
        denied = enforce_admin_token(request, required_role='ADMIN')
        if denied:
            return denied

        username = request.data.get("username", "").strip()
        full_name = request.data.get("full_name", "").strip()
        email = request.data.get("email", "").strip()
        pin = str(request.data.get("pin", "")).strip()
        role = request.data.get("role", "STAFF").strip().upper()

        if not email or not pin:
            return Response({
                "status": "error",
                "message": "Email and PIN are required."
            }, status=status.HTTP_400_BAD_REQUEST)

        if not username:
            username = email.split('@')[0]

        if len(pin) < 4:
            return Response({
                "status": "error",
                "message": "PIN must be at least 4 digits."
            }, status=status.HTTP_400_BAD_REQUEST)

        if AdminUser.objects.filter(username__iexact=username).exists():
            return Response({
                "status": "error",
                "message": f"Username '{username}' already exists. Please choose a different username."
            }, status=status.HTTP_400_BAD_REQUEST)

        if AdminUser.objects.filter(email__iexact=email).exists():
            return Response({
                "status": "error",
                "message": f"Email '{email}' is already registered."
            }, status=status.HTTP_400_BAD_REQUEST)

        valid_roles = [r[0] for r in AdminUser.ROLE_CHOICES]
        if role not in valid_roles:
            role = 'STAFF'

        user = AdminUser(
            username=username,
            full_name=full_name,
            email=email,
            role=role,
            is_active=True
        )
        user.set_pin(pin)
        user.save()

        logger.info(f"Admin created new user: {user.username} ({user.email}) with role: {user.role}")
        return Response({
            "status": "success",
            "message": f"User {user.email} created successfully with role '{user.role}'.",
            "user": {
                "id": user.id,
                "username": user.username,
                "full_name": user.full_name,
                "email": user.email,
                "role": user.role
            }
        }, status=status.HTTP_201_CREATED)

    def patch(self, request, user_id=None):
        """
        Edit User: Can update full_name, email, role, is_active, and reset PIN.
        Pass ID in query (?id=...) or in URL path.
        """
        denied = enforce_admin_token(request, required_role='ADMIN')
        if denied:
            return denied

        target_id = user_id or request.query_params.get("id") or request.data.get("id")
        if not target_id:
            return Response({
                "status": "error",
                "message": "User ID is required (pass ?id=<id> or in request body)."
            }, status=status.HTTP_400_BAD_REQUEST)

        user = AdminUser.objects.filter(id=target_id).first()
        if not user:
            return Response({
                "status": "error",
                "message": f"AdminUser with id {target_id} does not exist."
            }, status=status.HTTP_404_NOT_FOUND)

        if "full_name" in request.data:
            user.full_name = str(request.data["full_name"]).strip()

        if "email" in request.data:
            new_email = str(request.data["email"]).strip()
            if new_email and new_email.lower() != user.email.lower():
                if AdminUser.objects.filter(email__iexact=new_email).exclude(id=user.id).exists():
                    return Response({
                        "status": "error",
                        "message": f"Email '{new_email}' is already in use by another user."
                    }, status=status.HTTP_400_BAD_REQUEST)
                user.email = new_email

        if "username" in request.data:
            new_username = str(request.data["username"]).strip()
            if new_username and new_username.lower() != user.username.lower():
                if AdminUser.objects.filter(username__iexact=new_username).exclude(id=user.id).exists():
                    return Response({
                        "status": "error",
                        "message": f"Username '{new_username}' is already in use by another user."
                    }, status=status.HTTP_400_BAD_REQUEST)
                user.username = new_username

        if "role" in request.data:
            new_role = str(request.data["role"]).strip().upper()
            valid_roles = [r[0] for r in AdminUser.ROLE_CHOICES]
            if new_role in valid_roles:
                user.role = new_role

        if "is_active" in request.data:
            val = request.data["is_active"]
            user.is_active = (val is True or str(val).lower() == 'true')

        if "pin" in request.data and str(request.data["pin"]).strip():
            new_pin = str(request.data["pin"]).strip()
            if len(new_pin) < 4:
                return Response({
                    "status": "error",
                    "message": "PIN must be at least 4 digits."
                }, status=status.HTTP_400_BAD_REQUEST)
            user.set_pin(new_pin)

        user.save()
        logger.info(f"Admin updated user {user.id} ({user.username})")

        return Response({
            "status": "success",
            "message": f"User '{user.username}' updated successfully.",
            "user": {
                "id": user.id,
                "username": user.username,
                "full_name": user.full_name,
                "email": user.email,
                "role": user.role,
                "is_active": user.is_active,
                "updated_at": user.updated_at
            }
        }, status=status.HTTP_200_OK)

    def delete(self, request, user_id=None):
        """
        Delete User: Permanent deletion of a staff/academician/admin user.
        Pass ID in query (?id=...) or in URL path.
        Cannot delete the currently logged-in admin user.
        """
        denied = enforce_admin_token(request, required_role='ADMIN')
        if denied:
            return denied

        _, current_admin, _ = get_authenticated_admin_user(request)

        target_id = user_id or request.query_params.get("id") or request.data.get("id")
        if not target_id:
            return Response({
                "status": "error",
                "message": "User ID is required to delete (pass ?id=<id> or in request body)."
            }, status=status.HTTP_400_BAD_REQUEST)

        user = AdminUser.objects.filter(id=target_id).first()
        if not user:
            return Response({
                "status": "error",
                "message": f"AdminUser with id {target_id} does not exist."
            }, status=status.HTTP_404_NOT_FOUND)

        # Prevent admin from deleting themselves
        if current_admin and current_admin.id == user.id:
            return Response({
                "status": "error",
                "message": "You cannot delete your own admin account while logged in."
            }, status=status.HTTP_400_BAD_REQUEST)

        deleted_username = user.username
        deleted_email = user.email
        user.delete()

        logger.info(f"Admin deleted user: {deleted_username} ({deleted_email}, id: {target_id})")
        return Response({
            "status": "success",
            "message": f"User '{deleted_username}' ({deleted_email}) has been permanently deleted.",
            "deleted_user_id": int(target_id)
        }, status=status.HTTP_200_OK)


class ForgotPinRequestOTPView(APIView):
    """
    Flow A: Step 2 - Forgot PIN: Send OTP to registered email
    POST /api/admin/forgot-pin/request-otp/
    Body: {"email": "admin@shreyasfoundation.in"} or {"username": "admin.shreyas"}
    """
    def post(self, request):
        identifier = (request.data.get("email") or request.data.get("username") or "").strip()
        if not identifier:
            return Response({
                "status": "error",
                "message": "Username or registered email is required."
            }, status=status.HTTP_400_BAD_REQUEST)

        admin_user = AdminUser.objects.filter(
            Q(email__iexact=identifier) | Q(username__iexact=identifier),
            is_active=True
        ).first()

        if not admin_user:
            return Response({
                "status": "error",
                "message": "No active admin user found with this email or username."
            }, status=status.HTTP_404_NOT_FOUND)

        otp = f"{random.randint(100000, 999999)}"
        admin_user.otp_code = otp
        admin_user.otp_expires_at = timezone.now() + timedelta(minutes=10)
        admin_user.save(update_fields=['otp_code', 'otp_expires_at'])

        subject = "Shreyas Foundation Admin - Password/PIN Reset OTP"
        body = (
            f"Namaste {admin_user.full_name or admin_user.username},\n\n"
            f"Your OTP for resetting the PIN is: {otp}\n"
            f"If you did not request this, please ignore this email.\n\n"
            f"Shreyas Foundation Technical Team"
        )
        try:
            from .email_service import generate_otp_email_html
            sender = getattr(settings, 'DEFAULT_FROM_EMAIL', 'JMS Tech Solution <anand@jmstech.co>')
            html_message = generate_otp_email_html(
                recipient_name=admin_user.full_name or admin_user.username,
                otp_code=otp,
                purpose="reset"
            )
            send_mail(
                subject=subject,
                message=body,
                from_email=sender,
                recipient_list=[admin_user.email],
                html_message=html_message,
                fail_silently=True
            )
            logger.info(f"Forgot PIN OTP sent to {admin_user.email}")
        except Exception as e:
            logger.error(f"Failed to send email OTP: {e}")

        return Response({
            "status": "success",
            "message": f"OTP has been sent to {admin_user.email} (valid for 10 minutes).",
            "email": admin_user.email,
            **({"debug_otp": otp} if settings.DEBUG else {})
        }, status=status.HTTP_200_OK)


class ForgotPinVerifyAndResetView(APIView):
    """
    Flow A: Step 2 - Verify OTP & Set New PIN
    POST /api/admin/forgot-pin/reset/
    Body: {"email": "admin@shreyasfoundation.in", "otp": "123456", "new_pin": "5678"}
    """
    def post(self, request):
        identifier = (request.data.get("email") or request.data.get("username") or "").strip()
        otp = request.data.get("otp", "").strip()
        new_pin = str(request.data.get("new_pin", "")).strip()

        if not identifier or not otp or not new_pin:
            return Response({
                "status": "error",
                "message": "Email/Username, OTP, and new_pin are required."
            }, status=status.HTTP_400_BAD_REQUEST)

        if len(new_pin) < 4:
            return Response({
                "status": "error",
                "message": "New PIN must be at least 4 digits."
            }, status=status.HTTP_400_BAD_REQUEST)

        admin_user = AdminUser.objects.filter(
            Q(email__iexact=identifier) | Q(username__iexact=identifier),
            is_active=True
        ).first()

        if not admin_user:
            return Response({
                "status": "error",
                "message": "Admin user not found."
            }, status=status.HTTP_404_NOT_FOUND)

        if not admin_user.otp_code or admin_user.otp_code != otp:
            return Response({
                "status": "error",
                "message": "Invalid OTP code."
            }, status=status.HTTP_400_BAD_REQUEST)

        if admin_user.otp_expires_at and admin_user.otp_expires_at < timezone.now():
            return Response({
                "status": "error",
                "message": "OTP has expired. Please request a new OTP."
            }, status=status.HTTP_400_BAD_REQUEST)

        admin_user.set_pin(new_pin)
        admin_user.otp_code = None
        admin_user.otp_expires_at = None
        admin_user.save(update_fields=['pin_hash', 'otp_code', 'otp_expires_at'])

        return Response({
            "status": "success",
            "message": "Admin PIN has been reset successfully. You can now login with your new PIN."
        }, status=status.HTTP_200_OK)


class AdminInteractionCalendarView(APIView):
    """
    Step 8: Backend Calendar Tab - Lists day-wise scheduled interviews/interactions for Admin.
    GET /api/admin/interaction-calendar/?date=YYYY-MM-DD
    Headers: Authorization: Bearer <ADMIN_PERMANENT_TOKEN>
    Strictly restricted: Only users with role 'ADMIN' can access.
    """
    def get(self, request):
        denied = enforce_admin_token(request)
        if denied:
            return denied

        _, logged_in_admin, role = get_authenticated_admin_user(request)
        if role != 'ADMIN':
            return Response({
                "status": "error",
                "message": "Access restricted. Interaction calendar is only visible to Admin users."
            }, status=status.HTTP_403_FORBIDDEN)

        filter_date_str = request.query_params.get("date")
        filter_slot_param = request.query_params.get("slot") or request.query_params.get("slot_no")
        search_query = (request.query_params.get("search") or request.query_params.get("q") or "").strip()

        queryset = AdmissionApplication.objects.filter(
            interaction_date__isnull=False
        ).select_related('submission').order_by('interaction_date', 'interaction_slot_no', 'created_at')

        if filter_date_str:
            queryset = queryset.filter(interaction_date=filter_date_str)

        if filter_slot_param and str(filter_slot_param).strip().isdigit():
            queryset = queryset.filter(interaction_slot_no=int(filter_slot_param))

        # Global Search filter for child name, parent name, submission ID, application no, and whatsapp/phone
        if search_query:
            clean_digits = re.sub(r"\D", "", search_query)
            search_cond = (
                Q(child_first_name__icontains=search_query) |
                Q(child_surname__icontains=search_query) |
                Q(father_name__icontains=search_query) |
                Q(mother_name__icontains=search_query) |
                Q(application_number__icontains=search_query) |
                Q(father_whatsapp__icontains=search_query) |
                Q(mother_whatsapp__icontains=search_query)
            )
            if clean_digits:
                search_cond |= Q(father_whatsapp__icontains=clean_digits) | Q(submission__whatsapp_number__icontains=clean_digits)
            if search_query.isdigit():
                search_cond |= Q(submission_id=int(search_query)) | Q(id=int(search_query))
            queryset = queryset.filter(search_cond)

        # Group day-wise & slot-wise for the Calendar UI
        calendar_data = {}
        for app in queryset:
            date_key = str(app.interaction_date)
            if date_key not in calendar_data:
                calendar_data[date_key] = {
                    "date": date_key,
                    "total_children": 0,
                    "slots": {
                        1: {"slot_no": 1, "time": "9:00 AM TO 10:00 AM", "count": 0, "standard_capacity": 10, "max_capacity": 13, "children": []},
                        2: {"slot_no": 2, "time": "10:00 AM TO 11:00 AM", "count": 0, "standard_capacity": 10, "max_capacity": 13, "children": []},
                        3: {"slot_no": 3, "time": "11:00 AM TO 12:00 PM", "count": 0, "standard_capacity": 10, "max_capacity": 13, "children": []},
                        4: {"slot_no": 4, "time": "12:00 PM TO 1:00 PM", "count": 0, "standard_capacity": 10, "max_capacity": 13, "children": []},
                    }
                }

            calendar_data[date_key]["total_children"] += 1
            slot_no = app.interaction_slot_no or 1
            if slot_no in calendar_data[date_key]["slots"]:
                calendar_data[date_key]["slots"][slot_no]["count"] += 1
                calendar_data[date_key]["slots"][slot_no]["children"].append({
                    "application_id": app.id,
                    "submission_id": app.submission_id or (app.submission.id if app.submission else None),
                    "application_number": app.application_number,
                    "child_name": f"{app.child_first_name} {app.child_surname}".strip(),
                    "standard": app.standard,
                    "medium": app.medium,
                    "vibhag": app.vibhag,
                    "father_name": app.father_name,
                    "father_whatsapp": app.father_whatsapp or (app.submission.whatsapp_number if app.submission else ""),
                    "whatsapp_sent": app.interaction_whatsapp_sent,
                    "reminder_sent": app.interaction_reminder_sent,
                    "reminder_sent_at": app.interaction_reminder_sent_at.strftime("%Y-%m-%d %H:%M:%S") if app.interaction_reminder_sent_at else None
                })

        # Check if caller wants direct export (Excel Only: Assessment Part Y format)
        export_param = request.query_params.get("export") or request.query_params.get("format") or request.query_params.get("export_format")
        if export_param and str(export_param).lower() in ["excel", "xlsx", "true", "1"]:
            from django.http import HttpResponse
            from openpyxl import Workbook
            from openpyxl.styles import Font, PatternFill, Alignment, Border, Side

            filename_parts = ["Parameters_Of_Assessment_Part_Y"]
            if filter_date_str:
                filename_parts.append(filter_date_str)
            if filter_slot_param:
                filename_parts.append(f"Slot_{filter_slot_param}")
            filename_parts.append(timezone.now().strftime('%Y%m%d_%H%M%S'))
            filename = "_".join(filename_parts)

            wb = Workbook()
            ws = wb.active
            ws.title = "Assessment - Part Y"
            ws.views.sheetView[0].showGridLines = True

            # Styling definitions
            title_font = Font(name="Calibri", size=14, bold=True, underline="single", color="000000")
            header_font = Font(name="Calibri", size=10, bold=True, color="000000")
            header_fill = PatternFill(start_color="F2F4F7", end_color="F2F4F7", fill_type="solid")
            border_thin = Side(style='thin', color='000000')
            cell_border = Border(left=border_thin, right=border_thin, top=border_thin, bottom=border_thin)
            data_font = Font(name="Calibri", size=10)

            # Row 1: Title (Centered across columns A to L)
            ws.merge_cells("A1:L1")
            title_cell = ws.cell(row=1, column=1, value="PARAMETERS OF ASSESSMENT – PART Y")
            title_cell.font = title_font
            title_cell.alignment = Alignment(horizontal="center", vertical="center")
            ws.row_dimensions[1].height = 28

            # Row 2: Subtitle Metadata (Date, and Slot if slot-filtered)
            sub_font = Font(name="Calibri", size=11, bold=True, color="000000")
            subtitle_parts = []
            if filter_date_str:
                subtitle_parts.append(f"Date: {filter_date_str}")
            if filter_slot_param:
                subtitle_parts.append(f"Slot: {filter_slot_param}")
            subtitle_text = " | ".join(subtitle_parts) if subtitle_parts else "Date: All Dates"

            ws.merge_cells("A2:L2")
            sub_cell = ws.cell(row=2, column=1, value=subtitle_text)
            sub_cell.font = sub_font
            sub_cell.alignment = Alignment(horizontal="left", vertical="center")
            ws.row_dimensions[2].height = 22

            # Two-tier Headers at Row 3 & Row 4
            # (Columns: Sr. No., Admission Form No., Application No., Slot No, Slot Time, M, MI, Cognitive, Affective, Psychomotor, Total)
            headers_r3 = [
                ("A", "Sr. No.", True),
                ("B", "Admission Form No.", True),
                ("C", "Application No.", True),
                ("D", "Slot No", True),
                ("E", "Slot Time", True),
                ("F", "M", True),
                ("G", "MI", True),
                ("H:I", "Cognitive Assessment", False),
                ("J", "Affective Assessment", False),
                ("K", "Psychomotor Assessment", False),
                ("L", "Total", False),
            ]

            # Merge vertical for columns A to G (Row 3 to 4)
            for col_letter, text, is_vert_merged in headers_r3:
                if is_vert_merged:
                    ws.merge_cells(f"{col_letter}3:{col_letter}4")
                    cell = ws[f"{col_letter}3"]
                    cell.value = text
                elif col_letter == "H:I":
                    ws.merge_cells("H3:I3")
                    ws["H3"] = text
                elif col_letter == "J":
                    ws["J3"] = text
                elif col_letter == "K":
                    ws["K3"] = text
                elif col_letter == "L":
                    ws["L3"] = text

            # Row 4 marks values
            ws["H4"] = 5
            ws["I4"] = 5
            ws["J4"] = 10
            ws["K4"] = 10
            ws["L4"] = 30

            ws.row_dimensions[3].height = 24
            ws.row_dimensions[4].height = 20

            # Apply borders and styling to Header Rows (3 and 4)
            for r in [3, 4]:
                for c in range(1, 13):
                    cell = ws.cell(row=r, column=c)
                    cell.font = header_font
                    cell.fill = header_fill
                    cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
                    cell.border = cell_border

            # Data Rows (Starting at Row 5)
            current_row = 5
            for idx, app in enumerate(queryset, start=1):
                slot_no_val = app.interaction_slot_no if app.interaction_slot_no else "-"
                slot_time_val = app.interaction_time or "-"
                sub_id_val = str(app.submission.id) if app.submission else (str(app.submission_id) if app.submission_id else "-")
                app_no_val = str(app.application_number or f"SF_{app.id}")

                m_val = app.evaluator_m or ""

                # Determine MI value: if evaluator_mi is provided, use it; otherwise check past_illness (Yes/No)
                if app.evaluator_mi:
                    mi_val = app.evaluator_mi
                else:
                    raw_illness = (app.past_illness or "").strip().lower()
                    # Remove common punctuation like dots, dashes, commas, slashes
                    cleaned_illness = re.sub(r"[\s\.\-_/\\,;:!]+", "", raw_illness)
                    negatives = {
                        "", "none", "no", "na", "nil", "null", "not", "nothing", "nope",
                        "noone", "notany", "noillness", "noissue", "noissues", "notapplicable",
                        "never", "good", "fine", "ok", "normal", "healthy", "fit", "allgood",
                        "નથી", "કોઈનથી", "કંઈનથી", "ના", "કઈનથી", "કશુનથી", "કશુંનથી",
                        "નહિ", "નહી", "બરાબર", "સાજુ", "સાજું", "તંદુરસ્ત"
                    }
                    if not cleaned_illness or cleaned_illness in negatives or raw_illness in negatives or "નથી" in raw_illness or "no illness" in raw_illness:
                        mi_val = "No"
                    else:
                        mi_val = "Yes"
                cog1_val = app.cognitive_assessment_1 if app.cognitive_assessment_1 > 0 else ""
                cog2_val = app.cognitive_assessment_2 if app.cognitive_assessment_2 > 0 else ""
                aff_val = app.affective_assessment if app.affective_assessment > 0 else ""
                psy_val = app.psychomotor_assessment if app.psychomotor_assessment > 0 else ""
                total_y_val = app.total_part_y_score if app.total_part_y_score > 0 else ""

                row_values = [
                    idx,
                    sub_id_val,
                    app_no_val,
                    slot_no_val,
                    slot_time_val,
                    m_val,
                    mi_val,
                    cog1_val,
                    cog2_val,
                    aff_val,
                    psy_val,
                    total_y_val
                ]

                ws.append(row_values)
                ws.row_dimensions[current_row].height = 22
                for col_idx in range(1, 13):
                    cell = ws.cell(row=current_row, column=col_idx)
                    cell.font = data_font
                    cell.border = cell_border
                    cell.alignment = Alignment(horizontal="center", vertical="center")

                current_row += 1

            # Column width settings (Safe with merged cells in openpyxl)
            from openpyxl.utils import get_column_letter

            for col_idx in range(1, 13):
                col_letter = get_column_letter(col_idx)
                max_len = 0
                for r in range(5, current_row):
                    cell_val = str(ws.cell(row=r, column=col_idx).value or '')
                    max_len = max(max_len, len(cell_val))
                ws.column_dimensions[col_letter].width = max(max_len + 4, 12)

            ws.column_dimensions["A"].width = 8   # Sr. No.
            ws.column_dimensions["B"].width = 20  # Admission Form No. (submission_id)
            ws.column_dimensions["C"].width = 18  # Application No.
            ws.column_dimensions["D"].width = 10  # Slot No
            ws.column_dimensions["E"].width = 24  # Slot Time

            response = HttpResponse(content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
            response['Content-Disposition'] = f'attachment; filename="{filename}.xlsx"'
            wb.save(response)
            return response

        # Format output list
        days_list = []
        for date_key in sorted(calendar_data.keys()):
            day_obj = calendar_data[date_key]
            slots_list = [day_obj["slots"][s] for s in [1, 2, 3, 4]]
            days_list.append({
                "date": day_obj["date"],
                "total_scheduled": day_obj["total_children"],
                "standard_capacity": 40,
                "max_admin_capacity": 52,
                "is_standard_full": day_obj["total_children"] >= 40,
                "is_full": day_obj["total_children"] >= 52,
                "slots": slots_list
            })

        from .slot_service import INTERACTION_START_DATE, INTERACTION_END_DATE, PUBLIC_HOLIDAYS, SLOT_DEFINITIONS

        holidays_list = [
            {"date": str(h_date), "name": h_name}
            for h_date, h_name in sorted(PUBLIC_HOLIDAYS.items())
        ]

        return Response({
            "status": "success",
            "total_days_scheduled": len(days_list),
            "schedule_rules": {
                "start_date": str(INTERACTION_START_DATE),
                "end_date": str(INTERACTION_END_DATE) if INTERACTION_END_DATE else None,
                "is_open_ended": INTERACTION_END_DATE is None,
                "working_days": "Monday to Saturday (Sundays & Public Holidays skipped)",
                "daily_standard_capacity": 40,
                "daily_max_admin_capacity": 52,
                "slots": SLOT_DEFINITIONS,
                "holidays": holidays_list
            },
            "calendar": days_list
        }, status=status.HTTP_200_OK)


class AdminTriggerInteractionReminderView(APIView):
    """
    Admin endpoint to trigger or test the 8:00 AM IST interaction reminders.
    POST /api/admin/interaction-reminders/trigger/
    Query/Body params:
      - date: YYYY-MM-DD (optional, defaults to tomorrow)
    """
    def post(self, request):
        denied = enforce_admin_token(request)
        if denied:
            return denied

        _, logged_in_admin, role = get_authenticated_admin_user(request)
        if role != 'ADMIN':
            return Response({
                "status": "error",
                "message": "Only Admins can trigger interaction reminders."
            }, status=status.HTTP_403_FORBIDDEN)

        target_date_str = request.data.get('date') or request.query_params.get('date')
        target_date = None
        if target_date_str:
            try:
                target_date = datetime.strptime(str(target_date_str).strip(), "%Y-%m-%d").date()
            except ValueError:
                return Response({
                    "status": "error",
                    "message": "Invalid date format. Expected YYYY-MM-DD."
                }, status=status.HTTP_400_BAD_REQUEST)

        from .reminder_worker import send_due_interaction_reminders
        result = send_due_interaction_reminders(target_date=target_date)

        return Response({
            "status": "success" if result.get("success") else "error",
            "result": result
        }, status=status.HTTP_200_OK if result.get("success") else status.HTTP_500_INTERNAL_SERVER_ERROR)


class AdminExportDataView(APIView):
    """
    Data Export API for Excel (.xlsx) and CSV:
    Query Parameters:
      - type: 'enquiries' | 'submitted' | 'pending' (default: 'enquiries')
      - period: 'today' | 'week' | 'month' | 'all' (default: 'all')
      - export_format (or file_format): 'excel' | 'csv' (default: 'excel')
    """
    def get(self, request):
        import csv
        from django.http import HttpResponse
        from openpyxl import Workbook
        from openpyxl.styles import Font, PatternFill, Alignment, Border, Side

        denied = enforce_admin_token(request)
        if denied:
            return denied

        _, logged_in_admin, role = get_authenticated_admin_user(request)

        export_type = (request.query_params.get("type") or request.GET.get("type") or "enquiries").lower().strip()
        period = (
            request.query_params.get("period") or
            request.query_params.get("time_range") or
            request.GET.get("period") or
            request.GET.get("time_range") or
            "all"
        ).lower().strip()
        # Accept 'export_format', 'file_format', or fallback to 'format'
        export_format = (
            request.query_params.get("export_format") or
            request.query_params.get("file_format") or
            request.GET.get("export_format") or
            request.GET.get("file_format") or
            request.GET.get("format") or
            "excel"
        ).lower().strip()

        now = timezone.now()
        start_date = None

        if period in ["today", "1day"]:
            start_date = now.replace(hour=0, minute=0, second=0, microsecond=0)
        elif period in ["week", "this_week", "this week", "7days"]:
            start_date = now - timedelta(days=7)
        elif period in ["month", "this_month", "this month", "30days"]:
            start_date = now - timedelta(days=30)

        # 1. Base Queryset Filtering
        if export_type in ["calendar", "interaction", "interaction_calendar", "interactions"]:
            filename_prefix = "Parameters_Of_Assessment_Part_Y"
            filter_date_str = request.query_params.get("date") or request.GET.get("date")
            filter_slot_param = request.query_params.get("slot") or request.query_params.get("slot_no") or request.GET.get("slot") or request.GET.get("slot_no")
            qs = AdmissionApplication.objects.filter(
                interaction_date__isnull=False
            ).select_related('submission').order_by('interaction_date', 'interaction_slot_no', 'created_at')

            if filter_date_str:
                qs = qs.filter(interaction_date=filter_date_str)
            elif start_date:
                qs = qs.filter(interaction_date__gte=start_date.date())

            if filter_slot_param and str(filter_slot_param).strip().isdigit():
                qs = qs.filter(interaction_slot_no=int(filter_slot_param))

            # Return specialized Part Y template format for Excel
            wb = Workbook()
            ws = wb.active
            ws.title = "Assessment - Part Y"
            ws.views.sheetView[0].showGridLines = True

            title_font = Font(name="Calibri", size=14, bold=True, underline="single", color="000000")
            header_font = Font(name="Calibri", size=10, bold=True, color="000000")
            header_fill = PatternFill(start_color="F2F4F7", end_color="F2F4F7", fill_type="solid")
            border_thin = Side(style='thin', color='000000')
            cell_border = Border(left=border_thin, right=border_thin, top=border_thin, bottom=border_thin)
            data_font = Font(name="Calibri", size=10)

            ws.merge_cells("A1:L1")
            title_cell = ws.cell(row=1, column=1, value="PARAMETERS OF ASSESSMENT – PART Y")
            title_cell.font = title_font
            title_cell.alignment = Alignment(horizontal="center", vertical="center")
            ws.row_dimensions[1].height = 28

            # Row 2: Subtitle Metadata (Date, and Slot if slot-filtered)
            sub_font = Font(name="Calibri", size=11, bold=True, color="000000")
            subtitle_parts = []
            if filter_date_str:
                subtitle_parts.append(f"Date: {filter_date_str}")
            if filter_slot_param:
                subtitle_parts.append(f"Slot: {filter_slot_param}")
            subtitle_text = " | ".join(subtitle_parts) if subtitle_parts else "Date: All Dates"

            ws.merge_cells("A2:L2")
            sub_cell = ws.cell(row=2, column=1, value=subtitle_text)
            sub_cell.font = sub_font
            sub_cell.alignment = Alignment(horizontal="left", vertical="center")
            ws.row_dimensions[2].height = 22

            headers_r3 = [
                ("A", "Sr. No.", True),
                ("B", "Admission Form No.", True),
                ("C", "Application No.", True),
                ("D", "Slot No", True),
                ("E", "Slot Time", True),
                ("F", "M", True),
                ("G", "MI", True),
                ("H:I", "Cognitive Assessment", False),
                ("J", "Affective Assessment", False),
                ("K", "Psychomotor Assessment", False),
                ("L", "Total", False),
            ]

            for col_letter, text, is_vert_merged in headers_r3:
                if is_vert_merged:
                    ws.merge_cells(f"{col_letter}3:{col_letter}4")
                    ws[f"{col_letter}3"] = text
                elif col_letter == "H:I":
                    ws.merge_cells("H3:I3")
                    ws["H3"] = text
                elif col_letter == "J":
                    ws["J3"] = text
                elif col_letter == "K":
                    ws["K3"] = text
                elif col_letter == "L":
                    ws["L3"] = text

            ws["H4"] = 5
            ws["I4"] = 5
            ws["J4"] = 10
            ws["K4"] = 10
            ws["L4"] = 30

            ws.row_dimensions[3].height = 24
            ws.row_dimensions[4].height = 20

            for r in [3, 4]:
                for c in range(1, 13):
                    cell = ws.cell(row=r, column=c)
                    cell.font = header_font
                    cell.fill = header_fill
                    cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
                    cell.border = cell_border

            current_row = 5
            for idx, app in enumerate(qs, start=1):
                slot_no_val = app.interaction_slot_no if app.interaction_slot_no else "-"
                slot_time_val = app.interaction_time or "-"
                sub_id_val = str(app.submission.id) if app.submission else (str(app.submission_id) if app.submission_id else "-")
                app_no_val = str(app.application_number or f"SF_{app.id}")

                m_val = app.evaluator_m or ""

                # Determine MI value: if evaluator_mi is provided, use it; otherwise check past_illness (Yes/No)
                if app.evaluator_mi:
                    mi_val = app.evaluator_mi
                else:
                    raw_illness = (app.past_illness or "").strip().lower()
                    # Remove common punctuation like dots, dashes, commas, slashes
                    cleaned_illness = re.sub(r"[\s\.\-_/\\,;:!]+", "", raw_illness)
                    negatives = {
                        "", "none", "no", "na", "nil", "null", "not", "nothing", "nope",
                        "noone", "notany", "noillness", "noissue", "noissues", "notapplicable",
                        "never", "good", "fine", "ok", "normal", "healthy", "fit", "allgood",
                        "નથી", "કોઈનથી", "કંઈનથી", "ના", "કઈનથી", "કશુનથી", "કશુંનથી",
                        "નહિ", "નહી", "બરાબર", "સાજુ", "સાજું", "તંદુરસ્ત"
                    }
                    if not cleaned_illness or cleaned_illness in negatives or raw_illness in negatives or "નથી" in raw_illness or "no illness" in raw_illness:
                        mi_val = "No"
                    else:
                        mi_val = "Yes"
                cog1_val = app.cognitive_assessment_1 if app.cognitive_assessment_1 > 0 else ""
                cog2_val = app.cognitive_assessment_2 if app.cognitive_assessment_2 > 0 else ""
                aff_val = app.affective_assessment if app.affective_assessment > 0 else ""
                psy_val = app.psychomotor_assessment if app.psychomotor_assessment > 0 else ""
                total_y_val = app.total_part_y_score if app.total_part_y_score > 0 else ""

                row_values = [
                    idx,
                    sub_id_val,
                    app_no_val,
                    slot_no_val,
                    slot_time_val,
                    m_val,
                    mi_val,
                    cog1_val,
                    cog2_val,
                    aff_val,
                    psy_val,
                    total_y_val
                ]

                ws.append(row_values)
                ws.row_dimensions[current_row].height = 22
                for col_idx in range(1, 13):
                    cell = ws.cell(row=current_row, column=col_idx)
                    cell.font = data_font
                    cell.border = cell_border
                    cell.alignment = Alignment(horizontal="center", vertical="center")

                current_row += 1

            from openpyxl.utils import get_column_letter

            for col_idx in range(1, 13):
                col_letter = get_column_letter(col_idx)
                max_len = 0
                for r in range(5, current_row):
                    cell_val = str(ws.cell(row=r, column=col_idx).value or '')
                    max_len = max(max_len, len(cell_val))
                ws.column_dimensions[col_letter].width = max(max_len + 4, 12)

            ws.column_dimensions["A"].width = 8
            ws.column_dimensions["B"].width = 20
            ws.column_dimensions["C"].width = 18
            ws.column_dimensions["D"].width = 10
            ws.column_dimensions["E"].width = 24

            filename_parts = [filename_prefix]
            if filter_date_str:
                filename_parts.append(filter_date_str)
            if filter_slot_param:
                filename_parts.append(f"Slot_{filter_slot_param}")
            filename_parts.append(timezone.now().strftime('%Y%m%d_%H%M%S'))
            filename = f"{'_'.join(filename_parts)}.xlsx"

            response = HttpResponse(content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
            response['Content-Disposition'] = f'attachment; filename="{filename}"'
            wb.save(response)
            return response

        elif export_type in ["submitted", "submissions", "submitted_applications"]:
            filename_prefix = "Submitted_Applications"
            qs = AdmissionApplication.objects.select_related('submission', 'submission__created_by').order_by("-created_at")
            if role != 'ADMIN' and logged_in_admin:
                qs = qs.filter(submission__created_by=logged_in_admin)
            if start_date:
                qs = qs.filter(created_at__gte=start_date)

            headers = [
                "Application No", "Submission ID", "Student Name", "Surname",
                "Standard", "Medium", "Vibhag", "Father Name", "Mother Name",
                "Father WhatsApp", "Father Email", "Interaction Date", "Interaction Slot",
                "Interaction Time", "Rules Agreed", "Added By", "Submitted At"
            ]
            rows = []
            for app in qs:
                sub = app.submission
                added_by = (sub.created_by.full_name or sub.created_by.username) if sub and sub.created_by else "Online / Self"
                rows.append([
                    app.application_number,
                    f"#{sub.id}" if sub else "-",
                    app.child_first_name,
                    app.child_surname,
                    app.standard,
                    app.medium,
                    app.vibhag,
                    app.father_name,
                    app.mother_name,
                    app.father_whatsapp or (sub.whatsapp_number if sub else ""),
                    app.father_email or (sub.email if sub else ""),
                    str(app.interaction_date) if app.interaction_date else "-",
                    f"Slot {app.interaction_slot_no}" if app.interaction_slot_no else "-",
                    app.interaction_time or "-",
                    "Yes" if app.agreed_to_rules else "No",
                    added_by,
                    app.created_at.strftime("%d-%m-%Y %H:%M") if app.created_at else "-"
                ])

        elif export_type in ["pending", "pending_forms"]:
            filename_prefix = "Pending_Admission_Forms"
            qs = QuizSubmission.objects.filter(
                payment_status='RECEIVED',
                admission_application__isnull=True
            ).select_related('created_by').order_by("-created_at")

            if role != 'ADMIN' and logged_in_admin:
                qs = qs.filter(created_by=logged_in_admin)
            if start_date:
                qs = qs.filter(created_at__gte=start_date)

            headers = [
                "Submission ID", "Student Name", "Surname", "Parent / Father Name",
                "Standard", "Medium", "Vibhag", "WhatsApp Number", "Email",
                "Quiz Score", "QA Status", "Payment Status", "1-Time Reminder Sent",
                "Added By", "Registered Date"
            ]
            rows = []
            for sub in qs:
                added_by = (sub.created_by.full_name or sub.created_by.username) if sub.created_by else "Online / Self"
                rows.append([
                    f"#{sub.id}",
                    sub.child_name,
                    sub.surname,
                    sub.parent_name,
                    sub.standard,
                    sub.medium,
                    sub.vibhag,
                    sub.whatsapp_number,
                    sub.email,
                    f"{sub.score}/7",
                    sub.qa_status,
                    sub.payment_status,
                    sub.reminder_sent_at.strftime("%d-%m-%Y %H:%M") if sub.reminder_sent_at else "No",
                    added_by,
                    sub.created_at.strftime("%d-%m-%Y %H:%M") if sub.created_at else "-"
                ])

        else:  # Default: all enquiries
            filename_prefix = "Enquiries"
            qs = QuizSubmission.objects.select_related('created_by', 'admission_application').order_by("-created_at")

            if role != 'ADMIN' and logged_in_admin:
                qs = qs.filter(created_by=logged_in_admin)
            if start_date:
                qs = qs.filter(created_at__gte=start_date)

            headers = [
                "Submission ID", "Student Name", "Surname", "Parent / Father Name",
                "Standard", "Medium", "Vibhag", "WhatsApp Number", "Email",
                "Quiz Score", "Performance Tag", "QA Status", "Payment Status",
                "Form Submitted?", "Application Number", "Added By", "Created At"
            ]
            rows = []
            for sub in qs:
                added_by = (sub.created_by.full_name or sub.created_by.username) if sub.created_by else "Online / Self"
                app_no = sub.admission_application.application_number if hasattr(sub, 'admission_application') and sub.admission_application else "-"
                rows.append([
                    f"#{sub.id}",
                    sub.child_name,
                    sub.surname,
                    sub.parent_name,
                    sub.standard,
                    sub.medium,
                    sub.vibhag,
                    sub.whatsapp_number,
                    sub.email,
                    f"{sub.score}/7",
                    sub.tag or "-",
                    sub.qa_status,
                    sub.payment_status,
                    "Yes" if hasattr(sub, 'admission_application') and sub.admission_application else "No",
                    app_no,
                    added_by,
                    sub.created_at.strftime("%d-%m-%Y %H:%M") if sub.created_at else "-"
                ])

        filename = f"{filename_prefix}_{period}_{timezone.now().strftime('%Y%m%d_%H%M%S')}"

        # 2. Return CSV Format
        if export_format == "csv":
            response = HttpResponse(content_type='text/csv')
            response['Content-Disposition'] = f'attachment; filename="{filename}.csv"'
            writer = csv.writer(response)
            writer.writerow(headers)
            for row in rows:
                writer.writerow(row)
            return response

        # 3. Return Excel (.xlsx) Format with Styling
        wb = Workbook()
        ws = wb.active
        ws.title = filename_prefix[:30]

        # Header styling: Dark Blue background, bold white text
        header_font = Font(name="Calibri", size=11, bold=True, color="FFFFFF")
        header_fill = PatternFill(start_color="1E3A8A", end_color="1E3A8A", fill_type="solid")
        header_align = Alignment(horizontal="center", vertical="center", wrap_text=True)

        ws.append(headers)
        ws.row_dimensions[1].height = 28

        for col_idx in range(1, len(headers) + 1):
            cell = ws.cell(row=1, column=col_idx)
            cell.font = header_font
            cell.fill = header_fill
            cell.alignment = header_align

        # Append Data Rows with border and zebra striping
        thin_border = Border(
            left=Side(style='thin', color='E2E8F0'),
            right=Side(style='thin', color='E2E8F0'),
            top=Side(style='thin', color='E2E8F0'),
            bottom=Side(style='thin', color='E2E8F0')
        )
        data_font = Font(name="Calibri", size=10)
        alt_fill = PatternFill(start_color="F8FAFC", end_color="F8FAFC", fill_type="solid")

        for r_idx, row in enumerate(rows, start=2):
            ws.append(row)
            ws.row_dimensions[r_idx].height = 20
            for c_idx in range(1, len(row) + 1):
                cell = ws.cell(row=r_idx, column=c_idx)
                cell.font = data_font
                cell.border = thin_border
                cell.alignment = Alignment(vertical="center")
                if r_idx % 2 == 0:
                    cell.fill = alt_fill

        # Auto-fit column widths safely
        for col in ws.columns:
            max_len = 0
            for cell in col:
                val = str(cell.value or "")
                if len(val) > max_len:
                    max_len = len(val)
            col_letter = col[0].column_letter
            ws.column_dimensions[col_letter].width = max(max_len + 4, 14)

        import io
        output = io.BytesIO()
        wb.save(output)
        output.seek(0)

        response = HttpResponse(
            output.getvalue(),
            content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
        )
        response['Content-Disposition'] = f'attachment; filename="{filename}.xlsx"'
        # Expose Content-Disposition header so frontend Axios/Fetch can read filename
        response['Access-Control-Expose-Headers'] = 'Content-Disposition'
        return response


class AdminProxyDownloadView(APIView):
    """
    Proxy Download API for Azure Blob Storage files:
    Resolves CORS issues when downloading photos, signatures, or documents from frontend.
    GET /api/admin/download-file/?url=<blob_url>&filename=<custom_name>
    """
    def get(self, request):
        denied = enforce_admin_token(request)
        if denied:
            return denied

        file_url = (request.query_params.get("url") or "").strip()
        custom_filename = (request.query_params.get("filename") or "").strip()

        if not file_url:
            return Response({
                "status": "error",
                "message": "Query parameter 'url' is required."
            }, status=status.HTTP_400_BAD_REQUEST)

        # Security check: Allow files from Azure Blob Storage or local media
        from urllib.parse import urlparse
        import mimetypes
        import requests
        from django.http import HttpResponse, StreamingHttpResponse

        parsed = urlparse(file_url)
        allowed_hosts = [
            f"{settings.AZURE_ACCOUNT_NAME}.blob.core.windows.net",
            "shreyasfoundation.blob.core.windows.net",
            "localhost",
            "127.0.0.1"
        ]

        if parsed.netloc and not any(allowed in parsed.netloc.lower() for allowed in allowed_hosts):
            return Response({
                "status": "error",
                "message": f"Downloads are only permitted from verified storage hosts ({', '.join(allowed_hosts)})."
            }, status=status.HTTP_403_FORBIDDEN)

        # Derive clean filename if not specified
        if not custom_filename:
            path_segments = parsed.path.rstrip('/').split('/')
            custom_filename = path_segments[-1] if path_segments else "downloaded_file"

        # Guess mime type
        content_type, _ = mimetypes.guess_type(custom_filename)
        content_type = content_type or "application/octet-stream"

        # If Azure Blob URL from our storage account and account key is available,
        # attach a temporary SAS token so private/restricted blobs can be securely read by backend
        download_target_url = file_url
        if settings.AZURE_ACCOUNT_NAME and settings.AZURE_ACCOUNT_KEY and settings.AZURE_ACCOUNT_NAME in parsed.netloc:
            # Check if SAS token already present in query params
            if "sig=" not in file_url:
                try:
                    from datetime import datetime, timedelta, timezone as dt_tz
                    from azure.storage.blob import generate_blob_sas, BlobSasPermissions
                    
                    # Extract blob_name by removing leading container name from path
                    # e.g., /media/documents/photo.jpg -> documents/photo.jpg
                    clean_path = parsed.path.lstrip('/')
                    container_prefix = f"{settings.AZURE_CONTAINER}/"
                    if clean_path.startswith(container_prefix):
                        blob_name = clean_path[len(container_prefix):]
                    else:
                        blob_name = clean_path

                    sas_token = generate_blob_sas(
                        account_name=settings.AZURE_ACCOUNT_NAME,
                        container_name=settings.AZURE_CONTAINER,
                        blob_name=blob_name,
                        account_key=settings.AZURE_ACCOUNT_KEY,
                        permission=BlobSasPermissions(read=True),
                        expiry=datetime.now(dt_tz.utc) + timedelta(minutes=15)
                    )
                    separator = "&" if "?" in file_url else "?"
                    download_target_url = f"{file_url}{separator}{sas_token}"
                except Exception as sas_err:
                    logger.warning(f"[PROXY DOWNLOAD] Could not generate SAS token: {sas_err}")

        try:
            r = requests.get(download_target_url, stream=True, timeout=25)
            if r.status_code != 200:
                logger.error(f"[PROXY DOWNLOAD ERROR] Storage returned status {r.status_code} for {file_url}")
                return Response({
                    "status": "error",
                    "message": f"Failed to fetch file from storage (HTTP {r.status_code})."
                }, status=status.HTTP_502_BAD_GATEWAY)

            response = StreamingHttpResponse(
                r.iter_content(chunk_size=8192),
                content_type=r.headers.get('Content-Type') or content_type
            )
            response['Content-Disposition'] = f'attachment; filename="{custom_filename}"'
            response['Access-Control-Expose-Headers'] = 'Content-Disposition'
            return response

        except Exception as e:
            logger.exception(f"[PROXY DOWNLOAD EXCEPTION] {str(e)}")
            return Response({
                "status": "error",
                "message": f"Error downloading file: {str(e)}"
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class AdminAuditLogView(APIView):
    """
    Audit Logs: Strictly restricted to users with role 'ADMIN'.
    GET /api/admin/audit-logs/
    """
    def get(self, request):
        denied = enforce_admin_token(request, required_role='ADMIN')
        if denied:
            return denied

        queryset = AuditLog.objects.all().order_by('-created_at')

        method = request.query_params.get("method")
        if method:
            queryset = queryset.filter(method__iexact=method)

        actor = request.query_params.get("actor")
        if actor:
            queryset = queryset.filter(
                Q(actor_email__icontains=actor) | Q(actor_username__icontains=actor)
            )

        endpoint = request.query_params.get("endpoint")
        if endpoint:
            queryset = queryset.filter(endpoint__icontains=endpoint)

        page = int(request.query_params.get("page", 1))
        page_size = int(request.query_params.get("page_size", request.query_params.get("limit", 50)))
        if page < 1:
            page = 1
        if page_size < 1:
            page_size = 50

        total_records = queryset.count()
        total_pages = (total_records + page_size - 1) // page_size if total_records > 0 else 1

        offset = (page - 1) * page_size
        logs_slice = queryset[offset:offset + page_size]

        data = [{
            "id": log.id,
            "actor_username": log.actor_username,
            "actor_email": log.actor_email,
            "actor_role": log.actor_role,
            "method": log.method,
            "endpoint": log.endpoint,
            "query_params": log.query_params,
            "request_body": log.request_body,
            "response_status": log.response_status,
            "ip_address": log.ip_address,
            "user_agent": log.user_agent,
            "created_at": log.created_at
        } for log in logs_slice]

        return Response({
            "status": "success",
            "pagination": {
                "page": page,
                "page_size": page_size,
                "total_records": total_records,
                "total_pages": total_pages,
                "has_next": page < total_pages,
                "has_previous": page > 1
            },
            "total_logs": total_records,
            "returned_logs": len(data),
            "logs": data
        }, status=status.HTTP_200_OK)


# ==============================================================================
# Enquiries & Quiz Evaluation
# ==============================================================================

class SubmitQuizView(APIView):
    """
    GET: List all quiz submissions (Enquiries) for Admin Dashboard (Token Required)
         OR Fetch Questionnaire structure with ?view=questions (Public)
    POST: Submit enquiry (Admin side with Token) OR Quiz answers (Parent side with submission_id)
    """
    def get(self, request):
        if request.query_params.get("view") == "questions" or request.query_params.get("action") == "questions":
            lang = request.query_params.get("lang", "en").lower()
            return Response({
                "status": "success",
                "academic_year": "2027–28",
                "title": "SHREYAS FOUNDATION - PARENTAL UNDERSTANDING & PARTNERSHIP QUESTIONNAIRE",
                "total_marks": TOTAL_QUESTIONNAIRE_MARKS,
                "mcq_marks": TOTAL_MCQ_MARKS,
                "written_marks": 20,
                "language": lang,
                "questions": QUESTIONNAIRE_QUESTIONS
            }, status=status.HTTP_200_OK)

        denied = enforce_admin_token(request)
        if denied:
            return denied

        _, logged_in_admin, role = get_authenticated_admin_user(request)

        sub_id = request.query_params.get("id")
        if sub_id:
            submission = get_object_or_404(QuizSubmission, id=sub_id)
            serializer = QuizSubmissionListSerializer(submission)
            return Response({"status": "success", "data": serializer.data}, status=status.HTTP_200_OK)

        base_queryset = QuizSubmission.objects.all()
        if role != 'ADMIN' and logged_in_admin:
            base_queryset = base_queryset.filter(created_by=logged_in_admin)

        # FAST HIGH-PERFORMANCE AGGREGATION: Single SQL query instead of 8 separate DB hits
        stats = base_queryset.aggregate(
            all_total=Count('id'),
            all_fees_collected=Count(Case(When(payment_status='RECEIVED', then=1), output_field=IntegerField())),
            all_qa_passed=Count(Case(When(qa_status='PASSED', then=1), output_field=IntegerField())),
            all_qa_failed=Count(Case(When(qa_status='FAILED', then=1), output_field=IntegerField())),
            all_qa_pending=Count(Case(When(qa_status='PENDING', then=1), output_field=IntegerField())),
            all_submitted_forms=Count(Case(When(admission_application__isnull=False, then=1), output_field=IntegerField())),
            all_pending_forms=Count(Case(When(payment_status='RECEIVED', admission_application__isnull=True, then=1), output_field=IntegerField()))
        )

        all_total_enquiries = stats['all_total'] or 0
        all_fees_collected = stats['all_fees_collected'] or 0
        all_fee_unpaid = all_total_enquiries - all_fees_collected
        all_qa_passed = stats['all_qa_passed'] or 0
        all_qa_failed = stats['all_qa_failed'] or 0
        all_qa_pending = stats['all_qa_pending'] or 0
        all_submitted_forms = stats['all_submitted_forms'] or 0
        all_pending_forms = stats['all_pending_forms'] or 0

        queryset = base_queryset.select_related('created_by', 'admission_application').order_by("-created_at")

        vibhag = request.query_params.get("vibhag")
        if vibhag:
            queryset = queryset.filter(vibhag__iexact=vibhag)

        standard = request.query_params.get("standard")
        if standard:
            queryset = queryset.filter(standard__iexact=standard)

        medium = request.query_params.get("medium")
        if medium:
            queryset = queryset.filter(medium__iexact=medium)

        is_eligible = request.query_params.get("is_eligible")
        if is_eligible is not None:
            queryset = queryset.filter(is_eligible=(is_eligible.lower() == 'true'))

        payment_status = request.query_params.get("payment_status")
        if payment_status:
            queryset = queryset.filter(payment_status__iexact=payment_status)

        # Global Search Query (?search=... or ?q=...)
        search_query = (request.query_params.get("search") or request.query_params.get("q") or "").strip()
        if search_query:
            clean_search_digits = re.sub(r"\D", "", search_query)
            search_filter = (
                Q(child_name__icontains=search_query) |
                Q(parent_name__icontains=search_query) |
                Q(surname__icontains=search_query) |
                Q(whatsapp_number__icontains=search_query) |
                Q(email__icontains=search_query) |
                Q(standard__icontains=search_query) |
                Q(vibhag__icontains=search_query)
            )
            if clean_search_digits:
                search_filter |= Q(whatsapp_number__icontains=clean_search_digits)
            if search_query.isdigit():
                search_filter |= Q(id=int(search_query))
            queryset = queryset.filter(search_filter)

        # Quick Dropdown Filter (?filter=... or ?status=...)
        # Matches frontend dropdown options:
        # 'all', 'fees_paid', 'fees_unpaid', 'quiz_passed', 'quiz_failed', 'quiz_pending', 'pending_forms', 'submitted_forms'
        dropdown_filter = (request.query_params.get("filter") or request.query_params.get("status") or "").strip().lower()
        if dropdown_filter in ['fees_paid', 'fees paid', 'paid']:
            queryset = queryset.filter(payment_status="RECEIVED")
        elif dropdown_filter in ['fees_unpaid', 'fees unpaid', 'unpaid', 'fee unpaid']:
            queryset = queryset.exclude(payment_status="RECEIVED")
        elif dropdown_filter in ['quiz_passed', 'quiz passed', 'passed']:
            queryset = queryset.filter(qa_status="PASSED")
        elif dropdown_filter in ['quiz_failed', 'quiz failed', 'failed']:
            queryset = queryset.filter(qa_status="FAILED")
        elif dropdown_filter in ['quiz_pending', 'quiz pending']:
            queryset = queryset.filter(qa_status="PENDING")
        elif dropdown_filter in ['pending_forms', 'pending forms', 'pending']:
            queryset = queryset.filter(payment_status="RECEIVED", admission_application__isnull=True)
        elif dropdown_filter in ['submitted_forms', 'submitted forms', 'submitted']:
            queryset = queryset.filter(admission_application__isnull=False)

        # Time range filter (?time_range=... or ?period=...)
        # Matches 'today', 'this_week', 'this_month', 'all_time'
        time_range = (request.query_params.get("time_range") or request.query_params.get("period") or "").strip().lower()
        now = timezone.now()
        if time_range in ['today', '1day']:
            start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
            queryset = queryset.filter(created_at__gte=start_of_day)
        elif time_range in ['this_week', 'week', '7days']:
            start_of_week = now - timedelta(days=7)
            queryset = queryset.filter(created_at__gte=start_of_week)
        elif time_range in ['this_month', 'month', '30days']:
            start_of_month = now - timedelta(days=30)
            queryset = queryset.filter(created_at__gte=start_of_month)

        # Filtered count and stats: Single SQL aggregate query (Zero server overhead)
        filtered_stats = queryset.aggregate(
            filtered_count=Count('id'),
            filtered_fees_collected=Count(Case(When(payment_status='RECEIVED', then=1), output_field=IntegerField())),
            filtered_qa_passed=Count(Case(When(qa_status='PASSED', then=1), output_field=IntegerField())),
            filtered_qa_failed=Count(Case(When(qa_status='FAILED', then=1), output_field=IntegerField())),
            filtered_qa_pending=Count(Case(When(qa_status='PENDING', then=1), output_field=IntegerField())),
            filtered_submitted_forms=Count(Case(When(admission_application__isnull=False, then=1), output_field=IntegerField())),
            filtered_pending_forms=Count(Case(When(payment_status='RECEIVED', admission_application__isnull=True, then=1), output_field=IntegerField()))
        )

        filtered_count = filtered_stats['filtered_count'] or 0
        filtered_fees_collected = filtered_stats['filtered_fees_collected'] or 0
        filtered_qa_passed = filtered_stats['filtered_qa_passed'] or 0
        filtered_qa_failed = filtered_stats['filtered_qa_failed'] or 0
        filtered_qa_pending = filtered_stats['filtered_qa_pending'] or 0
        filtered_submitted_forms = filtered_stats['filtered_submitted_forms'] or 0
        filtered_pending_forms = filtered_stats['filtered_pending_forms'] or 0

        # Pagination support (defaults to page 1, 50 records per page)
        page = int(request.query_params.get("page", 1))
        page_size = int(request.query_params.get("page_size", request.query_params.get("limit", 50)))
        if page < 1:
            page = 1
        if page_size < 1:
            page_size = 50

        total_pages = (filtered_count + page_size - 1) // page_size if filtered_count > 0 else 1
        offset = (page - 1) * page_size
        paged_list = list(queryset[offset:offset + page_size])

        # Precompute repeated enquiry flags in 1 batch query to prevent N+1 queries (100 queries per page -> 1 query)
        if paged_list:
            phones_10 = set()
            for obj in paged_list:
                clean_p = "".join(filter(str.isdigit, str(obj.whatsapp_number)))
                if clean_p:
                    phones_10.add(clean_p[-10:] if len(clean_p) >= 10 else clean_p)
            if phones_10:
                phone_q = Q()
                for p10 in phones_10:
                    phone_q |= Q(whatsapp_number__endswith=p10)
                matching_rows = QuizSubmission.objects.filter(phone_q).values_list('id', 'whatsapp_number', 'child_name')
                pair_ids = {}
                for r_id, r_phone, r_child in matching_rows:
                    cp = "".join(filter(str.isdigit, str(r_phone)))
                    p10 = cp[-10:] if len(cp) >= 10 else cp
                    key = (p10, (r_child or "").strip().lower())
                    pair_ids.setdefault(key, []).append(r_id)
                for obj in paged_list:
                    cp = "".join(filter(str.isdigit, str(obj.whatsapp_number)))
                    p10 = cp[-10:] if len(cp) >= 10 else cp
                    key = (p10, (obj.child_name or "").strip().lower())
                    ids_for_pair = pair_ids.get(key, [])
                    obj._is_repeated_enquiry = any(i < obj.id for i in ids_for_pair)
                    obj._repeated_badge = "Repeated Enquiry" if len(ids_for_pair) > 1 else None

        serializer = QuizSubmissionListSerializer(paged_list, many=True)

        return Response({
            "status": "success",
            "scope": "ALL" if role == 'ADMIN' else "MY_ENQUIRIES",
            "user_role": role,
            "pagination": {
                "page": page,
                "page_size": page_size,
                "total_records": filtered_count,
                "total_pages": total_pages,
                "has_next": page < total_pages,
                "has_previous": page > 1
            },
            # Top Stats Cards / Tabs: Always All-Over Total Counts (NOT cut by page or table filter)
            "total_enquiries": all_total_enquiries,
            "fees_collected": all_fees_collected,
            "fee_unpaid": all_fee_unpaid,
            "qa_passed": all_qa_passed,
            "qa_failed": all_qa_failed,
            "qa_pending": all_qa_pending,
            "submitted_forms": all_submitted_forms,
            "pending_forms": all_pending_forms,
            # Filtered / Table statistics
            "filtered_records": filtered_count,
            "returned_enquiries": len(serializer.data),
            "filtered_stats": {
                "total": filtered_count,
                "fees_collected": filtered_fees_collected,
                "fee_unpaid": filtered_count - filtered_fees_collected,
                "qa_passed": filtered_qa_passed,
                "qa_failed": filtered_qa_failed,
                "qa_pending": filtered_qa_pending,
                "submitted_forms": filtered_submitted_forms,
                "pending_forms": filtered_pending_forms
            },
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
        standard = data.get("standard", "Nursery") or "Nursery"
        medium = data.get("medium", "Gujarati") or "Gujarati"
        parent_name = (data.get("parent_name") or data.get("father_name") or "").strip()
        child_name = data.get("child_name", "").strip()
        surname = data.get("surname", "").strip()
        vibhag = data.get("vibhag", "Shishu")
        payment_status_input = str(data.get("payment_status", "RECEIVED")).upper()
        if payment_status_input not in ['RECEIVED', 'PENDING']:
            payment_status_input = 'RECEIVED'
        language = data.get("language", "en") or "en"
        whatsapp_number = data.get("whatsapp_number", "")
        email = data.get("email", "")
        answers = data.get("answers", {})

        # Existing enquiry: Parent is submitting the 7 MCQs
        if submission_id:
            submission = QuizSubmission.objects.filter(id=submission_id).first()
            if not submission:
                return Response({
                    "status": "error",
                    "message": f"Enquiry with ID {submission_id} not found."
                }, status=status.HTTP_404_NOT_FOUND)

            existing_app = AdmissionApplication.objects.filter(submission=submission).first()
            if existing_app:
                return Response({
                    "status": "error",
                    "already_submitted": True,
                    "application_number": existing_app.application_number,
                    "message": f"Admission form already submitted (App No: {existing_app.application_number})."
                }, status=status.HTTP_400_BAD_REQUEST)

            if submission.qa_status in ["PASSED", "FAILED"]:
                return Response({
                    "status": "error",
                    "already_completed": True,
                    "submission_id": submission.id,
                    "score": submission.score,
                    "tag": submission.tag,
                    "message": f"Quiz has already been completed for this student with score {submission.score}/7 ({submission.tag}). Re-attempting the quiz is not allowed."
                }, status=status.HTTP_400_BAD_REQUEST)

            # Grade the questionnaire using 2027-28 Parental Partnership Evaluation Rubric (with AI grading for Q11-Q14)
            eval_result = score_parent_questionnaire(
                answers=answers,
                use_ai=True,
                child_name=submission.child_name,
                standard=submission.standard
            )
            mcq_marks = eval_result["mcq_score"]  # Out of 30
            written_marks = eval_result["written_score"]  # Out of 20
            total_score = eval_result["total_score"]  # Out of 50
            breakdown = eval_result["breakdown"]

            # Store AI evaluations inside answers for future educator review
            if eval_result.get("ai_evaluations"):
                answers["ai_evaluations"] = eval_result["ai_evaluations"]

            # Eligibility: Minimum 25 marks out of 50 (50%) to pass screening
            is_eligible = total_score >= 25
            qa_status = "PASSED" if is_eligible else "FAILED"
            tag = get_performance_tag(int(total_score), max_score=50)

            submission.answers = answers
            # Store total score out of 50 in score field
            submission.score = int(total_score)
            submission.is_eligible = is_eligible
            submission.qa_status = qa_status
            submission.tag = tag
            submission.quiz_completed_at = timezone.now()
            if language:
                submission.language = language
            submission.save()

            logger.info(f"[POST /api/submit-quiz/] Existing Submission {submission.id} graded: MCQ={mcq_marks}/30, Written={written_marks}/20, Total={total_score}/50 ({tag}), QA: {qa_status}")

            return Response({
                "status": "success",
                "submission_id": submission.id,
                "vibhag": submission.vibhag,
                "language": submission.language or "en",
                "qa_status": qa_status,
                "score": int(total_score),
                "total": 50,
                "mcq_score": mcq_marks,
                "written_score": written_marks,
                "tag": tag,
                "can_fill_form": True,
                "payment_status": submission.payment_status,
                "message": f"Questionnaire completed! Score: {int(total_score)}/50 ({tag}). You can now proceed to fill the admission form.",
                "breakdown": breakdown,
                "written_responses": eval_result.get("written_responses", {}),
                "ai_evaluations": eval_result.get("ai_evaluations", {})
            }, status=status.HTTP_200_OK)

        # New enquiry creation (Admin Side) -> Token is mandatory
        denied = enforce_admin_token(request)
        if denied:
            return denied

        _, logged_in_admin, _ = get_authenticated_admin_user(request)

        clean_phone = normalize_phone_number(whatsapp_number)
        clean_phone_10 = clean_phone[-10:] if len(clean_phone) >= 10 else clean_phone

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

        has_answers = bool(answers and len(answers) > 0)
        score = 0
        breakdown = {}

        if has_answers:
            eval_result = score_parent_questionnaire(answers)
            score = int(eval_result["total_score"])
            mcq_marks = eval_result["mcq_score"]
            # Eligibility: Minimum 25 marks out of 50 (50%) to pass screening
            is_eligible = score >= 25
            qa_status = "PASSED" if is_eligible else "FAILED"
            tag = get_performance_tag(score, max_score=50)
            breakdown = eval_result["breakdown"]
            quiz_completed_at = timezone.now()
        else:
            score = 0
            is_eligible = False
            qa_status = "PENDING"
            tag = "Pending QA"
            quiz_completed_at = None

        # Decide notification language:
        # 1. If explicit 'language' is passed (gu / en), use it.
        # 2. Otherwise auto-determine from 'medium': Gujarati medium -> 'gu', English medium -> 'en'.
        notif_language = "gu"
        if data.get("language") and data.get("language") in ["gu", "en"]:
            notif_language = data.get("language")
        elif str(medium).lower().startswith("eng"):
            notif_language = "en"
        else:
            notif_language = "gu"

        submission = QuizSubmission.objects.create(
            standard=standard,
            medium=medium,
            parent_name=parent_name,
            child_name=child_name,
            surname=surname,
            vibhag=vibhag,
            language=notif_language,
            whatsapp_number=whatsapp_number,
            email=email,
            answers=answers,
            score=score,
            tag=tag,
            qa_status=qa_status,
            is_eligible=is_eligible,
            quiz_completed_at=quiz_completed_at,
            payment_status=payment_status_input,
            created_by=logged_in_admin
        )

        logger.info(f"[POST /api/submit-quiz/] Created Submission ID: {submission.id}, Standard: {standard}, Medium: {medium}, Language: {notif_language}, Payment: {payment_status_input}")

        base_url = (data.get("frontend_base_url") or getattr(settings, 'FRONTEND_BASE_URL', 'https://g53rw5qd-5173.inc1.devtunnels.ms')).rstrip('/')
        form_direct_url = f"{base_url}/admission-form/?submission_id={submission.id}"

        wa_result = {}
        if payment_status_input == 'RECEIVED':
            # Send both: 1. Admission form link template, 2. School booklet PDF template (in parent's chosen language: gu/en)
            wa_result = send_payment_received_whatsapp_notifications(
                submission.whatsapp_number,
                submission.id,
                language=notif_language
            )
            if wa_result.get("overall_success"):
                submission.whatsapp_link_sent = True
                submission.save(update_fields=['whatsapp_link_sent'])
                logger.info(f"[POST /api/submit-quiz/] WhatsApp notifications sent automatically to {submission.whatsapp_number} (Link: {wa_result.get('link_sent')}, Booklet: {wa_result.get('booklet_sent')})")
            else:
                logger.warning(f"[POST /api/submit-quiz/] Auto WhatsApp send failed: Link={wa_result.get('link_result')}, Booklet={wa_result.get('booklet_result')}")

        if not has_answers:
            message = f"Enquiry created for {child_name} (Std: {standard}, Med: {medium}). Payment: {payment_status_input}."
        elif is_eligible:
            message = f"Congratulations! You scored {score}/50 ({tag}) for Vibhag: {vibhag}. You are eligible to fill the admission form."
        else:
            message = f"You have scored {score}/50 ({tag}). Minimum 25 marks (50%) are required to be eligible for admission."

        return Response({
            "status": "success",
            "submission_id": submission.id,
            "id": submission.id,
            "parent_name": submission.parent_name,
            "father_name": submission.parent_name,
            "child_name": submission.child_name,
            "student_name": submission.child_name,
            "surname": submission.surname,
            "whatsapp_number": submission.whatsapp_number,
            "email": submission.email,
            "medium": submission.medium,
            "standard": submission.standard,
            "vibhag": submission.vibhag,
            "payment_status": submission.payment_status,
            "language": submission.language or "en",
            "form_url": form_direct_url if submission.payment_status == 'RECEIVED' else "",
            "whatsapp_link_sent": submission.whatsapp_link_sent,
            "whatsapp_dispatch": {
                "triggered": payment_status_input == 'RECEIVED',
                "link_sent": wa_result.get("link_sent", False),
                "booklet_sent": wa_result.get("booklet_sent", False),
                "template_language": submission.language or language or "gu",
                "link_result": wa_result.get("link_result"),
                "booklet_result": wa_result.get("booklet_result")
            },
            "qa_status": qa_status,
            "score": score,
            "total": 50,
            "tag": tag,
            "can_fill_form": is_eligible,
            "added_by": (logged_in_admin.full_name or logged_in_admin.username) if logged_in_admin else "Online / Self",
            "created_by": {
                "id": logged_in_admin.id,
                "username": logged_in_admin.username,
                "full_name": logged_in_admin.full_name,
                "email": logged_in_admin.email,
                "role": logged_in_admin.role
            } if logged_in_admin else None,
            "is_repeated_enquiry": QuizSubmission.objects.filter(
                whatsapp_number__endswith=clean_phone_10,
                child_name__iexact=child_name.strip(),
                id__lt=submission.id
            ).exists(),
            "repeated_badge": "Repeated Enquiry" if QuizSubmission.objects.filter(
                whatsapp_number__endswith=clean_phone_10,
                child_name__iexact=child_name.strip()
            ).count() > 1 else None,
            "message": message,
            "breakdown": breakdown,
            "submitted_data": {
                "parent_name": submission.parent_name,
                "child_name": submission.child_name,
                "surname": submission.surname,
                "whatsapp_number": submission.whatsapp_number,
                "email": submission.email,
                "medium": submission.medium,
                "standard": submission.standard,
                "vibhag": submission.vibhag,
                "payment_status": submission.payment_status
            }
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

        if payment_status_input != "RECEIVED":
            logger.warning(f"[POST /api/verify-payment/] Payment status is {payment_status_input}, not RECEIVED.")
            submission.payment_status = payment_status_input
            submission.save(update_fields=['payment_status', 'payment_transaction_id'])
            return Response({
                "status": "error",
                "submission_id": submission.id,
                "payment_status": submission.payment_status,
                "whatsapp_link_sent": False,
                "message": "Payment not received! Admission form link will not be sent to WhatsApp."
            }, status=status.HTTP_400_BAD_REQUEST)

        submission.payment_status = "RECEIVED"
        submission.save(update_fields=['payment_status', 'payment_transaction_id'])
        logger.info(f"[POST /api/verify-payment/] Successfully updated DB: submission {submission.id} payment_status is now RECEIVED.")

        base_url = frontend_base_url.rstrip("/") if frontend_base_url else request.build_absolute_uri('/')[:-1]
        form_direct_url = f"{base_url}/admission-form/?submission_id={submission.id}"

        logger.info(f"[POST /api/verify-payment/] Triggering WhatsApp notifications (Form Link + Booklet PDF) to {submission.whatsapp_number}...")
        wa_result = send_payment_received_whatsapp_notifications(
            submission.whatsapp_number,
            submission.id,
            language=submission.language or "gu"
        )
        submission.whatsapp_link_sent = wa_result.get("overall_success", False)
        submission.save(update_fields=['whatsapp_link_sent'])

        if not wa_result.get("overall_success"):
            logger.error(f"[POST /api/verify-payment/] WhatsApp Template API Failed: Link={wa_result.get('link_result')}, Booklet={wa_result.get('booklet_result')}")
            return Response({
                "status": "error",
                "submission_id": submission.id,
                "vibhag": submission.vibhag,
                "payment_status": "RECEIVED",
                "whatsapp_link_sent": False,
                "whatsapp_error": wa_result,
                "message": "Payment received, but WhatsApp template message delivery failed."
            }, status=status.HTTP_502_BAD_GATEWAY)

        logger.info(f"[POST /api/verify-payment/] WhatsApp messages delivered successfully to {submission.whatsapp_number}!")
        return Response({
            "status": "success",
            "submission_id": submission.id,
            "vibhag": submission.vibhag,
            "payment_status": "RECEIVED",
            "whatsapp_link_sent": True,
            "whatsapp_number": submission.whatsapp_number,
            "form_url": form_direct_url,
            "link_sent": wa_result.get("link_sent"),
            "booklet_sent": wa_result.get("booklet_sent"),
            "message": f"Payment received! Admission form link and school booklet PDF sent to WhatsApp ({submission.whatsapp_number})."
        }, status=status.HTTP_200_OK)


# ==============================================================================
# Admission Form & Pre-fill 
# ==============================================================================

ADMISSION_FORM_OTP_TTL_SECONDS = 300  # 5 minutes (matches Meta 'otp' template validity period)
ADMISSION_FORM_OTP_RESEND_COOLDOWN_SECONDS = 60


def _extract_form_access_token(request) -> str:
    token = (
        request.headers.get("X-Form-Token")
        or request.META.get("HTTP_X_FORM_TOKEN")
        or request.query_params.get("form_access_token")
        or ""
    ).strip()
    if token:
        return token
    auth_header = (request.headers.get("Authorization") or request.META.get("HTTP_AUTHORIZATION") or "").strip()
    if auth_header.lower().startswith("bearer "):
        return auth_header
    if hasattr(request, "data") and isinstance(request.data, dict):
        body_token = str(request.data.get("form_access_token") or "").strip()
        if body_token:
            return body_token
    return ""


def _is_submission_otp_verified(request, submission: QuizSubmission) -> bool:
    """
    Strictly requires a valid signed ADMISSION_FORM access token (issued on OTP verify)
    for this specific submission_id. Does NOT bypass via DB flag so every new tab or
    incognito session opening the link must verify OTP again.
    """
    from .jwt_auth import verify_form_access_token
    token = _extract_form_access_token(request)
    if token and verify_form_access_token(token, expected_submission_id=submission.id):
        return True
    return False


def _build_eligible_form_payload(submission: QuizSubmission) -> dict:
    return {
        "status": "success",
        "can_fill_form": True,
        "otp_required": False,
        "otp_verified": True,
        "already_submitted": False,
        "submission_id": submission.id,
        "language": submission.language or "en",
        "score": submission.score,
        "tag": submission.tag,
        "vibhag": submission.vibhag,
        "message": "Eligible to fill admission form.",
        "draft_saved_at": submission.last_draft_saved_at,
        "saved_draft": submission.admission_form_draft or {},
        "saved_quiz_draft": submission.quiz_draft_answers or {},
        "prefilled_data": {
            "standard": submission.standard,
            "medium": submission.medium,
            "vibhag": submission.vibhag,
            "parent_name": submission.parent_name,
            "father_name": submission.parent_name,
            "student_name": submission.child_name,
            "child_name": submission.child_name,
            "child_first_name": submission.child_name,
            "surname": submission.surname,
            "child_surname": submission.surname,
            "father_surname": submission.surname,
            "mother_surname": submission.surname,
            "whatsapp_number": submission.whatsapp_number,
            "father_whatsapp": submission.whatsapp_number,
            "mother_whatsapp": submission.whatsapp_number,
            "email": submission.email,
            "father_email": submission.email,
            "mother_email": submission.email,
            "language": submission.language or "gu",
            "is_locked_for_user": True,
        },
    }


def _issue_and_send_form_otp(submission: QuizSubmission, force_resend: bool = False) -> dict:
    import secrets
    from datetime import timedelta
    from .whatsapp import mask_phone_number, send_whatsapp_otp_template

    now = timezone.now()
    phone = (submission.whatsapp_number or "").strip()
    masked_phone = mask_phone_number(phone) if phone else ""

    if not phone:
        return {
            "otp_sent": False,
            "already_sent_recently": False,
            "masked_phone": "",
            "otp_expires_in_seconds": 0,
            "error": "No WhatsApp number is registered for this submission.",
        }

    # Avoid duplicate sends on rapid page reloads / React StrictMode double-fetches
    if (
        not force_resend
        and submission.form_otp_code
        and submission.form_otp_expires_at
        and submission.form_otp_expires_at > now + timedelta(
            seconds=ADMISSION_FORM_OTP_TTL_SECONDS - ADMISSION_FORM_OTP_RESEND_COOLDOWN_SECONDS
        )
    ):
        remaining = max(1, int((submission.form_otp_expires_at - now).total_seconds()))
        return {
            "otp_sent": True,
            "already_sent_recently": True,
            "masked_phone": masked_phone,
            "otp_expires_in_seconds": remaining,
        }

    otp_code = f"{secrets.randbelow(900000) + 100000}"
    submission.form_otp_code = otp_code
    submission.form_otp_expires_at = now + timedelta(seconds=ADMISSION_FORM_OTP_TTL_SECONDS)
    submission.save(update_fields=["form_otp_code", "form_otp_expires_at"])

    wa_result = send_whatsapp_otp_template(
        recipient_number=phone,
        otp_code=otp_code,
        submission_id=submission.id,
    )

    return {
        "otp_sent": bool(wa_result.get("success")),
        "already_sent_recently": False,
        "masked_phone": masked_phone,
        "otp_expires_in_seconds": ADMISSION_FORM_OTP_TTL_SECONDS,
        "template": wa_result.get("template", ""),
        "whatsapp_result": wa_result,
    }


class AdmissionFormEligibilityCheckView(APIView):
    """
    Check if a submission_id is eligible, paid, and hasn't already submitted before opening form.
    When link is opened and OTP is not yet verified, automatically sends a WhatsApp OTP via
    the OTP template and returns status='otp_required' until verified.
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

        # OTP Gate FIRST: For ANY valid submission_id, send OTP to its registered WhatsApp number
        # as soon as the link is opened if not yet verified.
        if not _is_submission_otp_verified(request, submission):
            resend_flag = str(request.query_params.get("resend", "")).lower() in ("1", "true", "yes")
            otp_info = _issue_and_send_form_otp(submission, force_resend=resend_flag)
            logger.info(
                f"[GET /api/admission-form/check-eligibility/] Submission {submission_id} requires OTP verification "
                f"(otp_sent={otp_info.get('otp_sent')}, masked_phone={otp_info.get('masked_phone')})."
            )
            return Response({
                "status": "otp_required",
                "otp_required": True,
                "otp_verified": False,
                "can_fill_form": False,
                "already_submitted": False,
                "submission_id": submission.id,
                "language": submission.language or "en",
                "masked_phone": otp_info.get("masked_phone", ""),
                "otp_sent": otp_info.get("otp_sent", False),
                "otp_expires_in_seconds": otp_info.get("otp_expires_in_seconds", ADMISSION_FORM_OTP_TTL_SECONDS),
                "message": (
                    "OTP has been sent to your registered WhatsApp number. Please verify OTP to access the admission form."
                    if otp_info.get("otp_sent")
                    else otp_info.get("error", "Unable to send OTP to your WhatsApp number. Please try resending.")
                ),
            }, status=status.HTTP_200_OK)

        if submission.payment_status != "RECEIVED":
            logger.warning(f"[GET /api/admission-form/check-eligibility/] Submission {submission_id} payment is {submission.payment_status}.")
            return Response({
                "status": "error",
                "otp_required": False,
                "otp_verified": True,
                "can_fill_form": False,
                "payment_status": submission.payment_status,
                "message": "Payment is not yet received. Please complete fee payment to unlock the admission form."
            }, status=status.HTTP_402_PAYMENT_REQUIRED)

        existing_application = AdmissionApplication.objects.filter(submission=submission).first()
        if existing_application:
            logger.warning(f"[GET /api/admission-form/check-eligibility/] Form already submitted with Application No: {existing_application.application_number}")
            return Response({
                "status": "already_submitted",
                "otp_required": False,
                "otp_verified": True,
                "can_fill_form": False,
                "already_submitted": True,
                "application_number": existing_application.application_number,
                "submitted_at": existing_application.created_at,
                "message": f"This admission form has already been submitted (Application No: {existing_application.application_number}). You cannot submit it again."
            }, status=status.HTTP_200_OK)

        logger.info(f"[GET /api/admission-form/check-eligibility/] Submission {submission_id} is VALID, OTP-VERIFIED, and ELIGIBLE to open form.")
        return Response(_build_eligible_form_payload(submission), status=status.HTTP_200_OK)


class AdmissionFormSendOTPView(APIView):
    """
    POST /api/admission-form/send-otp/
    Explicitly sends or resends a 6-digit WhatsApp OTP via Meta Authentication template
    to the registered WhatsApp number of any valid QuizSubmission.
    Body: { "submission_id": 1 }
    """
    def post(self, request):
        submission_id = request.data.get("submission_id")
        if not submission_id:
            return Response({
                "status": "error",
                "message": "submission_id is required."
            }, status=status.HTTP_400_BAD_REQUEST)

        submission = QuizSubmission.objects.filter(id=submission_id).first()
        if not submission:
            return Response({
                "status": "error",
                "message": f"Quiz submission {submission_id} does not exist."
            }, status=status.HTTP_404_NOT_FOUND)

        otp_info = _issue_and_send_form_otp(submission, force_resend=True)
        if not otp_info.get("otp_sent"):
            return Response({
                "status": "error",
                "otp_sent": False,
                "masked_phone": otp_info.get("masked_phone", ""),
                "message": otp_info.get("error", "Failed to send WhatsApp OTP. Please try again."),
            }, status=status.HTTP_502_BAD_GATEWAY if submission.whatsapp_number else status.HTTP_400_BAD_REQUEST)

        return Response({
            "status": "success",
            "otp_sent": True,
            "submission_id": submission.id,
            "masked_phone": otp_info.get("masked_phone", ""),
            "otp_expires_in_seconds": otp_info.get("otp_expires_in_seconds", ADMISSION_FORM_OTP_TTL_SECONDS),
            "message": "OTP sent successfully to your registered WhatsApp number.",
        }, status=status.HTTP_200_OK)


class AdmissionFormVerifyOTPView(APIView):
    """
    POST /api/admission-form/verify-otp/
    Verifies the 6-digit WhatsApp OTP and unlocks access to the Admission Form.
    Body: { "submission_id": 1, "otp": "123456" }
    """
    def post(self, request):
        submission_id = request.data.get("submission_id")
        otp_input = str(request.data.get("otp") or "").strip()

        if not submission_id or not otp_input:
            return Response({
                "status": "error",
                "message": "Both submission_id and otp are required."
            }, status=status.HTTP_400_BAD_REQUEST)

        submission = QuizSubmission.objects.filter(id=submission_id).first()
        if not submission:
            return Response({
                "status": "error",
                "message": f"Quiz submission {submission_id} does not exist."
            }, status=status.HTTP_404_NOT_FOUND)

        if not submission.form_otp_code:
            return Response({
                "status": "error",
                "error_code": "OTP_NOT_REQUESTED",
                "message": "No active OTP found. Please request a new OTP."
            }, status=status.HTTP_400_BAD_REQUEST)

        if submission.form_otp_expires_at and timezone.now() > submission.form_otp_expires_at:
            return Response({
                "status": "error",
                "error_code": "OTP_EXPIRED",
                "message": "OTP has expired. Please request a new OTP."
            }, status=status.HTTP_400_BAD_REQUEST)

        if str(submission.form_otp_code).strip() != otp_input:
            return Response({
                "status": "error",
                "error_code": "INVALID_OTP",
                "message": "Invalid OTP. Please check your WhatsApp message and try again."
            }, status=status.HTTP_400_BAD_REQUEST)

        # Mark verified and clear OTP
        submission.form_otp_verified = True
        submission.form_otp_verified_at = timezone.now()
        submission.form_otp_code = None
        submission.form_otp_expires_at = None
        submission.save(update_fields=[
            "form_otp_verified",
            "form_otp_verified_at",
            "form_otp_code",
            "form_otp_expires_at",
        ])

        from .jwt_auth import generate_form_access_token
        form_access_token = generate_form_access_token(submission.id, submission.whatsapp_number)

        payload = _build_eligible_form_payload(submission)
        payload["message"] = "OTP verified successfully. You can now access the admission form."
        payload["form_access_token"] = form_access_token

        logger.info(f"[POST /api/admission-form/verify-otp/] OTP verified for submission_id={submission.id}.")
        return Response(payload, status=status.HTTP_200_OK)


class AdmissionFormAutoSaveDraftView(APIView):
    """
    Auto-save Draft API for Parents filling Questionnaire or Admission Form.
    Endpoint: /api/admission-form/autosave/
    Methods:
      GET: Retrieve auto-saved draft for a submission_id
           Query: ?submission_id=123&type=all (or type=quiz / type=form)
      POST: Auto-save draft progress as user types or advances steps
           Body:
           {
               "submission_id": 123,
               "draft_type": "all",  // "quiz", "form", or "all"
               "quiz_draft": { "1": "B", "2": "A", "11": "..." },
               "form_draft": { "birth_place_city": "Ahmedabad", "father_occupation": "Doctor", ... }
           }
    """
    def get(self, request):
        submission_id = request.query_params.get("submission_id")
        if not submission_id:
            return Response({
                "status": "error",
                "message": "submission_id query parameter is required."
            }, status=status.HTTP_400_BAD_REQUEST)

        submission = QuizSubmission.objects.filter(id=submission_id).first()
        if not submission:
            return Response({
                "status": "error",
                "message": f"Submission {submission_id} not found."
            }, status=status.HTTP_404_NOT_FOUND)

        if not _is_submission_otp_verified(request, submission):
            return Response({
                "status": "error",
                "error_code": "OTP_VERIFICATION_REQUIRED",
                "otp_required": True,
                "message": "Please verify the OTP sent to your WhatsApp number before accessing the admission form."
            }, status=status.HTTP_403_FORBIDDEN)

        draft_type = (request.query_params.get("type") or "all").lower()
        response_data = {
            "status": "success",
            "submission_id": submission.id,
            "last_draft_saved_at": submission.last_draft_saved_at,
            "qa_status": submission.qa_status,
            "has_submitted_final_form": hasattr(submission, 'admission_application') and bool(submission.admission_application)
        }

        if draft_type in ["quiz", "all"]:
            response_data["quiz_draft"] = submission.quiz_draft_answers or {}
        if draft_type in ["form", "all"]:
            response_data["form_draft"] = submission.admission_form_draft or {}

        return Response(response_data, status=status.HTTP_200_OK)

    def post(self, request):
        try:
            submission_id = request.data.get("submission_id")
            if not submission_id:
                return Response({
                    "status": "error",
                    "error_code": "MISSING_SUBMISSION_ID",
                    "message": "submission_id is required in the request payload for autosaving."
                }, status=status.HTTP_400_BAD_REQUEST)

            # Ensure valid integer submission_id
            try:
                sub_id_int = int(submission_id)
            except (ValueError, TypeError):
                return Response({
                    "status": "error",
                    "error_code": "INVALID_SUBMISSION_ID",
                    "message": f"Invalid submission_id '{submission_id}'. Must be a valid integer ID."
                }, status=status.HTTP_400_BAD_REQUEST)

            submission = QuizSubmission.objects.filter(id=sub_id_int).first()
            if not submission:
                return Response({
                    "status": "error",
                    "error_code": "SUBMISSION_NOT_FOUND",
                    "message": f"Quiz submission with ID {sub_id_int} does not exist."
                }, status=status.HTTP_404_NOT_FOUND)

            if not _is_submission_otp_verified(request, submission):
                return Response({
                    "status": "error",
                    "error_code": "OTP_VERIFICATION_REQUIRED",
                    "otp_required": True,
                    "message": "Please verify the OTP sent to your WhatsApp number before saving the admission form."
                }, status=status.HTTP_403_FORBIDDEN)

            # If final application already submitted, don't allow modifying draft
            if hasattr(submission, 'admission_application') and submission.admission_application:
                return Response({
                    "status": "error",
                    "error_code": "ALREADY_SUBMITTED",
                    "already_submitted": True,
                    "application_number": submission.admission_application.application_number,
                    "message": f"Admission application already submitted (App No: {submission.admission_application.application_number}). Draft can no longer be edited."
                }, status=status.HTTP_409_CONFLICT)

            draft_type = str(request.data.get("draft_type") or "all").lower().strip()
            if draft_type not in ["quiz", "form", "all"]:
                return Response({
                    "status": "error",
                    "error_code": "INVALID_DRAFT_TYPE",
                    "message": f"Invalid draft_type '{draft_type}'. Must be one of: 'quiz', 'form', or 'all'."
                }, status=status.HTTP_400_BAD_REQUEST)

            quiz_draft = request.data.get("quiz_draft")
            form_draft = request.data.get("form_draft")

            if quiz_draft is None and form_draft is None:
                return Response({
                    "status": "error",
                    "error_code": "NO_DRAFT_DATA",
                    "message": "Neither 'quiz_draft' nor 'form_draft' data was provided to save."
                }, status=status.HTTP_400_BAD_REQUEST)

            updated_fields = ['last_draft_saved_at']
            submission.last_draft_saved_at = timezone.now()

            # Validate quiz_draft is dict if provided
            if quiz_draft is not None:
                if not isinstance(quiz_draft, dict):
                    return Response({
                        "status": "error",
                        "error_code": "INVALID_QUIZ_FORMAT",
                        "message": "'quiz_draft' must be a valid key-value JSON object."
                    }, status=status.HTTP_400_BAD_REQUEST)
                if draft_type in ["quiz", "all"]:
                    current_q = submission.quiz_draft_answers or {}
                    current_q.update(quiz_draft)
                    submission.quiz_draft_answers = current_q
                    updated_fields.append('quiz_draft_answers')

            # Validate form_draft is dict if provided
            if form_draft is not None:
                if not isinstance(form_draft, dict):
                    return Response({
                        "status": "error",
                        "error_code": "INVALID_FORM_FORMAT",
                        "message": "'form_draft' must be a valid key-value JSON object."
                    }, status=status.HTTP_400_BAD_REQUEST)
                if draft_type in ["form", "all"]:
                    current_f = submission.admission_form_draft or {}
                    current_f.update(form_draft)
                    submission.admission_form_draft = current_f
                    updated_fields.append('admission_form_draft')

            submission.save(update_fields=updated_fields)
            logger.info(f"[AUTOSAVE SUCCESS] Submission {sub_id_int} draft saved at {submission.last_draft_saved_at}")

            return Response({
                "status": "success",
                "message": "Draft auto-saved successfully.",
                "submission_id": submission.id,
                "last_draft_saved_at": submission.last_draft_saved_at,
                "quiz_draft_saved": bool(quiz_draft is not None),
                "form_draft_saved": bool(form_draft is not None)
            }, status=status.HTTP_200_OK)

        except Exception as e:
            logger.exception(f"[AUTOSAVE UNEXPECTED ERROR] {str(e)}")
            return Response({
                "status": "error",
                "error_code": "SERVER_ERROR",
                "message": "An unexpected server error occurred while auto-saving the draft. Please try again."
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class AdmissionApplicationView(APIView):
    """
    GET: List all submitted admission forms (Admin - Token Required)
    POST: Submit completed admission form with signatures and documents (Parent - submission_id)
    PATCH: Admin endpoint to edit/update details (Admin - Token Required)
    DELETE: Admin endpoint to delete application/enquiry (Admin - Token Required)
    """
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    def get(self, request):
        # 1. If an ordinary browser (parent) opens this URL directly without an Authorization header,
        # redirect them seamlessly to the live frontend form on shreyas.jmstech.co!
        auth_header = request.headers.get('Authorization') or request.META.get('HTTP_AUTHORIZATION')
        submission_id = request.query_params.get("submission_id")
        accept_header = request.headers.get('Accept', '').lower()

        if not auth_header and ('text/html' in accept_header or not accept_header):
            import os
            from django.shortcuts import redirect
            frontend_base = (os.getenv("FRONTEND_BASE_URL") or "https://shreyas.jmstech.co").rstrip('/')
            target_url = f"{frontend_base}/admission-form/"
            if submission_id:
                target_url = f"{target_url}?submission_id={submission_id}"
            logger.info(f"[BROWSER REDIRECT] Redirecting parent to frontend: {target_url}")
            return redirect(target_url)

        denied = enforce_admin_token(request)
        if denied:
            return denied

        _, logged_in_admin, role = get_authenticated_admin_user(request)

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

        applications = AdmissionApplication.objects.select_related('submission', 'submission__created_by').order_by("-created_at")

        # Role-based scoping for Submitted Applications:
        # ADMIN sees all applications.
        # Other roles (STAFF / ACADEMICIAN) see only applications originating from their enquiries.
        if role != 'ADMIN' and logged_in_admin:
            applications = applications.filter(submission__created_by=logged_in_admin)

        # Filters for Admission Applications
        standard = request.query_params.get("standard")
        if standard:
            applications = applications.filter(standard__iexact=standard)

        vibhag = request.query_params.get("vibhag")
        if vibhag:
            applications = applications.filter(vibhag__iexact=vibhag)

        medium = request.query_params.get("medium")
        if medium:
            applications = applications.filter(medium__iexact=medium)

        admission_status = request.query_params.get("admission_status")
        if admission_status:
            applications = applications.filter(admission_status__iexact=admission_status)

        # Quick Pill Filter (?filter=... or ?status=...)
        # Matches frontend pills: 'all', 'pass' / 'passed', 'fail' / 'failed'
        tab_filter = (request.query_params.get("filter") or request.query_params.get("status") or "").strip().lower()
        if tab_filter in ['pass', 'passed']:
            applications = applications.filter(submission__qa_status="PASSED")
        elif tab_filter in ['fail', 'failed']:
            applications = applications.filter(submission__qa_status="FAILED")

        # Time range filter (?time_range=... or ?period=...)
        # Matches frontend dropdown: 'today', 'this_week', 'this_month', 'all_time'
        time_range = (request.query_params.get("time_range") or request.query_params.get("period") or "").strip().lower()
        now = timezone.now()
        if time_range in ['today', '1day']:
            start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
            applications = applications.filter(created_at__gte=start_of_day)
        elif time_range in ['this_week', 'this week', 'week', '7days']:
            start_of_week = now - timedelta(days=7)
            applications = applications.filter(created_at__gte=start_of_week)
        elif time_range in ['this_month', 'this month', 'month', '30days']:
            start_of_month = now - timedelta(days=30)
            applications = applications.filter(created_at__gte=start_of_month)

        # Search Query (?search=... or ?q=...)
        search_query = (request.query_params.get("search") or request.query_params.get("q") or "").strip()
        if search_query:
            clean_search_digits = re.sub(r"\D", "", search_query)
            app_search_filter = (
                Q(application_number__icontains=search_query) |
                Q(child_first_name__icontains=search_query) |
                Q(child_father_name__icontains=search_query) |
                Q(child_surname__icontains=search_query) |
                Q(mother_name__icontains=search_query) |
                Q(mother_surname__icontains=search_query) |
                Q(mother_whatsapp__icontains=search_query) |
                Q(submission__parent_name__icontains=search_query) |
                Q(submission__whatsapp_number__icontains=search_query) |
                Q(standard__icontains=search_query) |
                Q(vibhag__icontains=search_query)
            )
            if clean_search_digits:
                app_search_filter |= Q(mother_whatsapp__icontains=clean_search_digits)
                app_search_filter |= Q(submission__whatsapp_number__icontains=clean_search_digits)
            if search_query.isdigit():
                app_search_filter |= Q(id=int(search_query))
                app_search_filter |= Q(submission_id=int(search_query))
            applications = applications.filter(app_search_filter)

        # Summary Counts (for UI badges: All, Pass, Fail) - Single SQL aggregation query
        base_qs = AdmissionApplication.objects.all()
        if role != 'ADMIN' and logged_in_admin:
            base_qs = base_qs.filter(submission__created_by=logged_in_admin)
        app_counts = base_qs.aggregate(
            all_count=Count('id'),
            pass_count=Count(Case(When(submission__qa_status="PASSED", then=1), output_field=IntegerField())),
            fail_count=Count(Case(When(submission__qa_status="FAILED", then=1), output_field=IntegerField()))
        )
        all_count = app_counts['all_count'] or 0
        pass_count = app_counts['pass_count'] or 0
        fail_count = app_counts['fail_count'] or 0

        # Pagination support (defaults to page 1, 50 records per page)
        total_count = applications.count()
        page = int(request.query_params.get("page", 1))
        page_size = int(request.query_params.get("page_size", request.query_params.get("limit", 50)))
        if page < 1:
            page = 1
        if page_size < 1:
            page_size = 50

        total_pages = (total_count + page_size - 1) // page_size if total_count > 0 else 1
        offset = (page - 1) * page_size
        paged_applications = applications[offset:offset + page_size]

        serializer = AdmissionApplicationSerializer(paged_applications, many=True)
        return Response({
            "status": "success",
            "scope": "ALL" if role == 'ADMIN' else "MY_APPLICATIONS",
            "user_role": role,
            "counts": {
                "all": all_count,
                "pass": pass_count,
                "fail": fail_count
            },
            "pagination": {
                "page": page,
                "page_size": page_size,
                "total_records": total_count,
                "total_pages": total_pages,
                "has_next": page < total_pages,
                "has_previous": page > 1
            },
            "total_applications": total_count,
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

        if not _is_submission_otp_verified(request, submission):
            logger.warning(f"[POST /api/admission-form/] Submission {submission_id} attempted submit without OTP verification.")
            return Response({
                "status": "error",
                "error_code": "OTP_VERIFICATION_REQUIRED",
                "otp_required": True,
                "message": "Please verify the OTP sent to your WhatsApp number before submitting the admission form."
            }, status=status.HTTP_403_FORBIDDEN)

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
            # 1. Save Application & Calculate Part X (Quiz out of 50) + Reward Marks (Alak Malak + Sibling VK)
            application = serializer.save(submission=submission, form_submitted_at=timezone.now())
            from .evaluation_service import calculate_part_x_score
            calculate_part_x_score(application, save=True)
            logger.info(f"[POST /api/admission-form/] Form SUBMITTED SUCCESSFULLY! Application Number: {application.application_number}")

            # 2. Dynamic Interaction Slot Auto-Allocation (10 kids/slot, 4 slots/day, Mon-Sat)
            from .slot_service import allocate_next_available_interaction_slot
            from .whatsapp import send_whatsapp_interaction_scheduled_message

            slot_info = allocate_next_available_interaction_slot()
            application.interaction_date = slot_info["date"]
            application.interaction_slot_no = slot_info["slot_no"]
            application.interaction_time = slot_info["time"]
            application.interaction_venue = slot_info["venue"]

            # 3. Send WhatsApp Notification to Parent
            recipient_phone = submission.whatsapp_number or application.father_whatsapp or application.mother_whatsapp
            wa_sent = False
            wa_response = {}
            if recipient_phone:
                wa_response = send_whatsapp_interaction_scheduled_message(
                    recipient_number=recipient_phone,
                    date_str=slot_info["date_str"],
                    slot_no=slot_info["slot_no"],
                    time_str=slot_info["time"],
                    template_name="shreyas_interaction_scheduled_"
                )
                wa_sent = wa_response.get("success", False)
                application.interaction_whatsapp_sent = wa_sent

            application.save(update_fields=[
                'interaction_date',
                'interaction_slot_no',
                'interaction_time',
                'interaction_venue',
                'interaction_whatsapp_sent'
            ])

            logger.info(
                f"[POST /api/admission-form/] Interaction scheduled: Date={slot_info['date_str']}, "
                f"Slot={slot_info['slot_no']} ({slot_info['time']}), WhatsApp sent={wa_sent}"
            )

            return Response({
                "status": "success",
                "message": "Admission form submitted and stored successfully!",
                "application_id": application.id,
                "application_number": application.application_number,
                "vibhag": application.vibhag,
                "interaction_schedule": {
                    "date": slot_info["date_str"],
                    "slot_no": slot_info["slot_no"],
                    "time": slot_info["time"],
                    "venue": slot_info["venue"],
                    "whatsapp_sent": wa_sent,
                    "whatsapp_response": wa_response
                },
                "data": serializer.data
            }, status=status.HTTP_201_CREATED)

        logger.error(f"[POST /api/admission-form/] VALIDATION FAILED! Errors: {serializer.errors}")

        # Human-readable error message summarizing file format/upload issues for the user
        readable_errors = []
        for field, err_list in serializer.errors.items():
            first_err = str(err_list[0]) if isinstance(err_list, (list, tuple)) and len(err_list) > 0 else str(err_list)
            if 'upload a valid image' in first_err.lower():
                readable_errors.append(f"Field '{field}': Please upload a valid image file (.jpg, .jpeg, .png, .webp). PDF files or Word documents are not accepted for photographs/signatures.")
            elif 'invalid format' in first_err.lower() or 'only image files' in first_err.lower():
                readable_errors.append(f"Field '{field}': {first_err}")
            else:
                readable_errors.append(f"{field}: {first_err}")

        return Response({
            "status": "error",
            "message": " | ".join(readable_errors) if readable_errors else "Validation failed for admission application form.",
            "errors": serializer.errors
        }, status=status.HTTP_400_BAD_REQUEST)

    def patch(self, request):
        # Allow any authenticated Admin/Staff/Academician to initiate the update request
        denied = enforce_admin_token(request)
        if denied:
            return denied

        _, logged_in_admin, role = get_authenticated_admin_user(request)

        app_id = request.data.get("id") or request.query_params.get("id")
        submission_id = request.data.get("submission_id") or request.query_params.get("submission_id")

        if not app_id and not submission_id:
            return Response({
                "status": "error",
                "message": "Either 'id' or 'submission_id' is required to update an application or enquiry."
            }, status=status.HTTP_400_BAD_REQUEST)

        application = None
        submission = None

        if app_id:
            application = AdmissionApplication.objects.filter(id=app_id).first()
            if not application:
                return Response({
                    "status": "error",
                    "message": f"AdmissionApplication with ID {app_id} not found."
                }, status=status.HTTP_404_NOT_FOUND)
            submission = application.submission
        elif submission_id:
            submission = QuizSubmission.objects.filter(id=submission_id).first()
            application = AdmissionApplication.objects.filter(submission_id=submission_id).first()
            if not submission and not application:
                return Response({
                    "status": "error",
                    "message": f"No Enquiry or Application found for submission_id {submission_id}."
                }, status=status.HTTP_404_NOT_FOUND)

        # STRICT ROLE CHECK:
        # If an AdmissionApplication exists (the full admission form has been submitted),
        # ONLY users with role 'ADMIN' can edit it. Staff & Academicians are blocked.
        if application and role != 'ADMIN':
            return Response({
                "status": "error",
                "message": "Access restricted. Completed admission application forms can only be edited by Admin. Staff and Academicians can only edit enquiries."
            }, status=status.HTTP_403_FORBIDDEN)

        # 1. Update QuizSubmission fields (Allowed for Admin, Staff, Academicians)
        phone_updated = False
        new_phone = (
            request.data.get('father_whatsapp') or
            request.data.get('mother_whatsapp') or
            request.data.get('whatsapp_number') or
            request.data.get('phone')
        )

        if submission:
            if new_phone and str(new_phone).strip() != submission.whatsapp_number:
                submission.whatsapp_number = str(new_phone).strip()
                phone_updated = True

            if 'child_first_name' in request.data:
                submission.child_name = str(request.data['child_first_name']).strip()
            elif 'child_name' in request.data:
                submission.child_name = str(request.data['child_name']).strip()

            if 'child_surname' in request.data:
                submission.surname = str(request.data['child_surname']).strip()
            elif 'surname' in request.data:
                submission.surname = str(request.data['surname']).strip()

            if 'father_first_name' in request.data:
                submission.parent_name = str(request.data['father_first_name']).strip()
            elif 'parent_name' in request.data:
                submission.parent_name = str(request.data['parent_name']).strip()

            if 'vibhag' in request.data:
                v = str(request.data['vibhag']).strip()
                # Map class number/name to vibhag if needed
                vibhag_map = {'1': 'Arun', '2': 'Arun', '3': 'Kesari', '4': 'Kesari', '5': 'Kesari', '6': 'Kesari', '7': 'Kesari', '8': 'Kishore', '9': 'Kishore', '10': 'Kishore', 'nursery': 'Shishu', 'jr.kg': 'Shishu', 'sr.kg': 'Arun'}
                submission.vibhag = vibhag_map.get(v.lower(), v.capitalize() if v.capitalize() in ['Shishu', 'Arun', 'Kesari', 'Kishore'] else submission.vibhag)

            if 'standard' in request.data:
                submission.standard = str(request.data['standard']).strip()

            if 'medium' in request.data:
                submission.medium = str(request.data['medium']).strip()

            if 'father_email' in request.data:
                submission.email = str(request.data['father_email']).strip()
            elif 'email' in request.data:
                submission.email = str(request.data['email']).strip()

            if 'payment_status' in request.data:
                submission.payment_status = str(request.data['payment_status']).strip().upper()

            submission.save()
            logger.info(f"[PATCH /api/admission-form/] Updated QuizSubmission {submission.id} (Phone: {submission.whatsapp_number})")

            # If phone changed and payment is RECEIVED, automatically trigger WhatsApp template link & booklet!
            if phone_updated and submission.payment_status == 'RECEIVED':
                logger.info(f"[PATCH /api/admission-form/] Phone updated to {submission.whatsapp_number}. Re-triggering WhatsApp notifications...")
                wa_res = send_payment_received_whatsapp_notifications(
                    submission.whatsapp_number,
                    submission.id,
                    language=submission.language or "gu"
                )
                if wa_res.get("overall_success"):
                    submission.whatsapp_link_sent = True
                    submission.save(update_fields=['whatsapp_link_sent'])

        # 2. If AdmissionApplication exists, update it too
        if application:
            # Handle Admin custom slot assignment (Date and Slot No with buffer up to 13)
            slot_changed = False
            custom_date = request.data.get('interaction_date')
            custom_slot_no = request.data.get('interaction_slot_no')

            if custom_date and custom_slot_no:
                from .slot_service import validate_and_assign_admin_slot
                from datetime import datetime
                try:
                    if isinstance(custom_date, str):
                        try:
                            parsed_date = datetime.strptime(custom_date, "%Y-%m-%d").date()
                        except ValueError:
                            parsed_date = datetime.strptime(custom_date, "%d-%m-%Y").date()
                    else:
                        parsed_date = custom_date

                    assigned_slot = validate_and_assign_admin_slot(
                        application=application,
                        chosen_date=parsed_date,
                        chosen_slot_no=int(custom_slot_no)
                    )
                    application.interaction_date = assigned_slot["date"]
                    application.interaction_slot_no = assigned_slot["slot_no"]
                    application.interaction_time = assigned_slot["time"]
                    application.interaction_venue = assigned_slot["venue"]
                    slot_changed = True
                    logger.info(f"[ADMIN SLOT ASSIGNED] App {application.application_number} manually assigned to {assigned_slot['date_str']} Slot {assigned_slot['slot_no']} (Position {assigned_slot['booked_count']}/13)")
                except Exception as e:
                    return Response({
                        "status": "error",
                        "message": str(e)
                    }, status=status.HTTP_400_BAD_REQUEST)

            serializer = AdmissionApplicationSerializer(
                application,
                data=request.data,
                partial=True,
                context={'request': request, 'is_admin_update': True}
            )
            if serializer.is_valid():
                updated_app = serializer.save()
                if slot_changed:
                    updated_app.interaction_date = application.interaction_date
                    updated_app.interaction_slot_no = application.interaction_slot_no
                    updated_app.interaction_time = application.interaction_time
                    updated_app.interaction_venue = application.interaction_venue

                    # Send WhatsApp notification to parent with newly rescheduled slot
                    from .whatsapp import send_whatsapp_interaction_scheduled_message
                    parent_phone = updated_app.father_whatsapp or (submission.whatsapp_number if submission else "") or updated_app.mother_whatsapp
                    wa_sent = False
                    wa_response = {}
                    if parent_phone:
                        slot_date_str = updated_app.interaction_date.strftime("%d/%m/%Y")
                        wa_response = send_whatsapp_interaction_scheduled_message(
                            recipient_number=parent_phone,
                            date_str=slot_date_str,
                            slot_no=updated_app.interaction_slot_no,
                            time_str=updated_app.interaction_time,
                            template_name="shreyas_interaction_rescheduled_"
                        )
                        wa_sent = wa_response.get("success", False)
                        updated_app.interaction_whatsapp_sent = wa_sent
                        logger.info(f"[ADMIN SLOT NOTIF] WhatsApp template 'shreyas_interaction_rescheduled_' dispatched to {parent_phone}: success={wa_sent}")

                    updated_app.save(update_fields=[
                        'interaction_date',
                        'interaction_slot_no',
                        'interaction_time',
                        'interaction_venue',
                        'interaction_whatsapp_sent'
                    ])
                if phone_updated:
                    updated_app.father_whatsapp = submission.whatsapp_number
                    updated_app.save(update_fields=['father_whatsapp'])

                # Recalculate Part X + Part Y + Reward Marks and Merit Rank if score/bonus fields were updated
                from .evaluation_service import calculate_part_x_score, save_part_y_evaluation
                if 'part_y_score' in request.data or 'total_part_y_score' in request.data:
                    raw_py = request.data.get('part_y_score') if 'part_y_score' in request.data else request.data.get('total_part_y_score')
                    try:
                        save_part_y_evaluation(
                            application=updated_app,
                            evaluator_user=logged_in_admin,
                            m_tag=request.data.get('evaluator_m', updated_app.evaluator_m),
                            mi_tag=request.data.get('evaluator_mi', updated_app.evaluator_mi),
                            cognitive_1=updated_app.cognitive_assessment_1,
                            cognitive_2=updated_app.cognitive_assessment_2,
                            affective=updated_app.affective_assessment,
                            psychomotor=updated_app.psychomotor_assessment,
                            direct_part_y_score=float(raw_py)
                        )
                    except (ValueError, TypeError):
                        pass
                else:
                    calculate_part_x_score(updated_app, save=True)

                return Response({
                    "status": "success",
                    "message": "Application and enquiry updated successfully by Admin.",
                    "data": AdmissionApplicationSerializer(updated_app).data
                }, status=status.HTTP_200_OK)
            else:
                return Response({
                    "status": "error",
                    "errors": serializer.errors
                }, status=status.HTTP_400_BAD_REQUEST)

        # 3. If application has not been submitted yet (pure enquiry edit)
        return Response({
            "status": "success",
            "message": f"Enquiry {submission.id} updated successfully by Admin.",
            "data": {
                "id": submission.id,
                "submission_id": submission.id,
                "child_name": submission.child_name,
                "surname": submission.surname,
                "parent_name": submission.parent_name,
                "whatsapp_number": submission.whatsapp_number,
                "standard": submission.standard,
                "medium": submission.medium,
                "vibhag": submission.vibhag,
                "payment_status": submission.payment_status,
                "whatsapp_link_sent": submission.whatsapp_link_sent
            }
        }, status=status.HTTP_200_OK)

    def delete(self, request):
        denied = enforce_admin_token(request)
        if denied:
            return denied

        app_id = request.query_params.get("id")
        submission_id = request.query_params.get("submission_id")

        if not app_id and not submission_id:
            return Response({
                "status": "error",
                "message": "Either 'id' or 'submission_id' query parameter is required to delete."
            }, status=status.HTTP_400_BAD_REQUEST)

        if app_id:
            application = AdmissionApplication.objects.filter(id=app_id).first()
            if application:
                app_number = application.application_number
                application_id = application.id
                sub_id = application.submission_id
                application.delete()
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


# ==============================================================================
# Stages 1-8: Evaluation, Rating, Cutoff, Confirmation, Physical Print & Archival
# ==============================================================================

class AdminEvaluationAssessmentView(APIView):
    """
    Step 2 & 3: Digital Assessment Form for Academicians & Educators
    GET /api/admin/evaluation/<application_id>/: Retrieve single applicant breakdown & marks.
    GET /api/admin/evaluation/: Retrieve all applicants evaluation breakdown (with search, status & pagination).
    POST /api/admin/evaluation/<application_id>/: Save Part Y evaluation (Cognitive 5+5, Affective 10, Psychomotor 10).
    Header: Authorization: Bearer <ADMIN_PERMANENT_TOKEN>
    """
    def get(self, request, application_id=None):
        denied = enforce_admin_token(request)
        if denied:
            return denied

        from .evaluation_service import calculate_part_x_score

        # Case 1: All evaluations listing when application_id is omitted
        if not application_id:
            search_query = (request.query_params.get("search") or request.query_params.get("q") or "").strip()
            standard_filter = (request.query_params.get("standard") or "").strip()
            medium_filter = (request.query_params.get("medium") or "").strip()
            status_filter = (request.query_params.get("status") or "").strip().upper()

            queryset = AdmissionApplication.objects.select_related('submission', 'evaluator').exclude(
                admission_status__in=['CANCELLED', 'REJECTED']
            ).order_by('-final_aggregate_score', 'merit_rank', 'id')

            if standard_filter and not standard_filter.lower().startswith("all"):
                queryset = queryset.filter(standard__iexact=standard_filter)

            if medium_filter and not medium_filter.lower().startswith("all"):
                queryset = queryset.filter(medium__iexact=medium_filter)

            if status_filter and status_filter != "ALL":
                queryset = queryset.filter(admission_status__iexact=status_filter)

            if search_query:
                clean_digits = re.sub(r"\D", "", search_query)
                search_cond = (
                    Q(child_first_name__icontains=search_query) |
                    Q(child_surname__icontains=search_query) |
                    Q(father_name__icontains=search_query) |
                    Q(mother_name__icontains=search_query) |
                    Q(application_number__icontains=search_query) |
                    Q(father_whatsapp__icontains=search_query) |
                    Q(mother_whatsapp__icontains=search_query)
                )
                if clean_digits:
                    search_cond |= Q(father_whatsapp__icontains=clean_digits) | Q(submission__whatsapp_number__icontains=clean_digits)
                if search_query.isdigit():
                    search_cond |= Q(submission_id=int(search_query)) | Q(id=int(search_query))
                queryset = queryset.filter(search_cond)

            total_records = queryset.count()
            page = int(request.query_params.get("page", 1))
            page_size = int(request.query_params.get("page_size", request.query_params.get("limit", 50)))
            if page < 1:
                page = 1
            if page_size < 1:
                page_size = 50

            total_pages = (total_records + page_size - 1) // page_size if total_records > 0 else 1
            offset = (page - 1) * page_size
            page_items = list(queryset[offset:offset + page_size])

            evaluations_list = []
            for app in page_items:
                reward_m = round((app.alak_malak_score or 0.0) + (app.sibling_score or 0.0), 2)
                sub_id_val = app.submission.id if app.submission else app.submission_id
                evaluations_list.append({
                    "application_id": app.id,
                    "submission_id": sub_id_val,
                    "application_number": app.application_number,
                    "student_name": f"{app.child_first_name or ''} {app.child_surname or ''}".strip(),
                    "parent_name": app.father_name or app.mother_name or "",
                    "contact_number": app.father_whatsapp or (app.submission.whatsapp_number if app.submission else "") or app.mother_whatsapp or "",
                    "standard": app.standard,
                    "medium": app.medium,
                    "vibhag": app.vibhag,
                    "interaction_date": str(app.interaction_date) if app.interaction_date else None,
                    "interaction_slot_no": app.interaction_slot_no,
                    "interaction_time": app.interaction_time,
                    "evaluator": app.evaluator.full_name or app.evaluator.username if app.evaluator else None,
                    "evaluator_m": app.evaluator_m or "",
                    "evaluator_mi": app.evaluator_mi or "",
                    "part_x": {
                        "max_marks": 50,
                        "quiz_score": app.total_part_x_score or 0.0
                    },
                    "part_y": {
                        "max_marks": 30,
                        "cognitive_assessment_1": app.cognitive_assessment_1 or 0.0,
                        "cognitive_assessment_2": app.cognitive_assessment_2 or 0.0,
                        "affective_assessment": app.affective_assessment or 0.0,
                        "psychomotor_assessment": app.psychomotor_assessment or 0.0,
                        "total_part_y_score": app.total_part_y_score or 0.0
                    },
                    "reward_marks": {
                        "max_marks": 13,
                        "alak_malak_score": app.alak_malak_score or 0.0,
                        "sibling_score": app.sibling_score or 0.0,
                        "total_reward_marks": reward_m
                    },
                    "final_aggregate_score": app.final_aggregate_score or 0.0,
                    "max_total_score": 93,
                    "merit_rank": app.merit_rank,
                    "admission_status": app.admission_status,
                    "is_part_y_completed": bool(app.total_part_y_score and app.total_part_y_score > 0)
                })

            return Response({
                "status": "success",
                "pagination": {
                    "page": page,
                    "page_size": page_size,
                    "total_records": total_records,
                    "total_pages": total_pages,
                    "has_next": page < total_pages,
                    "has_previous": page > 1
                },
                "total_records": total_records,
                "evaluations": evaluations_list
            }, status=status.HTTP_200_OK)

        # Case 2: Single application details by ID
        application = AdmissionApplication.objects.select_related('submission', 'evaluator').filter(id=application_id).first()
        if not application:
            return Response({
                "status": "error",
                "message": f"Admission application {application_id} not found."
            }, status=status.HTTP_404_NOT_FOUND)

        # Pre-calculate or ensure Part X score is up to date
        calculate_part_x_score(application, save=True)

        # Extract written responses and any AI evaluations from submission
        written_answers = {}
        ai_evaluations = {}
        if application.submission and application.submission.answers:
            answers_dict = application.submission.answers
            for q_id in [11, 12, 13, 14]:
                ans_text = answers_dict.get(str(q_id)) or answers_dict.get(q_id)
                if ans_text:
                    written_answers[str(q_id)] = ans_text
            ai_evaluations = answers_dict.get("ai_evaluations", {})

        reward_marks = round((application.alak_malak_score or 0.0) + (application.sibling_score or 0.0), 2)

        return Response({
            "status": "success",
            "application_id": application.id,
            "submission_id": application.submission.id if application.submission else application.submission_id,
            "application_number": application.application_number,
            "student_info": {
                "name": f"{application.child_first_name} {application.child_surname}".strip(),
                "standard": application.standard,
                "medium": application.medium,
                "vibhag": application.vibhag,
                "interaction_date": str(application.interaction_date) if application.interaction_date else None,
                "interaction_time": application.interaction_time,
                "interaction_slot_no": application.interaction_slot_no,
                "parent_name": application.father_name or application.mother_name,
                "parent_whatsapp": application.father_whatsapp or application.mother_whatsapp,
            },
            "parent_written_responses": written_answers,
            "ai_written_evaluations": ai_evaluations,
            "part_x": {
                "max_marks": 50,
                "description": "Parental Quiz Total Score (out of 50)",
                "quiz_score": application.total_part_x_score,
                "total_part_x_score": application.total_part_x_score
            },
            "part_y": {
                "max_marks": 30,
                "description": "Admin / Educator Evaluation Marks (out of 30)",
                "evaluator": application.evaluator.full_name or application.evaluator.username if application.evaluator else None,
                "m_tag": application.evaluator_m or "",
                "mi_tag": application.evaluator_mi or "",
                "cognitive_assessment_1": application.cognitive_assessment_1 or 0.0,
                "cognitive_assessment_2": application.cognitive_assessment_2 or 0.0,
                "affective_assessment": application.affective_assessment or 0.0,
                "psychomotor_assessment": application.psychomotor_assessment or 0.0,
                "part_y_score": application.total_part_y_score or 0.0,
                "total_part_y_score": application.total_part_y_score or 0.0
            },
            "reward_marks": {
                "max_marks": 13,
                "alak_malak_months": application.alak_malak_months,
                "alak_malak_certificate_uploaded": bool(application.alak_malak_certificate),
                "alak_malak_score": application.alak_malak_score or 0.0,
                "sibling_in_shreyas": application.has_sibling_in_shreyas,
                "sibling_opted_vk": application.sibling_opted_vk,
                "sibling_receipt_uploaded": bool(application.sibling_vikas_kendra_1_receipt or application.sibling_vikas_kendra_2_receipt),
                "sibling_score": application.sibling_score or 0.0,
                "total_reward_marks": reward_marks
            },
            "final_aggregate_score": application.final_aggregate_score or 0.0,
            "max_total_score": 93,
            "merit_rank": application.merit_rank,
            "admission_status": application.admission_status
        }, status=status.HTTP_200_OK)

    def post(self, request, application_id):
        denied = enforce_admin_token(request)
        if denied:
            return denied

        _, logged_in_admin, _ = get_authenticated_admin_user(request)

        application = AdmissionApplication.objects.filter(id=application_id).first()
        if not application:
            return Response({
                "status": "error",
                "message": f"Admission application {application_id} not found."
            }, status=status.HTTP_404_NOT_FOUND)

        from .serializers import EvaluationAssessmentSerializer
        from .evaluation_service import save_part_y_evaluation

        serializer = EvaluationAssessmentSerializer(data=request.data)
        if not serializer.is_valid():
            return Response({
                "status": "error",
                "errors": serializer.errors
            }, status=status.HTTP_400_BAD_REQUEST)

        vd = serializer.validated_data
        try:
            final_score = save_part_y_evaluation(
                application=application,
                evaluator_user=logged_in_admin,
                m_tag=vd.get('m_tag', ''),
                mi_tag=vd.get('mi_tag', ''),
                cognitive_1=vd.get('cognitive_assessment_1', 0.0),
                cognitive_2=vd.get('cognitive_assessment_2', 0.0),
                affective=vd.get('affective_assessment', 0.0),
                psychomotor=vd.get('psychomotor_assessment', 0.0),
                direct_part_y_score=vd.get('direct_part_y_score')
            )
        except ValueError as val_err:
            return Response({
                "status": "error",
                "message": str(val_err)
            }, status=status.HTTP_400_BAD_REQUEST)

        reward_marks = round((application.alak_malak_score or 0.0) + (application.sibling_score or 0.0), 2)

        return Response({
            "status": "success",
            "message": f"Evaluation saved and merit rank updated for {application.application_number}.",
            "application_id": application.id,
            "application_number": application.application_number,
            "part_x_score": application.total_part_x_score,
            "part_y_score": application.total_part_y_score,
            "reward_marks": reward_marks,
            "alak_malak_score": application.alak_malak_score,
            "sibling_score": application.sibling_score,
            "final_aggregate_score": final_score,
            "max_total_score": 93,
            "merit_rank": application.merit_rank,
            "admission_status": application.admission_status
        }, status=status.HTTP_200_OK)


class AdminMeritBoardView(APIView):
    """
    Step 5 & 6: Student Rating & Cutoff Selection Board
    GET /api/admin/merit-board/?standard=1&medium=Gujarati&cutoff=80
    Recalculates or retrieves merit ranking sorted by aggregate marks descending.
    Assigns top 80 seats as SELECTED, rest as WAITLISTED.
    Strictly restricted: Only users with role 'ADMIN' can access.
    """
    def get(self, request):
        denied = enforce_admin_token(request)
        if denied:
            return denied

        _, logged_in_admin, role = get_authenticated_admin_user(request)
        if role != 'ADMIN':
            return Response({
                "status": "error",
                "message": "Access restricted. Merit Board & Cutoff ratings are only visible to Admin users."
            }, status=status.HTTP_403_FORBIDDEN)

        from .evaluation_service import recompute_merit_rank_and_cutoff
        from .serializers import MeritRankBoardSerializer

        standard = (request.query_params.get("standard") or "").strip()
        medium = (request.query_params.get("medium") or "").strip()
        
        # Cutoff can be dynamically passed from the UI cutoff input box (e.g. ?cutoff=80)
        cutoff_param = request.query_params.get("cutoff")
        cutoff_seats = int(cutoff_param) if (cutoff_param and str(cutoff_param).strip().isdigit()) else 80
        recompute = request.query_params.get("recompute", "true").lower() in ["true", "1", "yes"]

        # Additional UI Filter Parameters (Status & Global Search)
        status_filter = (request.query_params.get("status") or "").strip().upper()
        search_query = (request.query_params.get("search") or request.query_params.get("q") or "").strip()

        # If specific standard and medium provided, recompute for that slice
        if standard and medium and not standard.lower().startswith("all") and not medium.lower().startswith("all"):
            if recompute:
                recompute_summary = recompute_merit_rank_and_cutoff(standard=standard, medium=medium, cutoff_seats=cutoff_seats)
            ranked_apps = AdmissionApplication.objects.select_related('submission').filter(
                standard__iexact=standard,
                medium__iexact=medium
            ).exclude(admission_status__in=['CANCELLED', 'REJECTED']).order_by('-final_aggregate_score', 'merit_rank', 'created_at')
        else:
            # If "All Standards" or "All Mediums" is selected, rank across all standard x medium pairs
            all_pairs = AdmissionApplication.objects.values_list('standard', 'medium').distinct()
            if recompute:
                for std_val, med_val in all_pairs:
                    if std_val and med_val:
                        recompute_merit_rank_and_cutoff(standard=std_val, medium=med_val, cutoff_seats=cutoff_seats)
            
            ranked_apps = AdmissionApplication.objects.select_related('submission').exclude(
                admission_status__in=['CANCELLED', 'REJECTED']
            ).order_by('-final_aggregate_score', 'merit_rank', 'created_at')

        # Compute accurate 4-Card Summary metrics BEFORE search/status filtering
        summary_counts = ranked_apps.aggregate(
            total_ranked=Count('id'),
            selected_seats=Count(Case(When(admission_status='SELECTED', then=1), output_field=IntegerField())),
            waitlisted=Count(Case(When(admission_status='WAITLISTED', then=1), output_field=IntegerField()))
        )

        summary = {
            "standard": standard or "All Standards",
            "medium": medium or "All Mediums",
            "total_ranked": summary_counts['total_ranked'] or 0,
            "selected_seats": summary_counts['selected_seats'] or 0,
            "waitlisted": summary_counts['waitlisted'] or 0,
            "cutoff_seats": cutoff_seats,
            "cutoff_quota": cutoff_seats
        }

        # Apply Status Filter if provided (e.g. SELECTED, WAITLISTED)
        if status_filter and status_filter != "ALL":
            ranked_apps = ranked_apps.filter(admission_status__iexact=status_filter)

        # Apply Global Search (matches Student Name, Parent Name, Submission ID, Application No, Phone)
        if search_query:
            clean_digits = re.sub(r"\D", "", search_query)
            search_cond = (
                Q(child_first_name__icontains=search_query) |
                Q(child_surname__icontains=search_query) |
                Q(father_name__icontains=search_query) |
                Q(mother_name__icontains=search_query) |
                Q(application_number__icontains=search_query) |
                Q(father_whatsapp__icontains=search_query) |
                Q(mother_whatsapp__icontains=search_query)
            )
            if clean_digits:
                search_cond |= Q(father_whatsapp__icontains=clean_digits) | Q(submission__whatsapp_number__icontains=clean_digits)
            if search_query.isdigit():
                search_cond |= Q(submission_id=int(search_query)) | Q(id=int(search_query))
            ranked_apps = ranked_apps.filter(search_cond)

        # Pagination support (default page 1, 50 applicants per page)
        total_applicants = ranked_apps.count()
        page = int(request.query_params.get("page", 1))
        page_size = int(request.query_params.get("page_size", request.query_params.get("limit", 50)))
        if page < 1:
            page = 1
        if page_size < 1:
            page_size = 50

        total_pages = (total_applicants + page_size - 1) // page_size if total_applicants > 0 else 1
        offset = (page - 1) * page_size
        paged_ranked_apps = list(ranked_apps[offset:offset + page_size])
        if not (standard and medium and not standard.lower().startswith("all") and not medium.lower().startswith("all")):
            for idx, app_obj in enumerate(paged_ranked_apps, start=offset + 1):
                app_obj.merit_rank = idx

        serializer = MeritRankBoardSerializer(paged_ranked_apps, many=True)

        return Response({
            "status": "success",
            "summary": summary,
            "cutoff_seats": cutoff_seats,
            "pagination": {
                "page": page,
                "page_size": page_size,
                "total_records": total_applicants,
                "total_pages": total_pages,
                "has_next": page < total_pages,
                "has_previous": page > 1
            },
            "total_applicants": total_applicants,
            "returned_applicants": len(serializer.data),
            "merit_list": serializer.data
        }, status=status.HTTP_200_OK)


class AdminMeritCancellationView(APIView):
    """
    Cancellation handling with Automatic Waitlist Promotion:
    When a selected student cancels, their status becomes CANCELLED, and the next
    waitlisted applicant automatically becomes SELECTED (filling the 80th seat).
    POST /api/admin/merit-board/cancel/<application_id>/
    """
    def post(self, request, application_id):
        denied = enforce_admin_token(request)
        if denied:
            return denied

        _, logged_in_admin, role = get_authenticated_admin_user(request)
        if role != 'ADMIN':
            return Response({
                "status": "error",
                "message": "Access restricted. Only Admin users can cancel admissions and promote candidates."
            }, status=status.HTTP_403_FORBIDDEN)

        application = AdmissionApplication.objects.filter(id=application_id).first()
        if not application:
            return Response({
                "status": "error",
                "message": f"Admission application {application_id} not found."
            }, status=status.HTTP_404_NOT_FOUND)

        old_status = application.admission_status
        standard = application.standard
        medium = application.medium

        application.admission_status = 'CANCELLED'
        application.merit_rank = None
        application.save(update_fields=['admission_status', 'merit_rank'])

        from .evaluation_service import recompute_merit_rank_and_cutoff
        summary = recompute_merit_rank_and_cutoff(standard=standard, medium=medium, cutoff_seats=80)

        # Find candidate newly promoted to 80th seat (if any)
        promoted_app = AdmissionApplication.objects.filter(
            standard__iexact=standard,
            medium__iexact=medium,
            merit_rank=80,
            admission_status='SELECTED'
        ).first()

        promoted_data = None
        if promoted_app and promoted_app.id != application.id:
            promoted_data = {
                "application_id": promoted_app.id,
                "application_number": promoted_app.application_number,
                "student_name": f"{promoted_app.child_first_name} {promoted_app.child_surname}".strip(),
                "rank": promoted_app.merit_rank,
                "score": promoted_app.final_aggregate_score
            }

        return Response({
            "status": "success",
            "message": f"Application {application.application_number} cancelled. Waitlist updated automatically.",
            "cancelled_application": application.application_number,
            "old_status": old_status,
            "promoted_waitlist_candidate": promoted_data,
            "summary": summary
        }, status=status.HTTP_200_OK)


class ParentConfirmInteractionSlotView(APIView):
    """
    Allows parents to confirm their scheduled interaction slot via WhatsApp link / Portal.
    POST /api/admission-form/confirm-slot/
    Body: {"application_number": "SF-24E309F3"} or {"application_id": 1}
    """
    def post(self, request):
        app_no = request.data.get("application_number")
        app_id = request.data.get("application_id")

        if not app_no and not app_id:
            return Response({
                "status": "error",
                "message": "application_number or application_id is required."
            }, status=status.HTTP_400_BAD_REQUEST)

        query = AdmissionApplication.objects.filter(application_number=app_no) if app_no else AdmissionApplication.objects.filter(id=app_id)
        application = query.first()

        if not application:
            return Response({
                "status": "error",
                "message": "Admission application not found."
            }, status=status.HTTP_404_NOT_FOUND)

        application.parent_confirmed_slot = True
        application.save(update_fields=['parent_confirmed_slot'])

        return Response({
            "status": "success",
            "message": "Interaction slot successfully confirmed by parent.",
            "application_number": application.application_number,
            "interaction_date": str(application.interaction_date),
            "interaction_time": application.interaction_time,
            "interaction_slot_no": application.interaction_slot_no,
            "parent_confirmed_slot": True
        }, status=status.HTTP_200_OK)


def _sign_azure_blob_url_if_needed(file_url: Optional[str]) -> Optional[str]:
    """
    If file_url points to our Azure Blob Storage container and does not already have a SAS signature,
    appends a temporary read-only SAS token so images/documents render and print seamlessly inside the A4 PDF/HTML.
    """
    if not file_url or not isinstance(file_url, str):
        return None
    if "sig=" in file_url or file_url.startswith("data:"):
        return file_url

    try:
        from urllib.parse import urlparse, unquote
        parsed = urlparse(file_url)
        acct_name = getattr(settings, 'AZURE_ACCOUNT_NAME', None)
        acct_key = getattr(settings, 'AZURE_ACCOUNT_KEY', None)
        container = getattr(settings, 'AZURE_CONTAINER', 'media')

        if acct_name and acct_key and parsed.netloc and acct_name.lower() in parsed.netloc.lower():
            from datetime import datetime, timedelta, timezone as dt_tz
            from azure.storage.blob import generate_blob_sas, BlobSasPermissions

            clean_path = unquote(parsed.path.lstrip('/'))
            container_prefix = f"{container}/"
            if clean_path.startswith(container_prefix):
                blob_name = clean_path[len(container_prefix):]
            else:
                blob_name = clean_path

            sas_token = generate_blob_sas(
                account_name=acct_name,
                container_name=container,
                blob_name=blob_name,
                account_key=acct_key,
                permission=BlobSasPermissions(read=True),
                expiry=datetime.now(dt_tz.utc) + timedelta(hours=4)
            )
            if sas_token:
                sep = "&" if "?" in file_url else "?"
                return f"{file_url}{sep}{sas_token}"
    except Exception as e:
        logger.warning(f"[SAS SIGNING] Could not sign blob URL {file_url}: {e}")

    return file_url


def _fetch_file_as_data_uri(file_url: Optional[str]) -> tuple[Optional[str], Optional[str], str]:
    """
    Downloads an uploaded document/image on the backend (using signed Azure SAS URL)
    and returns (data_uri, base64_raw, mime_type) so the generated HTML file is 100% self-contained!
    This guarantees that even when opened locally as a downloaded file (file:///C:/Users/.../Downloads/...html),
    all images and PDFs render immediately without CORS blocks or broken image icons.
    """
    if not file_url or not isinstance(file_url, str):
        return None, None, ""
    if file_url.startswith("data:"):
        return file_url, None, ""

    signed_url = _sign_azure_blob_url_if_needed(file_url)
    clean_lower = file_url.split("?")[0].lower()

    import mimetypes
    import base64
    guessed_mime, _ = mimetypes.guess_type(clean_lower)
    if clean_lower.endswith(".pdf"):
        guessed_mime = "application/pdf"
    elif clean_lower.endswith((".jpg", ".jpeg")):
        guessed_mime = "image/jpeg"
    elif clean_lower.endswith(".png"):
        guessed_mime = "image/png"
    elif clean_lower.endswith(".webp"):
        guessed_mime = "image/webp"
    else:
        guessed_mime = guessed_mime or "image/jpeg"

    try:
        r = requests.get(signed_url, timeout=15)
        if r.status_code == 200 and r.content:
            content_type = r.headers.get("Content-Type", "").split(";")[0].strip()
            if not content_type or content_type == "application/octet-stream":
                content_type = guessed_mime
            b64_str = base64.b64encode(r.content).decode("ascii")
            return f"data:{content_type};base64,{b64_str}", b64_str, content_type
    except Exception as e:
        logger.warning(f"[INLINE DATA URI FETCH] Failed to inline {file_url}: {e}")

    return signed_url, None, guessed_mime


def generate_professional_school_admission_form_html(forms_list):
    """
    Renders an authentic, high-end, professional School Admission Physical Form & Quiz Result Document (A4 printable / PDF).
    Features:
    - Official Shreyas Foundation Spiral Logo in header
    - Top Floating Action Bar (Print / Save as PDF - hidden on print)
    - Page 1: Complete Admission Application Details
    - Page 2: Complete Parental Understanding & Partnership Questionnaire (Quiz Result)
    - Page 3+: Each Uploaded Document on its own dedicated A4 page
    """
    import html as html_lib

    def esc(val, default="-"):
        if val is None or str(val).strip() == "":
            return default
        return html_lib.escape(str(val))

    shreyas_logo_url, _, _ = _fetch_file_as_data_uri("https://shreyasfoundation.blob.core.windows.net/media/shreyas_logo.png")
    logo_img_html = f'<img src="{shreyas_logo_url}" alt="Shreyas Foundation Logo" class="school-logo-img" />'

    first_item = forms_list[0] if forms_list else {}
    first_fd_meta = first_item.get("form_details", {})
    first_sp_meta = first_item.get("student_profile", {})
    app_no_for_file = first_fd_meta.get("application_number") or "Bulk"
    student_for_file = re.sub(r"[^A-Za-z0-9_-]+", "_", f"{first_sp_meta.get('first_name', '')}_{first_sp_meta.get('surname', '')}").strip("_") or "Student"
    pdf_filename = f"Admission_And_Quiz_{app_no_for_file}_{student_for_file}.pdf"

    cards_html = ""
    for idx, item in enumerate(forms_list):
        sh = item.get("school_header", {})
        fd = item.get("form_details", {})
        sp = item.get("student_profile", {})
        pp = item.get("parent_profile", {})
        gp = item.get("guardian_profile", {})
        sib_info = item.get("siblings_info", {})
        ps_info = item.get("previous_school", {})
        vk_info = item.get("vikas_kendra_activities", {})
        docs_info = item.get("documents_uploaded", {})
        inter_info = item.get("interaction_schedule", {})
        scores = item.get("assessment_scores", {})
        px = scores.get("part_x", {})
        py = scores.get("part_y", {})
        sigs = item.get("signatures_and_declarations", {})
        quiz = item.get("quiz_result", {})

        signed_photo_url, _, _ = _fetch_file_as_data_uri(sp.get("photo_url"))
        signed_father_sig, _, _ = _fetch_file_as_data_uri(sigs.get("father_signature_url"))
        signed_mother_sig, _, _ = _fetch_file_as_data_uri(sigs.get("mother_signature_url"))
        signed_guardian_sig, _, _ = _fetch_file_as_data_uri(sigs.get("guardian_signature_url"))

        photo_html = f'<img src="{signed_photo_url}" class="student-photo" alt="Student Photo" />' if signed_photo_url else '<div class="photo-placeholder">Affix Recent<br>Passport Size<br>Photograph Here</div>'
        father_sig_html = f'<img src="{signed_father_sig}" class="sig-img" />' if signed_father_sig else '<div class="sig-space"></div>'
        mother_sig_html = f'<img src="{signed_mother_sig}" class="sig-img" />' if signed_mother_sig else '<div class="sig-space"></div>'
        guardian_sig_html = f'<img src="{signed_guardian_sig}" class="sig-img" />' if signed_guardian_sig else '<div class="sig-space"></div>'

        break_class = "page-break" if idx > 0 else ""

        # Guardian rows if present
        guardian_html = ""
        if not pp.get("has_parents", True) or gp.get("guardian_name") or gp.get("guardians_list"):
            g_full = f"{gp.get('guardian_name') or ''} {gp.get('guardian_father_name') or ''} {gp.get('guardian_surname') or ''}".strip() or "-"
            guardian_html = f"""
            <div class="section-title">2B. GUARDIAN PARTICULARS</div>
            <table class="grid-table">
                <tr>
                    <td class="lbl" style="width: 20%;">Guardian Full Name:</td>
                    <td class="val font-semibold" style="width: 30%;">{esc(g_full)}</td>
                    <td class="lbl" style="width: 20%;">Mobile / WhatsApp:</td>
                    <td class="val" style="width: 30%;">{esc(gp.get('guardian_whatsapp') or gp.get('guardian_phone_residence'))}</td>
                </tr>
                <tr>
                    <td class="lbl">Education & Occupation:</td>
                    <td class="val">{esc(gp.get('guardian_education'))} / {esc(gp.get('guardian_occupation'))}</td>
                    <td class="lbl">Annual Income & Email:</td>
                    <td class="val">{esc(gp.get('guardian_annual_income'))} | {esc(gp.get('guardian_email'))}</td>
                </tr>
                <tr>
                    <td class="lbl">Residential Address:</td>
                    <td class="val">{esc(gp.get('guardian_address_residence'))}</td>
                    <td class="lbl">Office Address:</td>
                    <td class="val">{esc(gp.get('guardian_address_office'))}</td>
                </tr>
            </table>
            """

        # Languages Known helper
        langs_known = sp.get("languages_known") or {}
        if isinstance(langs_known, dict) and langs_known:
            lang_parts = []
            for who, l_val in langs_known.items():
                if isinstance(l_val, dict):
                    sub_p = [f"{k}: {v}" for k, v in l_val.items() if v]
                    if sub_p:
                        lang_parts.append(f"{who.title()} ({', '.join(sub_p)})")
                elif isinstance(l_val, list):
                    lang_parts.append(f"{who.title()}: {', '.join(str(x) for x in l_val)}")
                elif l_val:
                    lang_parts.append(f"{who.title()}: {l_val}")
            langs_known_str = " | ".join(lang_parts) if lang_parts else "-"
        else:
            langs_known_str = "-"

        # Siblings rows
        siblings_list = sib_info.get("siblings_list") or []
        if sib_info.get("has_sibling_in_shreyas"):
            if siblings_list:
                sib_rows = []
                for s_idx, s_obj in enumerate(siblings_list, start=1):
                    s_name = s_obj.get("name") or s_obj.get("sibling_name") or "-"
                    s_cls = s_obj.get("class") or s_obj.get("sibling_class") or "-"
                    vk1_r = s_obj.get("vk1_receipt") or s_obj.get("sibling_vikas_kendra_1_receipt") or ("Yes" if s_obj.get("vk1_opted") else "No")
                    vk2_r = s_obj.get("vk2_receipt") or s_obj.get("sibling_vikas_kendra_2_receipt") or ("Yes" if s_obj.get("vk2_opted") else "No")
                    vk3_r = s_obj.get("vk3_receipt") or s_obj.get("sibling_vikas_kendra_3_receipt") or ("Yes" if s_obj.get("vk3_opted") else "No")
                    sib_rows.append(f"#{s_idx}: <strong>{esc(s_name)}</strong> (Class: {esc(s_cls)}) — VK1: {esc(vk1_r)}, VK2: {esc(vk2_r)}, VK3: {esc(vk3_r)}")
                sibling_summary_html = "<br>".join(sib_rows)
            else:
                s_name = sib_info.get("sibling_name") or "-"
                s_cls = sib_info.get("sibling_class") or "-"
                vk1_r = sib_info.get("vk1_receipt") or sib_info.get("vk1_opted") or "-"
                vk2_r = sib_info.get("vk2_receipt") or sib_info.get("vk2_opted") or "-"
                sibling_summary_html = f"<strong>{esc(s_name)}</strong> (Class: {esc(s_cls)}) | VK1 Receipt: {esc(vk1_r)} | VK2 Receipt: {esc(vk2_r)}"
        else:
            sibling_summary_html = "No Sibling in Shreyas"

        # Vikas Kendra Activities
        vk1_acts = ", ".join(str(x) for x in (vk_info.get("vk1") or [])) or "-"
        vk2_acts = ", ".join(str(x) for x in (vk_info.get("vk2") or [])) or "-"
        vk3_acts = ", ".join(str(x) for x in (vk_info.get("vk3") or [])) or "-"

        # Uploaded Documents — Each document rendered directly on its own dedicated A4 page (Removed from Form Page 1)
        doc_items_list = [
            ("Child Passport Photograph", docs_info.get("photograph_child")),
            ("Birth Certificate", docs_info.get("birth_certificate")),
            ("Child's Aadhar Card", docs_info.get("child_aadhar_card")),
            ("Parent's / Guardian's Aadhar Card", docs_info.get("parent_aadhar_card")),
            ("Doctor's Health Certificate", docs_info.get("health_certificate")),
            ("Eye Checkup Certificate", docs_info.get("eye_checkup_certificate")),
            ("Caste Certificate", docs_info.get("caste_certificate")),
            ("Previous Year's Report Card", docs_info.get("previous_report_card")),
            ("Alak Malak Certificate", docs_info.get("alak_malak_certificate")),
        ]
        uploaded_annexures = []
        for doc_label, raw_doc_url in doc_items_list:
            if raw_doc_url:
                data_uri_or_url, b64_raw, mime_type = _fetch_file_as_data_uri(raw_doc_url)
                signed_fallback_url = _sign_azure_blob_url_if_needed(raw_doc_url)
                uploaded_annexures.append((doc_label, data_uri_or_url, b64_raw, mime_type, signed_fallback_url))

        total_pages_for_student = 2 + len(uploaded_annexures)

        # Build Dedicated Full-Page A4 Sheets for Each Uploaded Document
        annexures_page_html = ""
        for ann_idx, (ann_label, doc_src, b64_raw, mime_type, signed_fallback_url) in enumerate(uploaded_annexures, start=1):
            current_page_num = 2 + ann_idx
            clean_path_lower = (signed_fallback_url or "").split("?")[0].lower()
            is_pdf = mime_type == "application/pdf" or clean_path_lower.endswith(".pdf")
            is_word = clean_path_lower.endswith(".doc") or clean_path_lower.endswith(".docx")

            if is_pdf:
                # Pass inline base64 PDF data directly into data-pdf-b64 so PDF.js renders immediately even from file:// without CORS!
                b64_attr = b64_raw or ""
                preview_html = f"""
                <div class="pdf-canvas-list" data-pdf-b64="{b64_attr}" data-pdf-url="{esc(signed_fallback_url)}" data-doc-label="{esc(ann_label)}">
                    <div class="pdf-loading-msg">Rendering PDF Document ({esc(ann_label)})...</div>
                </div>
                """
            elif is_word:
                preview_html = f"""
                <div class="annexure-doc-box">
                    <div style="font-size: 36px; margin-bottom: 8px;">📄</div>
                    <div style="font-size: 14px;"><strong>{esc(ann_label)}</strong></div>
                </div>
                """
            else:
                preview_html = f"""
                <div class="full-doc-img-wrapper">
                    <img src="{doc_src}" alt="{esc(ann_label)}" class="full-doc-img" />
                </div>
                """

            annexures_page_html += f"""
        <!-- DEDICATED DOCUMENT PAGE {current_page_num}: {esc(ann_label)} -->
        <div class="admission-sheet page-break">
            <div class="header-container" style="padding-bottom: 6px; margin-bottom: 8px;">
                <div class="logo-area">
                    {logo_img_html}
                </div>
                <div class="header-text">
                    <h1 class="trust-title">SHREYAS FOUNDATION — ATTACHED DOCUMENT ({ann_idx} OF {len(uploaded_annexures)})</h1>
                    <h2 class="school-title" style="font-size: 15px; margin-top: 2px;">{esc(ann_label).upper()}</h2>
                    <p class="school-subtitle">Student: <strong>{esc(sp.get('first_name'))} {esc(sp.get('surname'))}</strong> | App No: <strong>{esc(fd.get('application_number'))}</strong> | Submission ID: <strong>#{esc(fd.get('submission_id'))}</strong> | Standard: <strong>{esc(fd.get('standard'))} ({esc(fd.get('medium'))})</strong></p>
                </div>
            </div>

            <div class="full-doc-container">
                {preview_html}
            </div>

            <div class="form-footer">
                <div>Document: {esc(ann_label)} | Application No: {esc(fd.get('application_number'))}</div>
                <div>Page {current_page_num} of {total_pages_for_student} (Attached Verification Document)</div>
            </div>
        </div>
            """

        # Build Page 2: Parental Quiz Result & Question-by-Question Breakdown
        mcq_rows_html = ""
        for q_item in quiz.get("mcq_questions", []):
            is_best = q_item.get("earned_marks", 0) == q_item.get("max_marks", 3)
            score_cls = "q-score-high" if is_best else ("q-score-mid" if q_item.get("earned_marks", 0) >= 2 else "q-score-low")
            mcq_rows_html += f"""
            <tr>
                <td class="text-center font-semibold">Q{q_item.get('id')}</td>
                <td>
                    <div class="q-focus">{esc(q_item.get('focus'))}</div>
                    <div class="q-text">{esc(q_item.get('question_en'))}</div>
                    <div class="q-text-gu">{esc(q_item.get('question_gu'))}</div>
                </td>
                <td>
                    <div><strong>Option {esc(q_item.get('selected_key'))}:</strong> {esc(q_item.get('selected_text_en'))}</div>
                    <div class="q-text-gu">{esc(q_item.get('selected_text_gu'))}</div>
                </td>
                <td class="text-center"><span class="{score_cls}">{q_item.get('earned_marks', 0)} / {q_item.get('max_marks', 3)}</span></td>
            </tr>
            """

        written_rows_html = ""
        for w_item in quiz.get("written_questions", []):
            written_rows_html += f"""
            <tr>
                <td class="text-center font-semibold">Q{w_item.get('id')}</td>
                <td style="width: 36%;">
                    <div class="q-focus">{esc(w_item.get('focus'))}</div>
                    <div class="q-text">{esc(w_item.get('question_en'))}</div>
                    <div class="q-text-gu">{esc(w_item.get('question_gu'))}</div>
                </td>
                <td style="width: 34%;">
                    <div class="written-ans-box">{esc(w_item.get('parent_answer'), 'No answer provided')}</div>
                </td>
                <td style="width: 22%;">
                    <div class="font-semibold" style="color: #0f3d2e; margin-bottom: 3px;">Score: {w_item.get('earned_marks', 0)} / {w_item.get('max_marks', 5)}</div>
                    <div class="ai-fb-box">{esc(w_item.get('ai_feedback'), 'Evaluated')}</div>
                </td>
            </tr>
            """

        qa_status_val = quiz.get("qa_status", "PENDING")
        qa_pill_cls = "status-pill" if qa_status_val == "PASSED" else "rank-badge"

        cards_html += f"""
        <!-- PAGE 1: OFFICIAL ADMISSION APPLICATION & ASSESSMENT FORM -->
        <div class="admission-sheet {break_class}">
            <!-- Header Section -->
            <div class="header-container">
                <div class="logo-area">
                    {logo_img_html}
                </div>
                <div class="header-text">
                    <h1 class="trust-title">{esc(sh.get('trust_name', 'Shreyas Foundation')).upper()}</h1>
                    <h2 class="school-title">{esc(sh.get('school_name', 'Vikas Kendra')).upper()}</h2>
                    <p class="school-subtitle">Post Box No. 4070, Near Manekbaug Hall, Ambawadi, Ahmedabad – 380015</p>
                    <div class="badge-title">OFFICIAL ADMISSION APPLICATION &amp; ASSESSMENT RECORD</div>
                    <div class="academic-year">ACADEMIC SESSION: {esc(sh.get('academic_session', '2026 - 2027'))}</div>
                </div>
                <div class="photo-container">
                    {photo_html}
                </div>
            </div>

            <!-- Meta Strip -->
            <table class="form-meta-table">
                <tr>
                    <td style="width: 25%;"><strong>Application No:</strong> <span class="highlight">{esc(fd.get('application_number'))}</span></td>
                    <td style="width: 25%;"><strong>Submission ID:</strong> #{esc(fd.get('submission_id'))} ({esc(fd.get('date_of_submission'))})</td>
                    <td style="width: 25%;"><strong>Standard &amp; Medium:</strong> {esc(fd.get('standard'))} ({esc(fd.get('medium'))})</td>
                    <td style="width: 25%;"><strong>Vibhag:</strong> {esc(fd.get('vibhag'))}</td>
                </tr>
                <tr>
                    <td><strong>Merit Rank:</strong> <span class="rank-badge">#{esc(fd.get('merit_rank'))}</span></td>
                    <td><strong>Admission Status:</strong> <span class="status-pill">{esc(fd.get('admission_status'))}</span></td>
                    <td><strong>Quiz Result:</strong> <span class="{qa_pill_cls}">{esc(quiz.get('total_score', 0))}/50 ({esc(qa_status_val)} - {esc(quiz.get('tag'))})</span></td>
                    <td><strong>Interaction Slot:</strong> {esc(inter_info.get('date'))} ({esc(inter_info.get('time'))})</td>
                </tr>
            </table>

            <!-- Section 1: Student Information, Milestones & Health -->
            <div class="section-title">1. STUDENT PARTICULARS, MILESTONES &amp; HEALTH DETAILS</div>
            <table class="grid-table">
                <tr>
                    <td class="lbl" style="width: 18%;">Student's Full Name:</td>
                    <td class="val" style="width: 32%; font-weight: bold; font-size: 12px;">{esc(sp.get('first_name'))} {esc(sp.get('father_name'))} {esc(sp.get('surname'))}</td>
                    <td class="lbl" style="width: 18%;">Date of Birth &amp; Age:</td>
                    <td class="val" style="width: 32%;">{esc(sp.get('date_of_birth'))} (Age: {esc(sp.get('age_summary'))})</td>
                </tr>
                <tr>
                    <td class="lbl">Birth Place (City/Taluka/Dist):</td>
                    <td class="val">{esc(sp.get('birth_place_full'))}</td>
                    <td class="lbl">Height &amp; Weight:</td>
                    <td class="val">Height: {esc(sp.get('height_cm'))} cm | Weight: {esc(sp.get('weight_kg'))} kg</td>
                </tr>
                <tr>
                    <td class="lbl">Mother Tongue (M / F):</td>
                    <td class="val">Mother: {esc(sp.get('mother_tongue_mother'))} | Father: {esc(sp.get('mother_tongue_father'))}</td>
                    <td class="lbl">Developmental Milestones:</td>
                    <td class="val">Started Talking: {esc(sp.get('age_started_talking'))} | Walking: {esc(sp.get('age_started_walking'))}</td>
                </tr>
                <tr>
                    <td class="lbl">Languages Known:</td>
                    <td colspan="3" class="val">{esc(langs_known_str)}</td>
                </tr>
                <tr>
                    <td class="lbl">Past Illness / Medical History:</td>
                    <td class="val">{esc(sp.get('past_illness'))}</td>
                    <td class="lbl">Specific Care Needed:</td>
                    <td class="val">{esc(sp.get('specific_care_needed'))}</td>
                </tr>
            </table>

            <!-- Section 2: Family Information -->
            <div class="section-title">2. PARENTS PARTICULARS (FATHER &amp; MOTHER)</div>
            <table class="grid-table">
                <thead>
                    <tr class="sub-head">
                        <th style="width: 20%;">Particulars</th>
                        <th style="width: 40%;">Father Details</th>
                        <th style="width: 40%;">Mother Details</th>
                    </tr>
                </thead>
                <tbody>
                    <tr>
                        <td class="lbl">Full Name (Name / Father / Surname):</td>
                        <td class="val font-semibold">{esc(pp.get('father_name'))} {esc(pp.get('father_father_name'))} {esc(pp.get('father_surname'))}</td>
                        <td class="val font-semibold">{esc(pp.get('mother_name'))} {esc(pp.get('mother_father_or_husband_name'))} {esc(pp.get('mother_surname'))}</td>
                    </tr>
                    <tr>
                        <td class="lbl">Educational Qualification:</td>
                        <td class="val">{esc(pp.get('father_qualification'))}</td>
                        <td class="val">{esc(pp.get('mother_qualification'))}</td>
                    </tr>
                    <tr>
                        <td class="lbl">Occupation &amp; Annual Income:</td>
                        <td class="val">{esc(pp.get('father_occupation'))} (Income: {esc(pp.get('father_annual_income'))})</td>
                        <td class="val">{esc(pp.get('mother_occupation'))} (Income: {esc(pp.get('mother_annual_income'))})</td>
                    </tr>
                    <tr>
                        <td class="lbl">WhatsApp / Residence / Office Phone:</td>
                        <td class="val">WA: {esc(pp.get('father_mobile'))} | Res: {esc(pp.get('father_phone_residence'))} | Off: {esc(pp.get('father_phone_office'))}</td>
                        <td class="val">WA: {esc(pp.get('mother_mobile'))} | Res: {esc(pp.get('mother_phone_residence'))} | Off: {esc(pp.get('mother_phone_office'))}</td>
                    </tr>
                    <tr>
                        <td class="lbl">Email &amp; Nationality:</td>
                        <td class="val">{esc(pp.get('father_email'))} ({esc(pp.get('father_nationality', 'Indian'))})</td>
                        <td class="val">{esc(pp.get('mother_email'))} ({esc(pp.get('mother_nationality', 'Indian'))})</td>
                    </tr>
                    <tr>
                        <td class="lbl">Residential Address:</td>
                        <td class="val">{esc(pp.get('father_address_residence') or pp.get('residential_address'))}</td>
                        <td class="val">{esc(pp.get('mother_address_residence') or pp.get('residential_address'))}</td>
                    </tr>
                    <tr>
                        <td class="lbl">Office Address:</td>
                        <td class="val">{esc(pp.get('father_address_office'))}</td>
                        <td class="val">{esc(pp.get('mother_address_office'))}</td>
                    </tr>
                </tbody>
            </table>

            {guardian_html}

            <!-- Section 3: Siblings, Present School, Alak Malak & Vikas Kendra -->
            <div class="section-title">3. SIBLINGS IN SHREYAS, PREVIOUS SCHOOL &amp; VIKAS KENDRA ACTIVITIES</div>
            <table class="grid-table">
                <tr>
                    <td class="lbl" style="width: 20%;">Siblings in Shreyas:</td>
                    <td colspan="3" class="val">{sibling_summary_html}</td>
                </tr>
                <tr>
                    <td class="lbl">Present / Previous School:</td>
                    <td class="val" style="width: 30%;">{esc(ps_info.get('school_name'))} ({esc(ps_info.get('school_place'))})</td>
                    <td class="lbl" style="width: 20%;">School Std, Medium &amp; Year:</td>
                    <td class="val" style="width: 30%;">Std: {esc(ps_info.get('school_std'))} | Med: {esc(ps_info.get('school_medium'))} | Year: {esc(ps_info.get('school_year'))}</td>
                </tr>
                <tr>
                    <td class="lbl">Attended Alak Malak?:</td>
                    <td class="val">{"Yes" if px.get("has_attended_alak_malak") else "No"} (Duration: {esc(px.get("alak_malak_months", 0))} Months)</td>
                    <td class="lbl">Schooling Declarations:</td>
                    <td class="val">No Private Tuitions: <strong>{"Yes" if sigs.get("agrees_discourage_private_tuitions") else "No"}</strong> | Natural Medium: <strong>{"Yes" if sigs.get("open_to_natural_medium_schooling") else "No"}</strong></td>
                </tr>
                <tr>
                    <td class="lbl">Vikas Kendra 1 (Opt 5):</td>
                    <td colspan="3" class="val">{esc(vk1_acts)}</td>
                </tr>
                <tr>
                    <td class="lbl">Vikas Kendra 2 (Opt 3):</td>
                    <td class="val">{esc(vk2_acts)}</td>
                    <td class="lbl">Vikas Kendra 3 Activities:</td>
                    <td class="val">{esc(vk3_acts)}</td>
                </tr>
            </table>

            <!-- Section 4: Declaration & Signatures -->
            <div class="declaration-block">
                <strong>Declaration (Agreed to Rules: {"Yes" if sigs.get("agreed_to_rules") else "No"} | Date: {esc(sigs.get("terms_agreed_date"))}):</strong>
                I/We hereby declare that all information furnished in this admission application is true and complete to the best of my/our knowledge. I/We agree to abide by the philosophy, rules, and code of conduct of Shreyas Foundation Vikas Kendra.
            </div>

            <div class="signatures-container">
                <div class="sig-box">
                    {father_sig_html}
                    <div class="sig-line">Father's Signature</div>
                </div>
                <div class="sig-box">
                    {mother_sig_html}
                    <div class="sig-line">Mother's Signature</div>
                </div>
                <div class="sig-box">
                    {guardian_sig_html}
                    <div class="sig-line">Guardian's Signature</div>
                </div>
                <div class="sig-box">
                    <div class="sig-space"></div>
                    <div class="sig-line">Head of School / Director</div>
                </div>
            </div>

            <!-- Footer -->
            <div class="form-footer">
                <div>Document Generated On: {esc(fd.get('date_of_submission'))} | Application No: {esc(fd.get('application_number'))}</div>
                <div>Page 1 of {total_pages_for_student} (Admission Application Form)</div>
            </div>
        </div>

        <!-- PAGE 2: PARENTAL UNDERSTANDING & PARTNERSHIP QUESTIONNAIRE (QUIZ RESULT) -->
        <div class="admission-sheet page-break">
            <div class="header-container" style="padding-bottom: 6px; margin-bottom: 6px;">
                <div class="logo-area">
                    {logo_img_html}
                </div>
                <div class="header-text">
                    <h1 class="trust-title">SHREYAS FOUNDATION — PARENTAL QUESTIONNAIRE &amp; QUIZ RESULT</h1>
                    <p class="school-subtitle">Student: <strong>{esc(sp.get('first_name'))} {esc(sp.get('surname'))}</strong> | App No: <strong>{esc(fd.get('application_number'))}</strong> | Submission ID: <strong>#{esc(fd.get('submission_id'))}</strong> | Standard: <strong>{esc(fd.get('standard'))} ({esc(fd.get('medium'))})</strong></p>
                </div>
            </div>

            <!-- Quiz Score Summary Bar -->
            <table class="form-meta-table">
                <tr>
                    <td style="width: 20%;"><strong>Quiz Status:</strong> <span class="{qa_pill_cls}">{esc(qa_status_val)}</span></td>
                    <td style="width: 20%;"><strong>MCQ Score (Q1-Q10):</strong> <strong>{quiz.get('mcq_score', 0)} / 30</strong></td>
                    <td style="width: 20%;"><strong>Written Score (Q11-Q14):</strong> <strong>{quiz.get('written_score', 0)} / 20</strong></td>
                    <td style="width: 20%;"><strong>Total Quiz Score:</strong> <span class="highlight">{quiz.get('total_score', 0)} / 50</span></td>
                    <td style="width: 20%;"><strong>Performance Tag:</strong> <strong>{esc(quiz.get('tag'))}</strong></td>
                </tr>
            </table>

            <!-- Part A: 10 MCQs Table -->
            <div class="section-title">PART A: MULTIPLE CHOICE QUESTIONS (10 QUESTIONS × 3 MARKS = 30 MARKS)</div>
            <table class="grid-table">
                <thead>
                    <tr class="sub-head">
                        <th style="width: 6%;">Q#</th>
                        <th style="width: 46%;">Question &amp; Assessment Focus</th>
                        <th style="width: 38%;">Parent's Selected Answer</th>
                        <th style="width: 10%;">Marks</th>
                    </tr>
                </thead>
                <tbody>
                    {mcq_rows_html}
                </tbody>
            </table>

            <!-- Part B: 4 Written Questions Table -->
            <div class="section-title">PART B: WRITTEN REFLECTION QUESTIONS &amp; AI EVALUATION (4 QUESTIONS × 5 MARKS = 20 MARKS)</div>
            <table class="grid-table">
                <thead>
                    <tr class="sub-head">
                        <th style="width: 6%;">Q#</th>
                        <th style="width: 36%;">Question Prompt</th>
                        <th style="width: 36%;">Parent's Written Response</th>
                        <th style="width: 22%;">Score &amp; Evaluation Feedback</th>
                    </tr>
                </thead>
                <tbody>
                    {written_rows_html}
                </tbody>
            </table>

            <div class="form-footer">
                <div>Quiz Completed At: {esc(quiz.get('quiz_completed_at'))} | Parent WhatsApp: {esc(pp.get('father_mobile') or pp.get('mother_mobile'))}</div>
                <div>Page 2 of {total_pages_for_student} (Parental Quiz Result &amp; Evaluation Report)</div>
            </div>
        </div>
        {annexures_page_html}
        """

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>Official Admission &amp; Quiz Record - Shreyas Foundation</title>
    <style>
        @page {{
            size: A4 portrait;
            margin: 8mm;
        }}
        * {{
            box-sizing: border-box;
            -webkit-print-color-adjust: exact !important;
            print-color-adjust: exact !important;
        }}
        body {{
            font-family: 'Segoe UI', Calibri, Arial, sans-serif;
            margin: 0;
            padding: 15px 10px 20px 10px;
            background-color: #f1f5f9;
            color: #1e293b;
        }}
        .admission-sheet {{
            width: 210mm;
            min-height: 282mm;
            padding: 9mm 11mm;
            margin: 0 auto 15px auto;
            background: #ffffff;
            border: 2px solid #0f3d2e;
            box-shadow: 0 4px 12px rgba(0,0,0,0.1);
            position: relative;
        }}
        .page-break {{
            page-break-before: always;
        }}
        .header-container {{
            display: flex;
            align-items: center;
            border-bottom: 2px solid #0f3d2e;
            padding-bottom: 6px;
            margin-bottom: 6px;
        }}
        .logo-area {{
            width: 62px;
            text-align: center;
            display: flex;
            align-items: center;
            justify-content: center;
        }}
        .school-logo-img {{
            width: 54px;
            height: 54px;
            object-fit: contain;
        }}
        .top-bar-logo {{
            width: 26px;
            height: 26px;
            object-fit: contain;
            vertical-align: middle;
            margin-right: 6px;
            background: #ffffff;
            border-radius: 50%;
            padding: 2px;
        }}
        .header-text {{
            flex: 1;
            text-align: center;
            padding: 0 8px;
        }}
        .trust-title {{
            margin: 0;
            font-size: 15px;
            font-weight: 800;
            letter-spacing: 0.8px;
            color: #0f3d2e;
        }}
        .school-title {{
            margin: 1px 0 0 0;
            font-size: 18px;
            font-weight: 900;
            letter-spacing: 1.2px;
            color: #8b1e28;
        }}
        .school-subtitle {{
            margin: 2px 0;
            font-size: 8.5px;
            color: #475569;
        }}
        .badge-title {{
            display: inline-block;
            background-color: #0f3d2e;
            color: #ffffff;
            font-size: 10px;
            font-weight: bold;
            padding: 2px 10px;
            border-radius: 3px;
            margin-top: 2px;
            letter-spacing: 0.5px;
        }}
        .academic-year {{
            font-size: 9.5px;
            font-weight: bold;
            color: #1e293b;
            margin-top: 2px;
        }}
        .photo-container {{
            width: 78px;
            height: 92px;
            border: 1.5px dashed #0f3d2e;
            display: flex;
            align-items: center;
            justify-content: center;
            background: #fafaf9;
            overflow: hidden;
        }}
        .photo-placeholder {{
            font-size: 7.5px;
            color: #64748b;
            text-align: center;
            padding: 4px;
            line-height: 1.2;
        }}
        .student-photo {{
            width: 100%;
            height: 100%;
            object-fit: cover;
        }}
        .form-meta-table {{
            width: 100%;
            border-collapse: collapse;
            margin-bottom: 6px;
            font-size: 9.5px;
            background: #f8fafc;
            border: 1px solid #cbd5e1;
        }}
        .form-meta-table td {{
            padding: 3.5px 6px;
            border: 1px solid #cbd5e1;
        }}
        .highlight {{
            color: #8b1e28;
            font-weight: bold;
        }}
        .rank-badge {{
            background: #fef08a;
            color: #854d0e;
            padding: 1px 5px;
            font-weight: bold;
            border-radius: 3px;
        }}
        .status-pill {{
            background: #dcfce7;
            color: #166534;
            padding: 1px 5px;
            font-weight: bold;
            border-radius: 3px;
        }}
        .section-title {{
            background: #0f3d2e;
            color: #ffffff;
            font-size: 9.5px;
            font-weight: bold;
            padding: 2.5px 7px;
            letter-spacing: 0.4px;
            margin-top: 5px;
            border-left: 4px solid #8b1e28;
        }}
        .grid-table {{
            width: 100%;
            border-collapse: collapse;
            margin-bottom: 5px;
            font-size: 9px;
        }}
        .grid-table th, .grid-table td {{
            border: 1px solid #cbd5e1;
            padding: 3px 5px;
            vertical-align: top;
        }}
        .grid-table .lbl {{
            background-color: #f8fafc;
            color: #334155;
            font-weight: 600;
        }}
        .grid-table .val {{
            color: #0f172a;
        }}
        .grid-table .sub-head th {{
            background-color: #f1f5f9;
            color: #0f3d2e;
            font-size: 8.8px;
            text-align: center;
            font-weight: 700;
            padding: 3px;
        }}
        .text-center {{ text-align: center; }}
        .font-semibold {{ font-weight: 600; }}
        .text-xs {{ font-size: 8.5px; }}
        .doc-pill {{
            display: inline-block;
            padding: 1px 5px;
            margin: 1px 2px;
            border-radius: 3px;
            font-size: 8px;
            font-weight: 600;
            text-decoration: none;
        }}
        .doc-ok {{
            background: #dcfce7;
            color: #166534;
            border: 1px solid #86efac;
        }}
        .doc-missing {{
            background: #f1f5f9;
            color: #64748b;
            border: 1px solid #cbd5e1;
        }}
        .q-focus {{
            font-size: 8px;
            font-weight: 700;
            color: #8b1e28;
            text-transform: uppercase;
            margin-bottom: 1px;
        }}
        .q-text {{
            font-size: 8.8px;
            color: #0f172a;
            line-height: 1.25;
        }}
        .q-text-gu {{
            font-size: 8.3px;
            color: #475569;
            line-height: 1.25;
            margin-top: 1px;
        }}
        .q-score-high {{
            background: #dcfce7;
            color: #166534;
            font-weight: 700;
            padding: 1px 5px;
            border-radius: 3px;
        }}
        .q-score-mid {{
            background: #fef9c3;
            color: #854d0e;
            font-weight: 700;
            padding: 1px 5px;
            border-radius: 3px;
        }}
        .q-score-low {{
            background: #fee2e2;
            color: #991b1b;
            font-weight: 700;
            padding: 1px 5px;
            border-radius: 3px;
        }}
        .written-ans-box {{
            white-space: pre-wrap;
            font-size: 8.8px;
            color: #0f172a;
            line-height: 1.3;
        }}
        .ai-fb-box {{
            font-size: 8.2px;
            color: #334155;
            line-height: 1.25;
            background: #f8fafc;
            padding: 3px 4px;
            border-left: 2px solid #0f3d2e;
        }}
        .score-table .total-row td {{
            background-color: #f8fafc;
            border-top: 1.5px solid #0f3d2e;
            font-weight: bold;
            font-size: 9.5px;
        }}
        .total-lbl {{ color: #0f3d2e; font-weight: bold; }}
        .total-val {{ text-align: center; font-weight: bold; color: #8b1e28; font-size: 10px; }}
        .grand-total-row td {{
            background-color: #f0fdf4 !important;
            border-top: 1.5px solid #16a34a !important;
            border-bottom: 1.5px solid #16a34a !important;
        }}
        .grand-lbl {{
            font-weight: 900;
            color: #166534;
            font-size: 10px;
            text-align: right;
            padding-right: 10px;
        }}
        .grand-val {{
            font-weight: 900;
            color: #166534;
            font-size: 11px;
            text-align: center;
        }}
        .declaration-block {{
            border: 1px dashed #cbd5e1;
            padding: 4px 7px;
            font-size: 8px;
            color: #475569;
            line-height: 1.25;
            margin-top: 4px;
            margin-bottom: 8px;
            background: #fafaf9;
        }}
        .signatures-container {{
            display: flex;
            justify-content: space-between;
            margin-top: 6px;
            padding-top: 3px;
        }}
        .sig-box {{
            width: 23%;
            text-align: center;
        }}
        .sig-space {{
            height: 28px;
        }}
        .sig-img {{
            max-height: 28px;
            max-width: 100%;
            display: block;
            margin: 0 auto;
        }}
        .sig-line {{
            border-top: 1px solid #475569;
            padding-top: 2px;
            font-size: 8.5px;
            font-weight: 600;
            color: #334155;
        }}
        .form-footer {{
            display: flex;
            justify-content: space-between;
            font-size: 7.8px;
            color: #64748b;
            border-top: 1px solid #e2e8f0;
            padding-top: 3px;
            margin-top: 8px;
        }}
        .full-doc-container {{
            width: 100%;
            min-height: 225mm;
            border: 1px solid #cbd5e1;
            border-radius: 4px;
            background: #ffffff;
            display: flex;
            flex-direction: column;
            align-items: center;
            justify-content: center;
            padding: 6px;
            overflow: hidden !important;
        }}
        .full-doc-img-wrapper {{
            width: 100%;
            display: flex;
            align-items: center;
            justify-content: center;
            overflow: hidden !important;
        }}
        .full-doc-img {{
            max-width: 100%;
            max-height: 225mm;
            width: auto;
            height: auto;
            object-fit: contain;
            display: block;
            margin: 0 auto;
        }}
        .pdf-canvas-list {{
            width: 100%;
            display: flex;
            flex-direction: column;
            align-items: center;
            justify-content: center;
            gap: 8px;
            overflow: hidden !important;
        }}
        .pdf-page-img {{
            max-width: 100%;
            max-height: 225mm;
            width: auto;
            height: auto;
            object-fit: contain;
            display: block;
            margin: 0 auto;
        }}
        .pdf-loading-msg {{
            font-size: 11px;
            color: #475569;
            padding: 20px;
        }}
        .annexure-doc-box {{
            padding: 24px;
            text-align: center;
            color: #334155;
        }}

        @media print {{
            .no-print {{
                display: none !important;
            }}
            body {{
                background-color: #ffffff;
                padding: 0;
            }}
            .admission-sheet {{
                border: 2px solid #0f3d2e;
                box-shadow: none;
                margin: 0;
                width: 100%;
                min-height: auto;
            }}
        }}
    </style>
    <script src="https://cdnjs.cloudflare.com/ajax/libs/pdf.js/3.11.174/pdf.min.js"></script>
    <script src="https://cdnjs.cloudflare.com/ajax/libs/html2canvas/1.4.1/html2canvas.min.js"></script>
    <script src="https://cdnjs.cloudflare.com/ajax/libs/jspdf/2.5.1/jspdf.umd.min.js"></script>
</head>
<body data-pdf-filename="{pdf_filename}">
    <div id="pdf-download-overlay" class="no-print" style="position:fixed; top:0; left:0; right:0; bottom:0; background:#f8fafc; z-index:999999; display:flex; flex-direction:column; align-items:center; justify-content:center; font-family:'Segoe UI',sans-serif;">
        <div style="background:#ffffff; padding:36px 48px; border-radius:16px; box-shadow:0 10px 30px rgba(0,0,0,0.1); border:1px solid #e2e8f0; text-align:center; max-width:440px; width:90%;">
            <img src="{shreyas_logo_url}" alt="Shreyas Logo" style="width:72px; height:72px; object-fit:contain; margin-bottom:16px;" />
            <h3 style="margin:0 0 8px 0; color:#0f3d2e; font-size:18px; font-weight:700;">Shreyas Foundation</h3>
            <p id="overlay-msg" style="margin:0 0 20px 0; color:#64748b; font-size:14px; line-height:1.5;">Preparing your official Admission Document &amp; Quiz Report...</p>
            <div id="overlay-progress-bar" style="width:100%; height:6px; background:#e2e8f0; border-radius:3px; overflow:hidden; position:relative; margin-bottom:18px;">
                <div style="width:50%; height:100%; background:#0f3d2e; border-radius:3px; animation:shreyasPulse 1.2s infinite ease-in-out;"></div>
            </div>
            <div id="overlay-status-text" style="font-size:13px; font-weight:600; color:#0f3d2e; display:flex; align-items:center; justify-content:center; gap:8px;">
                <span>⏳ Downloading PDF...</span>
            </div>
            <div id="overlay-fallback" style="margin-top:16px; display:none;">
                <p style="font-size:12px; color:#64748b; margin-bottom:8px;">If download didn't start automatically:</p>
                <button onclick="window.downloadTruePdfFile()" style="background:#0f3d2e; color:#fff; border:none; padding:8px 18px; border-radius:6px; font-size:13px; font-weight:600; cursor:pointer;">📥 Click Here to Download PDF</button>
            </div>
        </div>
    </div>
    <style>
        @keyframes shreyasPulse {{
            0% {{ transform: translateX(-100%); }}
            100% {{ transform: translateX(200%); }}
        }}
    </style>
    <div id="pdf-generation-toast" class="no-print" style="position:fixed; bottom:20px; right:20px; background:#0f3d2e; color:#fff; padding:12px 20px; border-radius:8px; font-size:13px; font-weight:700; box-shadow:0 4px 14px rgba(0,0,0,0.25); z-index:10000; display:flex; align-items:center; gap:10px;">
        <span>⏳ Preparing &amp; downloading PDF file...</span>
    </div>
    {cards_html}
    <script>
        // 1. Render any attached PDF documents into static, non-scrollable A4 canvas images
        // 2. Automatically generate and download a real .pdf file (Admission_And_Quiz_SF-XXXX_Name.pdf)
        document.addEventListener("DOMContentLoaded", function () {{
            function renderPdfDocToContainer(pdfDoc, container) {{
                container.innerHTML = "";
                var pagePromises = [];
                for (var pageNum = 1; pageNum <= pdfDoc.numPages; pageNum++) {{
                    (function (pNum) {{
                        var p = pdfDoc.getPage(pNum).then(function (page) {{
                            var viewport = page.getViewport({{ scale: 2.0 }});
                            var canvas = document.createElement("canvas");
                            var ctx = canvas.getContext("2d");
                            canvas.width = viewport.width;
                            canvas.height = viewport.height;
                            return page.render({{ canvasContext: ctx, viewport: viewport }}).promise.then(function () {{
                                var img = document.createElement("img");
                                img.src = canvas.toDataURL("image/png");
                                img.className = "pdf-page-img";
                                container.appendChild(img);
                            }});
                        }});
                        pagePromises.push(p);
                    }})(pageNum);
                }}
                return Promise.all(pagePromises);
            }}

            var allPdfPromises = [];
            if (window.pdfjsLib) {{
                window.pdfjsLib.GlobalWorkerOptions.workerSrc = "https://cdnjs.cloudflare.com/ajax/libs/pdf.js/3.11.174/pdf.worker.min.js";
                var containers = document.querySelectorAll(".pdf-canvas-list");
                containers.forEach(function (container) {{
                    var pdfB64 = container.getAttribute("data-pdf-b64");
                    var pdfUrl = container.getAttribute("data-pdf-url");

                    if (pdfB64 && pdfB64.length > 10) {{
                        try {{
                            var raw = window.atob(pdfB64);
                            var uint8Array = new Uint8Array(raw.length);
                            for (var i = 0; i < raw.length; i++) {{
                                uint8Array[i] = raw.charCodeAt(i);
                            }}
                            var p = window.pdfjsLib.getDocument({{ data: uint8Array }}).promise.then(function (pdfDoc) {{
                                return renderPdfDocToContainer(pdfDoc, container);
                            }}).catch(function (err) {{
                                console.error("PDF.js base64 render error:", err);
                            }});
                            allPdfPromises.push(p);
                            return;
                        }} catch (e) {{
                            console.error("Base64 decode error:", e);
                        }}
                    }}

                    if (pdfUrl) {{
                        var p2 = window.pdfjsLib.getDocument(pdfUrl).promise.then(function (pdfDoc) {{
                            return renderPdfDocToContainer(pdfDoc, container);
                        }}).catch(function () {{
                            container.innerHTML = '<div class="pdf-loading-msg">Unable to preview PDF offline.</div>';
                        }});
                        allPdfPromises.push(p2);
                    }}
                }});
            }}

            window.downloadTruePdfFile = async function () {{
                var toast = document.getElementById("pdf-generation-toast");
                var btn = document.getElementById("btn-download-pdf");
                var overlay = document.getElementById("pdf-download-overlay");
                var overlayStatus = document.getElementById("overlay-status-text");
                var overlayMsg = document.getElementById("overlay-msg");
                var overlayFallback = document.getElementById("overlay-fallback");

                if (toast) {{
                    toast.style.display = "flex";
                    toast.innerHTML = "<span>⏳ Generating high-quality PDF document...</span>";
                }}
                if (overlayStatus) {{
                    overlayStatus.innerHTML = "<span>⏳ Rendering PDF pages, please wait...</span>";
                }}
                if (btn) btn.disabled = true;

                try {{
                    var sheets = document.querySelectorAll(".admission-sheet");
                    var jsPDF = window.jspdf.jsPDF;
                    var pdf = new jsPDF({{ orientation: "portrait", unit: "mm", format: "a4", compress: true }});

                    for (var i = 0; i < sheets.length; i++) {{
                        if (overlayStatus) {{
                            overlayStatus.innerHTML = "<span>📄 Processing page " + (i + 1) + " of " + sheets.length + "...</span>";
                        }}
                        var sheet = sheets[i];
                        var canvas = await window.html2canvas(sheet, {{
                            scale: 2,
                            useCORS: true,
                            allowTaint: true,
                            backgroundColor: "#ffffff",
                            logging: false
                        }});
                        var imgData = canvas.toDataURL("image/jpeg", 0.95);
                        var pdfWidth = 210;
                        var pdfHeight = (canvas.height * pdfWidth) / canvas.width;
                        if (pdfHeight > 297) pdfHeight = 297;

                        if (i > 0) {{
                            pdf.addPage();
                        }}
                        pdf.addImage(imgData, "JPEG", 0, 0, pdfWidth, pdfHeight, undefined, "FAST");
                    }}

                    var filename = document.body.getAttribute("data-pdf-filename") || "Admission_And_Quiz_Form.pdf";
                    pdf.save(filename);

                    if (toast) {{
                        toast.style.background = "#166534";
                        toast.innerHTML = "<span>✅ PDF Downloaded (" + filename + ")</span>";
                        setTimeout(function () {{ toast.style.display = "none"; }}, 4000);
                    }}

                    if (overlayStatus) {{
                        overlayStatus.innerHTML = "<span style='color:#166534;'>✅ PDF Download Completed!</span>";
                    }}
                    if (overlayMsg) {{
                        overlayMsg.innerText = "Your document has been downloaded successfully.";
                    }}
                    var pBar = document.getElementById("overlay-progress-bar");
                    if (pBar) pBar.style.display = "none";

                    // If opened via auto-download, notify parent window / close tab after 2.5s
                    setTimeout(function () {{
                        if (window.parent && window.parent !== window) {{
                            window.parent.postMessage({{ event: "PDF_DOWNLOAD_COMPLETE", filename: filename }}, "*");
                        }}
                        // Try closing tab if it was opened just for download
                        if (window.opener) {{
                            window.close();
                        }} else if (overlay) {{
                            // Otherwise gracefully fade overlay out so user can view/re-download if desired
                            overlay.style.transition = "opacity 0.6s ease";
                            overlay.style.opacity = "0";
                            setTimeout(function () {{ overlay.style.display = "none"; }}, 600);
                        }}
                    }}, 1800);

                }} catch (err) {{
                    console.error("PDF generation failed, falling back to print dialog:", err);
                    if (toast) toast.style.display = "none";
                    if (overlayStatus) {{
                        overlayStatus.innerHTML = "<span style='color:#dc2626;'>⚠️ Download delayed</span>";
                    }}
                    if (overlayFallback) overlayFallback.style.display = "block";
                    window.print();
                }} finally {{
                    if (btn) btn.disabled = false;
                }}
            }};

            // Check if preview mode is explicitly requested (?preview=1 or ?no_auto_pdf=1)
            var params = new URLSearchParams(window.location.search);
            var isExplicitPreview = params.get("preview") === "1" || params.get("no_auto_pdf") === "1";
            var overlayEl = document.getElementById("pdf-download-overlay");

            if (isExplicitPreview && overlayEl) {{
                overlayEl.style.display = "none";
                var t = document.getElementById("pdf-generation-toast");
                if (t) t.style.display = "none";
            }}

            // Wait for all embedded PDFs and images to finish rendering, then auto-download the .pdf file!
            Promise.all(allPdfPromises).then(function () {{
                setTimeout(function () {{
                    if (!isExplicitPreview) {{
                        window.downloadTruePdfFile();
                    }}
                }}, 600);
            }});
        }});
    </script>
</body>
</html>"""


from rest_framework.renderers import JSONRenderer, BrowsableAPIRenderer, StaticHTMLRenderer, BaseRenderer

class PassthroughHtmlRenderer(BaseRenderer):
    media_type = 'text/html'
    format = 'html'

    def render(self, data, accepted_media_type=None, renderer_context=None):
        if isinstance(data, str):
            return data.encode('utf-8')
        return str(data).encode('utf-8')


class AdminPrintPhysicalFormView(APIView):
    """
    Step 7: Render or download complete official physical form + quiz result document (HTML / Printable PDF).
    Supports:
    - GET /api/admin/applications/<application_id>/print/ (by application_id, application_number, or submission_id)
    - GET /api/admin/applications/<application_id>/download/ (forces direct document download)
    - GET /api/admin/applications/download/?application_id=... or ?submission_id=...
    - GET /api/admin/applications/print-bulk/?standard=1&medium=Gujarati&status=SELECTED
    Query Params:
    - ?download=1 or ?download=true : Triggers direct file download (Content-Disposition: attachment)
    - ?format=json : Returns structured JSON with both form details, quiz breakdown, and rendered HTML
    - ?token=<JWT> : Allows direct browser link download/print from Admin Panel
    """
    renderer_classes = [JSONRenderer, BrowsableAPIRenderer, PassthroughHtmlRenderer]

    def get(self, request, application_id=None):
        denied = enforce_admin_token(request)
        if denied:
            return denied

        is_bulk_print = "print-bulk" in request.path.lower()
        is_bulk_download = "download-bulk" in request.path.lower() or request.query_params.get("bulk") in ["1", "true"]
        submission_id_param = request.query_params.get("submission_id")
        app_id_param = request.query_params.get("application_id") or request.query_params.get("id")
        app_no_param = request.query_params.get("application_number")
        ids_param = request.query_params.get("ids") or request.query_params.get("application_ids")
        sub_ids_param = request.query_params.get("submission_ids")

        # 0. Bulk IDs Selection (e.g. ?ids=25,26,27 or ?submission_ids=195,196,197)
        if is_bulk_download and (ids_param or sub_ids_param):
            if ids_param:
                parsed_ids = [int(i.strip()) for i in str(ids_param).split(",") if i.strip().isdigit()]
                applications = list(AdmissionApplication.objects.select_related('evaluator', 'submission').filter(id__in=parsed_ids).order_by('merit_rank', 'id'))
            else:
                parsed_sub_ids = [int(i.strip()) for i in str(sub_ids_param).split(",") if i.strip().isdigit()]
                applications = list(AdmissionApplication.objects.select_related('evaluator', 'submission').filter(submission_id__in=parsed_sub_ids).order_by('merit_rank', 'id'))

            if not applications:
                return Response({
                    "status": "error",
                    "message": "No admission applications found for the specified IDs."
                }, status=status.HTTP_404_NOT_FOUND)

        # 1. If ?submission_id=<id> is explicitly provided, ONLY match that exact submission_id
        elif submission_id_param:
            if str(submission_id_param).isdigit():
                app_obj = AdmissionApplication.objects.select_related('evaluator', 'submission').filter(
                    submission_id=int(submission_id_param)
                ).first()
            else:
                app_obj = None

            if not app_obj:
                return Response({
                    "status": "error",
                    "message": f"Admission application for submission_id '{submission_id_param}' not found."
                }, status=status.HTTP_404_NOT_FOUND)
            applications = [app_obj]

        # 2. If ?application_id=<id> or ?id=<id> is explicitly provided, ONLY match that exact application id
        elif app_id_param and not application_id:
            if str(app_id_param).isdigit():
                app_obj = AdmissionApplication.objects.select_related('evaluator', 'submission').filter(
                    id=int(app_id_param)
                ).first()
            else:
                app_obj = AdmissionApplication.objects.select_related('evaluator', 'submission').filter(
                    application_number__iexact=str(app_id_param).strip()
                ).first()

            if not app_obj:
                return Response({
                    "status": "error",
                    "message": f"Admission application '{app_id_param}' not found."
                }, status=status.HTTP_404_NOT_FOUND)
            applications = [app_obj]

        # 3. If ?application_number=<app_no> is explicitly provided, ONLY match that exact application_number
        elif app_no_param and not application_id:
            app_obj = AdmissionApplication.objects.select_related('evaluator', 'submission').filter(
                application_number__iexact=str(app_no_param).strip()
            ).first()
            if not app_obj:
                return Response({
                    "status": "error",
                    "message": f"Admission application '{app_no_param}' not found."
                }, status=status.HTTP_404_NOT_FOUND)
            applications = [app_obj]

        # 4. If URL path <application_id> is provided (/api/admin/applications/<application_id>/print/ or /download/)
        #    Strictly return ONLY 1 record! (Prioritize exact submission_id or exact id depending on ?by= param, or exact id first then submission_id)
        elif application_id:
            lookup_by = (request.query_params.get("by") or "").lower().strip()
            app_obj = None
            if str(application_id).isdigit():
                num_id = int(application_id)
                if lookup_by == "submission_id":
                    app_obj = AdmissionApplication.objects.select_related('evaluator', 'submission').filter(
                        submission_id=num_id
                    ).first()
                elif lookup_by == "application_id":
                    app_obj = AdmissionApplication.objects.select_related('evaluator', 'submission').filter(
                        id=num_id
                    ).first()
                else:
                    # Check exact AdmissionApplication.id first; if not found, fallback to exact submission_id
                    app_obj = AdmissionApplication.objects.select_related('evaluator', 'submission').filter(
                        id=num_id
                    ).first()
                    if not app_obj:
                        app_obj = AdmissionApplication.objects.select_related('evaluator', 'submission').filter(
                            submission_id=num_id
                        ).first()
            else:
                app_obj = AdmissionApplication.objects.select_related('evaluator', 'submission').filter(
                    application_number__iexact=str(application_id).strip()
                ).first()

            if not app_obj:
                return Response({
                    "status": "error",
                    "message": f"Admission application '{application_id}' not found."
                }, status=status.HTTP_404_NOT_FOUND)
            applications = [app_obj]

        # 5. Bulk download or print filtered by standard, medium, status
        elif is_bulk_print or is_bulk_download:
            standard = request.query_params.get("standard")
            medium = request.query_params.get("medium")
            app_status = request.query_params.get("status", "SELECTED")

            applications = AdmissionApplication.objects.select_related('evaluator', 'submission').all()
            if standard and standard.lower() != "all":
                applications = applications.filter(standard__iexact=standard)
            if medium and medium.lower() != "all":
                applications = applications.filter(medium__iexact=medium)
            if app_status and app_status.lower() != "all":
                applications = applications.filter(admission_status__iexact=app_status)
            applications = list(applications.order_by('merit_rank', 'application_number'))

            if not applications:
                return Response({
                    "status": "error",
                    "message": "No admission applications found matching the selected filters."
                }, status=status.HTTP_404_NOT_FOUND)
        else:
            return Response({
                "status": "error",
                "message": "Please provide 'submission_id', 'application_id', or 'application_number' to download a single student's document."
            }, status=status.HTTP_400_BAD_REQUEST)

        from .evaluation_service import calculate_part_x_score

        # Index QUESTIONNAIRE_QUESTIONS by ID for fast lookup
        question_lookup = {q["id"]: q for q in QUESTIONNAIRE_QUESTIONS}

        forms_data = []
        for app in applications:
            # Ensure Part X score is calculated and up-to-date
            if not app.total_part_x_score:
                calculate_part_x_score(app, save=True)

            sub = app.submission
            sub_answers = (sub.answers if sub and isinstance(sub.answers, dict) else {}) or {}
            ai_evals = sub_answers.get("ai_evaluations", {}) if isinstance(sub_answers, dict) else {}

            # Score breakdown for Quiz (MCQs 1-10 and Written 11-14)
            mcq_questions_data = []
            mcq_total_earned = 0
            for q_id in range(1, 11):
                q_meta = question_lookup.get(q_id, {})
                sel_key = sub_answers.get(str(q_id)) or sub_answers.get(q_id) or "-"
                sel_key = str(sel_key).strip().upper()
                earned = PARENT_MCQ_SCORING_MATRIX.get(q_id, {}).get(sel_key, 0)
                mcq_total_earned += earned

                sel_opt_obj = next((o for o in q_meta.get("options", []) if o.get("key") == sel_key), None)
                mcq_questions_data.append({
                    "id": q_id,
                    "focus": q_meta.get("focus", ""),
                    "question_en": q_meta.get("question_en", ""),
                    "question_gu": q_meta.get("question_gu", ""),
                    "selected_key": sel_key,
                    "selected_text_en": sel_opt_obj.get("text_en", "Not Answered") if sel_opt_obj else "Not Answered",
                    "selected_text_gu": sel_opt_obj.get("text_gu", "") if sel_opt_obj else "",
                    "earned_marks": earned,
                    "max_marks": 3,
                    "best_option": ALIGNED_ANSWERS.get(q_id, "A")
                })

            written_questions_data = []
            written_total_earned = 0.0
            for q_id in [11, 12, 13, 14]:
                q_meta = question_lookup.get(q_id, {})
                ans_text = str(sub_answers.get(str(q_id)) or sub_answers.get(q_id) or "").strip()
                eval_obj = ai_evals.get(str(q_id)) or ai_evals.get(q_id) or {}
                w_score = float(eval_obj.get("score", 0.0)) if isinstance(eval_obj, dict) else 0.0
                w_fb = eval_obj.get("feedback", "") if isinstance(eval_obj, dict) else ""
                written_total_earned += w_score

                written_questions_data.append({
                    "id": q_id,
                    "focus": q_meta.get("focus", ""),
                    "question_en": q_meta.get("question_en", ""),
                    "question_gu": q_meta.get("question_gu", ""),
                    "parent_answer": ans_text,
                    "earned_marks": round(w_score, 1),
                    "max_marks": 5,
                    "ai_feedback": w_fb
                })

            total_quiz_score = sub.score if (sub and sub.score) else round(mcq_total_earned + written_total_earned, 1)
            if written_total_earned == 0.0 and total_quiz_score > mcq_total_earned:
                written_total_earned = round(total_quiz_score - mcq_total_earned, 1)

            age_parts = []
            if app.present_age_year is not None:
                age_parts.append(f"{app.present_age_year} Y")
            if app.present_age_month is not None:
                age_parts.append(f"{app.present_age_month} M")
            if app.present_age_day is not None:
                age_parts.append(f"{app.present_age_day} D")
            age_summary = " ".join(age_parts) if age_parts else "-"

            birth_place_parts = [p for p in [app.birth_place_city, app.birth_place_taluka, app.birth_place_district] if p]
            birth_place_full = ", ".join(birth_place_parts) if birth_place_parts else "-"

            forms_data.append({
                "school_header": {
                    "trust_name": "Shreyas Foundation",
                    "school_name": "Vikas Kendra",
                    "location": "Ahmedabad, Gujarat",
                    "academic_session": "2026 - 2027"
                },
                "form_details": {
                    "application_id": app.id,
                    "application_number": app.application_number,
                    "submission_id": sub.id if sub else app.submission_id,
                    "date_of_submission": (app.form_submitted_at or app.created_at).strftime("%d-%m-%Y") if (app.form_submitted_at or app.created_at) else "-",
                    "vibhag": app.vibhag,
                    "standard": app.standard,
                    "medium": app.medium,
                    "merit_rank": app.merit_rank,
                    "admission_status": app.admission_status
                },
                "student_profile": {
                    "first_name": app.child_first_name,
                    "father_name": app.child_father_name or app.father_name,
                    "surname": app.child_surname,
                    "date_of_birth": str(app.date_of_birth) if app.date_of_birth else "-",
                    "present_age_year": app.present_age_year,
                    "present_age_month": app.present_age_month,
                    "present_age_day": app.present_age_day,
                    "age_summary": age_summary,
                    "gender": getattr(app, 'child_gender', '-'),
                    "blood_group": getattr(app, 'child_blood_group', '-'),
                    "birth_place": f"{app.birth_place_city or ''} {app.birth_place_district or ''}".strip() or "-",
                    "birth_place_city": app.birth_place_city,
                    "birth_place_taluka": app.birth_place_taluka,
                    "birth_place_district": app.birth_place_district,
                    "birth_place_full": birth_place_full,
                    "age_started_talking": app.age_started_talking_year or "-",
                    "age_started_walking": app.age_started_walking_year or "-",
                    "mother_tongue": app.mother_tongue_mother or app.mother_tongue_father or "-",
                    "mother_tongue_mother": app.mother_tongue_mother or "-",
                    "mother_tongue_father": app.mother_tongue_father or "-",
                    "languages_known": app.languages_known or {},
                    "height_cm": app.child_height_cm or "-",
                    "weight_kg": app.child_weight_kg or "-",
                    "past_illness": app.past_illness or "-",
                    "specific_care_needed": app.specific_care_needed or "-",
                    "nationality": app.father_nationality or app.mother_nationality or "Indian",
                    "photo_url": app.photograph_child.url if app.photograph_child else None
                },
                "parent_profile": {
                    "has_parents": app.has_parents,
                    "father_name": app.father_name,
                    "father_father_name": app.father_father_name,
                    "father_surname": app.father_surname,
                    "father_qualification": app.father_education,
                    "father_occupation": app.father_occupation,
                    "father_annual_income": app.father_annual_income,
                    "father_mobile": app.father_whatsapp or (sub.whatsapp_number if sub else ""),
                    "father_phone_residence": app.father_phone_residence,
                    "father_phone_office": app.father_phone_office,
                    "father_email": app.father_email or (sub.email if sub else ""),
                    "father_nationality": app.father_nationality or "Indian",
                    "father_address_residence": app.father_address_residence,
                    "father_address_office": app.father_address_office,
                    "mother_name": app.mother_name,
                    "mother_father_or_husband_name": app.mother_father_or_husband_name,
                    "mother_surname": app.mother_surname,
                    "mother_qualification": app.mother_education,
                    "mother_occupation": app.mother_occupation,
                    "mother_annual_income": app.mother_annual_income,
                    "mother_mobile": app.mother_whatsapp,
                    "mother_phone_residence": app.mother_phone_residence,
                    "mother_phone_office": app.mother_phone_office,
                    "mother_email": app.mother_email,
                    "mother_nationality": app.mother_nationality or "Indian",
                    "mother_address_residence": app.mother_address_residence,
                    "mother_address_office": app.mother_address_office,
                    "residential_address": app.father_address_residence or app.mother_address_residence or "-",
                    "office_address": app.father_address_office or app.mother_address_office or "-"
                },
                "guardian_profile": {
                    "guardian_name": app.guardian_name,
                    "guardian_father_name": app.guardian_father_name,
                    "guardian_surname": app.guardian_surname,
                    "guardian_education": app.guardian_education,
                    "guardian_occupation": app.guardian_occupation,
                    "guardian_annual_income": app.guardian_annual_income,
                    "guardian_whatsapp": app.guardian_whatsapp,
                    "guardian_phone_residence": app.guardian_phone_residence,
                    "guardian_phone_office": app.guardian_phone_office,
                    "guardian_email": app.guardian_email,
                    "guardian_nationality": app.guardian_nationality,
                    "guardian_address_residence": app.guardian_address_residence,
                    "guardian_address_office": app.guardian_address_office,
                    "guardians_list": app.guardians or []
                },
                "siblings_info": {
                    "has_sibling_in_shreyas": app.has_sibling_in_shreyas,
                    "sibling_name": app.sibling_name,
                    "sibling_class": app.sibling_class,
                    "vk1_opted": app.sibling_vikas_kendra_1_opted,
                    "vk1_receipt": app.sibling_vikas_kendra_1_receipt,
                    "vk2_opted": app.sibling_vikas_kendra_2_opted,
                    "vk2_receipt": app.sibling_vikas_kendra_2_receipt,
                    "siblings_list": app.siblings or []
                },
                "previous_school": {
                    "school_name": app.current_school_name or "-",
                    "school_place": app.current_school_place or "-",
                    "school_year": app.current_school_year or "-",
                    "school_std": app.current_school_std or "-",
                    "school_medium": app.current_school_medium or "-"
                },
                "vikas_kendra_activities": {
                    "vk1": app.vikas_kendra_1_activities or [],
                    "vk2": app.vikas_kendra_2_activities or [],
                    "vk3": app.vikas_kendra_3_activities or []
                },
                "documents_uploaded": {
                    "photograph_child": app.photograph_child.url if app.photograph_child else None,
                    "birth_certificate": app.birth_certificate.url if app.birth_certificate else None,
                    "child_aadhar_card": app.child_aadhar_card.url if app.child_aadhar_card else None,
                    "parent_aadhar_card": app.parent_aadhar_card.url if app.parent_aadhar_card else None,
                    "health_certificate": app.health_certificate.url if app.health_certificate else None,
                    "eye_checkup_certificate": app.eye_checkup_certificate.url if app.eye_checkup_certificate else None,
                    "caste_certificate": app.caste_certificate.url if app.caste_certificate else None,
                    "previous_report_card": app.previous_report_card.url if app.previous_report_card else None,
                    "alak_malak_certificate": app.alak_malak_certificate.url if app.alak_malak_certificate else None
                },
                "interaction_schedule": {
                    "date": app.interaction_date.strftime("%d-%m-%Y") if app.interaction_date else "-",
                    "slot_no": app.interaction_slot_no,
                    "time": app.interaction_time or "-",
                    "venue": app.interaction_venue or "Shreyas Foundation – Main Office",
                    "parent_confirmed_slot": app.parent_confirmed_slot
                },
                "assessment_scores": {
                    "part_x": {
                        "has_attended_alak_malak": app.has_attended_alak_malak,
                        "alak_malak_months": app.alak_malak_months,
                        "alak_malak": app.alak_malak_score,
                        "parent_mcq": app.mcq_score_part_x,
                        "sibling_vk": app.sibling_score,
                        "total_part_x": app.total_part_x_score
                    },
                    "part_y": {
                        "evaluator": app.evaluator.full_name or app.evaluator.username if app.evaluator else "-",
                        "m_category": app.evaluator_m,
                        "mi_category": app.evaluator_mi,
                        "cognitive_1": app.cognitive_assessment_1,
                        "cognitive_2": app.cognitive_assessment_2,
                        "affective": app.affective_assessment,
                        "psychomotor": app.psychomotor_assessment,
                        "total_part_y": app.total_part_y_score
                    },
                    "final_score": app.final_aggregate_score
                },
                "signatures_and_declarations": {
                    "agrees_discourage_private_tuitions": app.agrees_discourage_private_tuitions,
                    "open_to_natural_medium_schooling": app.open_to_natural_medium_schooling,
                    "agreed_to_rules": app.agreed_to_rules,
                    "terms_agreed_date": str(app.terms_agreed_date) if app.terms_agreed_date else "-",
                    "father_signature_url": app.father_signature.url if app.father_signature else None,
                    "mother_signature_url": app.mother_signature.url if app.mother_signature else None,
                    "guardian_signature_url": app.guardian_signature.url if app.guardian_signature else None
                },
                "quiz_result": {
                    "submission_id": sub.id if sub else None,
                    "qa_status": sub.qa_status if sub else "PENDING",
                    "tag": sub.tag if sub else "-",
                    "is_eligible": sub.is_eligible if sub else False,
                    "mcq_score": mcq_total_earned,
                    "written_score": round(written_total_earned, 1),
                    "total_score": total_quiz_score,
                    "max_score": 50,
                    "quiz_completed_at": sub.quiz_completed_at.strftime("%d-%m-%Y %H:%M") if (sub and sub.quiz_completed_at) else "-",
                    "mcq_questions": mcq_questions_data,
                    "written_questions": written_questions_data
                }
            })

        # Generate the professional printable HTML
        from django.http import HttpResponse
        html_content = generate_professional_school_admission_form_html(forms_data)

        # First primary candidate details for backward compatibility with frontend JSON readers
        first_form = forms_data[0] if forms_data else {}
        first_sp = first_form.get("student_profile", {})
        first_pp = first_form.get("parent_profile", {})
        first_fd = first_form.get("form_details", {})
        first_sc = first_form.get("assessment_scores", {})
        px = first_sc.get("part_x", {})
        py = first_sc.get("part_y", {})

        student_name_str = f"{first_sp.get('first_name', '')} {first_sp.get('surname', '')}".strip()
        father_mother_str = f"{first_pp.get('father_name', '')} / {first_pp.get('mother_name', '')}".strip(" /")
        std_med_str = f"{first_fd.get('standard', '')} / {first_fd.get('medium', '')}".strip(" /")
        rewards_val = round(float(px.get('alak_malak', 0) or 0) + float(px.get('sibling_vk', 0) or 0), 2)
        assessment_summary_str = f"Part X (Quiz): {px.get('total_part_x', 0)}/50 | Part Y: {py.get('total_part_y', 0)}/30 | Rewards: {rewards_val}/13 | Final Aggregate: {first_sc.get('final_score', 0)}/93"

        # Check if caller specifically requested raw JSON
        print_format = (request.query_params.get("format") or request.query_params.get("view") or "").lower().strip()

        if print_format == "json":
            return Response({
                "status": "success",
                "student_name": student_name_str,
                "standard_medium": std_med_str,
                "father_mother": father_mother_str,
                "whatsapp_contact": first_pp.get("father_mobile") or first_pp.get("mother_mobile") or "-",
                "assessment_score": assessment_summary_str,
                "total_rendered": len(forms_data),
                "forms": forms_data,
                "html": html_content
            }, status=status.HTTP_200_OK)

        # Check if caller wants direct binary PDF / ZIP download (Default when hitting /download/, /download-bulk/ or ?download=1)
        is_direct_pdf_download = "download" in request.path.lower() or request.query_params.get("download") in ["1", "true"]
        is_preview_requested = request.query_params.get("preview") in ["1", "true"] or request.query_params.get("no_auto_pdf") == "1"

        # Case A: Single Student Direct Binary PDF Download
        if is_direct_pdf_download and not is_preview_requested and len(applications) == 1:
            try:
                from .pdf_generator import generate_single_student_complete_pdf_stream
                single_app = applications[0]
                single_form_data = forms_data[0] if forms_data else {}
                pdf_bytes = generate_single_student_complete_pdf_stream(single_app, single_form_data)

                app_no_clean = re.sub(r'[^a-zA-Z0-9_\-]', '', str(single_app.application_number or f"SF_{single_app.id}"))
                student_name_clean = re.sub(r'[^a-zA-Z0-9_\-]', '_', f"{single_app.child_first_name}_{single_app.child_surname}").strip('_')
                download_filename = f"Admission_And_Quiz_{app_no_clean}_{student_name_clean}.pdf"

                pdf_response = HttpResponse(pdf_bytes, content_type="application/pdf")
                pdf_response['Content-Disposition'] = f'attachment; filename="{download_filename}"'
                pdf_response['Content-Length'] = len(pdf_bytes)
                return pdf_response
            except Exception as e:
                logger.error(f"Native PDF binary generation failed, falling back to HTML: {e}")

        # Case B: Multiple Students Bulk Download (Option 1 - Clean ZIP Package of individual student PDFs)
        elif is_direct_pdf_download and not is_preview_requested and len(applications) > 1:
            try:
                import io
                import zipfile
                from .pdf_generator import generate_single_student_complete_pdf_stream

                zip_buffer = io.BytesIO()
                with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zip_file:
                    for idx, app_item in enumerate(applications):
                        app_data = forms_data[idx] if idx < len(forms_data) else {}
                        try:
                            item_pdf_bytes = generate_single_student_complete_pdf_stream(app_item, app_data)
                            app_no_c = re.sub(r'[^a-zA-Z0-9_\-]', '', str(app_item.application_number or f"SF_{app_item.id}"))
                            name_c = re.sub(r'[^a-zA-Z0-9_\-]', '_', f"{app_item.child_first_name}_{app_item.child_surname}").strip('_')
                            file_in_zip = f"{idx + 1:02d}_Admission_{app_no_c}_{name_c}.pdf"
                            zip_file.writestr(file_in_zip, item_pdf_bytes)
                        except Exception as item_err:
                            logger.error(f"Failed to generate PDF for application {app_item.id} in bulk: {item_err}")

                zip_buffer.seek(0)
                zip_data = zip_buffer.getvalue()

                # Generate clean zip filename based on criteria
                q_std = request.query_params.get("standard") or "All"
                q_med = request.query_params.get("medium") or "All"
                q_st = request.query_params.get("status") or "SELECTED"
                zip_filename = f"Shreyas_Admissions_Std_{q_std}_{q_med}_{q_st}_{len(applications)}_Students.zip"

                zip_response = HttpResponse(zip_data, content_type="application/zip")
                zip_response['Content-Disposition'] = f'attachment; filename="{zip_filename}"'
                zip_response['Content-Length'] = len(zip_data)
                return zip_response
            except Exception as zip_err:
                logger.error(f"Bulk ZIP download failed, falling back to HTML: {zip_err}")

        # Fallback / Preview HTML response
        response = HttpResponse(html_content, content_type="text/html; charset=utf-8")
        return response


class AdminAcademicYearRollView(APIView):
    """
    Step 8: New Academic Year Reset & Archival
    GET: Summary of current active academic season.
    POST: Archive current applications and rollover to next academic year.
    Endpoint: /api/admin/academic-year/rollover/
    """
    def get(self, request):
        denied = enforce_admin_token(request)
        if denied:
            return denied

        total_enquiries = QuizSubmission.objects.count()
        total_applications = AdmissionApplication.objects.count()
        total_selected = AdmissionApplication.objects.filter(admission_status='SELECTED').count()
        total_waitlisted = AdmissionApplication.objects.filter(admission_status='WAITLISTED').count()

        return Response({
            "status": "success",
            "current_season": "2026-2027",
            "statistics": {
                "total_enquiries": total_enquiries,
                "total_applications": total_applications,
                "selected_admissions": total_selected,
                "waitlisted_admissions": total_waitlisted
            },
            "ready_for_next_year": True
        }, status=status.HTTP_200_OK)


# ==============================================================================
# WhatsApp Cloud API & CRM SaaS 2-Way Webhook
# ==============================================================================

class WhatsAppWebhookView(APIView):
    """
    Two-Way WhatsApp Webhook:
    1. GET: Handshake verification for Meta / WhatsApp SaaS (hub.challenge),
            OR live status inspection when called without hub.mode (e.g. GET /api/whatsapp/webhook/?phone=917874566064 or ?message_id=wamid...)
    2. POST: Receives real-time message updates, delivery statuses (sent, delivered, read, failed), and incoming chats.
             Returns parsed status details directly in the response JSON.
    Endpoint: /api/whatsapp/webhook/
    """
    authentication_classes = []
    permission_classes = []

    def get(self, request):
        mode = request.query_params.get("hub.mode")
        token = request.query_params.get("hub.verify_token")
        challenge = request.query_params.get("hub.challenge")

        # 1. Meta / SaaS Webhook Verification Handshake
        if mode:
            expected_token = os.getenv("WHATSAPP_WEBHOOK_VERIFY_TOKEN", "shreyas_webhook_token_2026")
            if mode == "subscribe" and token == expected_token:
                logger.info(f"[WHATSAPP WEBHOOK] Subscription verified successfully with token: {token}")
                from django.http import HttpResponse
                return HttpResponse(challenge, content_type="text/plain", status=200)

            logger.warning(f"[WHATSAPP WEBHOOK] Verification failed. Mode: {mode}, Token: {token}")
            return Response({"error": "Verification token mismatch"}, status=status.HTTP_403_FORBIDDEN)

        # 2. Status Inspection Mode (GET /api/whatsapp/webhook/?message_id=... or ?phone=... or ?submission_id=...)
        q_msg_id = (request.query_params.get("message_id") or "").strip()
        q_phone = normalize_phone_number(request.query_params.get("phone") or "")
        q_sub_id = (request.query_params.get("submission_id") or "").strip()

        all_tracked = list(WHATSAPP_STATUS_TRACKER.values())
        if q_msg_id:
            all_tracked = [m for m in all_tracked if m.get("message_id") == q_msg_id]
        if q_phone:
            all_tracked = [m for m in all_tracked if q_phone in str(m.get("recipient", ""))]
        if q_sub_id and q_sub_id.isdigit():
            all_tracked = [m for m in all_tracked if str(m.get("submission_id")) == q_sub_id]

        # Sort newest first
        all_tracked.reverse()

        summary = {
            "sent": sum(1 for m in all_tracked if m.get("current_status") == "sent"),
            "delivered": sum(1 for m in all_tracked if m.get("current_status") == "delivered"),
            "read": sum(1 for m in all_tracked if m.get("current_status") == "read"),
            "failed": sum(1 for m in all_tracked if m.get("current_status") == "failed"),
        }

        return Response({
            "status": "success",
            "total_tracked_messages": len(all_tracked),
            "status_summary": summary,
            "messages": all_tracked
        }, status=status.HTTP_200_OK)

    def post(self, request):
        payload = request.data
        logger.info(f"[WHATSAPP WEBHOOK RECEIVED] Payload: {json.dumps(payload)[:500]}")

        parsed_statuses = []
        parsed_messages = []

        if isinstance(payload, dict):
            # Case A: Flat status callback payload (e.g. forwarded from SaaS CRM)
            if payload.get("message_id") and payload.get("status"):
                m_id = str(payload.get("message_id"))
                st_val = str(payload.get("status")).lower()
                recip = normalize_phone_number(payload.get("recipient_id") or payload.get("phone") or "")
                ts = str(payload.get("timestamp") or "")
                errs = payload.get("errors") or []
                record_whatsapp_message_status(
                    message_id=m_id,
                    status_val=st_val,
                    recipient=recip,
                    timestamp=ts,
                    errors=errs
                )
                parsed_statuses.append({
                    "message_id": m_id,
                    "delivery_status": st_val,  # sent | delivered | read | failed
                    "recipient_id": recip,
                    "timestamp": ts,
                    "errors": errs
                })

            # Case B: Standard Meta entry -> changes -> value -> statuses / messages
            for entry in payload.get("entry", []):
                if not isinstance(entry, dict):
                    continue
                for change in entry.get("changes", []):
                    if not isinstance(change, dict):
                        continue
                    val = change.get("value", {})
                    if not isinstance(val, dict):
                        continue

                    for st in val.get("statuses", []):
                        if not isinstance(st, dict):
                            continue
                        m_id = str(st.get("id") or st.get("message_id") or "")
                        st_val = str(st.get("status") or "").lower()
                        recip = normalize_phone_number(st.get("recipient_id") or "")
                        ts = str(st.get("timestamp") or "")
                        errs = st.get("errors") or []
                        if m_id and st_val:
                            record_whatsapp_message_status(
                                message_id=m_id,
                                status_val=st_val,
                                recipient=recip,
                                timestamp=ts,
                                errors=errs
                            )
                            # Avoid duplicate entry if both flat and nested were in same payload
                            if not any(ps["message_id"] == m_id and ps["delivery_status"] == st_val for ps in parsed_statuses):
                                parsed_statuses.append({
                                    "message_id": m_id,
                                    "delivery_status": st_val,  # sent | delivered | read | failed
                                    "recipient_id": recip,
                                    "timestamp": ts,
                                    "errors": errs
                                })

                    for msg in val.get("messages", []):
                        if not isinstance(msg, dict):
                            continue
                        # If it's a LOG_TEMPLATE / TRIGGER_TEMPLATE event
                        if msg.get("event") in ["LOG_TEMPLATE", "TRIGGER_TEMPLATE"]:
                            m_id = str(msg.get("message_id") or "")
                            recip = normalize_phone_number(msg.get("phone") or "")
                            tpl_name = str(msg.get("template_name") or "")
                            sub_id = msg.get("submission_id")
                            if m_id:
                                record_whatsapp_message_status(
                                    message_id=m_id,
                                    status_val="sent",
                                    recipient=recip,
                                    template_name=tpl_name,
                                    submission_id=sub_id
                                )
                            parsed_messages.append({
                                "event": msg.get("event"),
                                "message_id": m_id,
                                "delivery_status": "sent",
                                "recipient": recip,
                                "template_name": tpl_name,
                                "submission_id": sub_id
                            })
                        else:
                            m_id = str(msg.get("id") or "")
                            sender = normalize_phone_number(msg.get("from") or "")
                            m_type = str(msg.get("type") or "text")
                            text_body = (msg.get("text") or {}).get("body", "") if isinstance(msg.get("text"), dict) else ""
                            parsed_messages.append({
                                "message_id": m_id,
                                "from": sender,
                                "type": m_type,
                                "text": text_body,
                                "direction": "inbound"
                            })

        # Avoid bounce-back loop if payload was already forwarded from WhatsApp SaaS CRM
        is_from_saas_crm = bool(
            request.headers.get("X-Forwarded-From-SaaS-CRM")
            or request.META.get("HTTP_X_FORWARDED_FROM_SAAS_CRM")
            or (isinstance(payload, dict) and (
                payload.get("forwarded_from_saas_crm")
                or payload.get("forwarded_from_shreyas")
                or payload.get("via_crm")
            ))
        )

        # Forward to external WhatsApp SaaS CRM only if not already originating from SaaS CRM
        if WHATSAPP_CRM_WEBHOOK_URL and not is_from_saas_crm:
            try:
                def _forward(url, data, headers):
                    try:
                        requests.post(url, json=data, headers=headers, timeout=10)
                        logger.info(f"[WHATSAPP FORWARD] Successfully forwarded webhook to {url}")
                    except Exception as fe:
                        logger.warning(f"[WHATSAPP FORWARD FAILED] Could not forward to {url}: {fe}")

                forward_headers = {"Content-Type": "application/json", "X-Forwarded-From-Shreyas": "1"}
                threading.Thread(target=_forward, args=(WHATSAPP_CRM_WEBHOOK_URL, payload, forward_headers), daemon=True).start()
            except Exception as e:
                logger.error(f"[WHATSAPP WEBHOOK FORWARD ERROR] {e}")

        # Return rich JSON response showing exact Meta status (sent, delivered, read, failed)
        primary_status = parsed_statuses[0]["delivery_status"] if parsed_statuses else (
            parsed_messages[0].get("delivery_status", "sent") if parsed_messages else "sent"
        )
        return Response({
            "status": primary_status,
            "delivery_status": primary_status,
            "success": True,
            "statuses": parsed_statuses,
            "messages": parsed_messages
        }, status=status.HTTP_200_OK)

