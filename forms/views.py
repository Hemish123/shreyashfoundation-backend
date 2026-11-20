import logging
import json
import random
import os
import requests
import threading
from datetime import timedelta

from django.shortcuts import get_object_or_404
from django.conf import settings
from django.db import models
from django.db.models import Q
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
    normalize_phone_number
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
    Requires 'Authorization: Bearer <token>' in request headers.
    Returns (is_authenticated, admin_user_obj, role)
    """
    auth_header = request.headers.get('Authorization') or request.META.get('HTTP_AUTHORIZATION') or ""
    if auth_header and auth_header.lower().startswith('bearer '):
        user = get_admin_user_from_token(auth_header)
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
        queryset = AdmissionApplication.objects.filter(
            interaction_date__isnull=False
        ).select_related('submission').order_by('interaction_date', 'interaction_slot_no', 'created_at')

        if filter_date_str:
            queryset = queryset.filter(interaction_date=filter_date_str)

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
                    "application_number": app.application_number,
                    "child_name": f"{app.child_first_name} {app.child_surname}".strip(),
                    "standard": app.standard,
                    "medium": app.medium,
                    "vibhag": app.vibhag,
                    "father_name": app.father_name,
                    "father_whatsapp": app.father_whatsapp or (app.submission.whatsapp_number if app.submission else ""),
                    "whatsapp_sent": app.interaction_whatsapp_sent
                })

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

        return Response({
            "status": "success",
            "total_days_scheduled": len(days_list),
            "calendar": days_list
        }, status=status.HTTP_200_OK)


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
        period = (request.query_params.get("period") or request.GET.get("period") or "all").lower().strip()
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

        if period == "today":
            start_date = now.replace(hour=0, minute=0, second=0, microsecond=0)
        elif period == "week":
            start_date = now - timedelta(days=7)
        elif period == "month":
            start_date = now - timedelta(days=30)

        # 1. Base Queryset Filtering
        if export_type in ["submitted", "submissions", "submitted_applications"]:
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
            qs = QuizSubmission.objects.select_related('created_by').order_by("-created_at")

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

        queryset = QuizSubmission.objects.all().order_by("-created_at")

        # Role-based scoping:
        # ADMIN sees everything.
        # Other roles (STAFF, ACADEMICIAN) see only their own created enquiries.
        if role != 'ADMIN' and logged_in_admin:
            queryset = queryset.filter(created_by=logged_in_admin)

        vibhag = request.query_params.get("vibhag")
        if vibhag:
            queryset = queryset.filter(vibhag__iexact=vibhag)

        is_eligible = request.query_params.get("is_eligible")
        if is_eligible is not None:
            queryset = queryset.filter(is_eligible=(is_eligible.lower() == 'true'))

        payment_status = request.query_params.get("payment_status")
        if payment_status:
            queryset = queryset.filter(payment_status__iexact=payment_status)

        # Card summary counts (calculated on full filtered queryset before slicing)
        total_count = queryset.count()
        fees_collected = queryset.filter(payment_status="RECEIVED").count()
        qa_passed = queryset.filter(qa_status="PASSED").count()
        qa_failed = queryset.filter(qa_status="FAILED").count()
        qa_pending = queryset.filter(qa_status="PENDING").count()
        submitted_forms = queryset.filter(admission_application__isnull=False).count()
        pending_forms = queryset.filter(payment_status="RECEIVED", admission_application__isnull=True).count()

        # Pagination support (defaults to page 1, 50 records per page)
        page = int(request.query_params.get("page", 1))
        page_size = int(request.query_params.get("page_size", request.query_params.get("limit", 50)))
        if page < 1:
            page = 1
        if page_size < 1:
            page_size = 50

        total_pages = (total_count + page_size - 1) // page_size if total_count > 0 else 1
        offset = (page - 1) * page_size
        paged_queryset = queryset[offset:offset + page_size]

        serializer = QuizSubmissionListSerializer(paged_queryset, many=True)

        return Response({
            "status": "success",
            "scope": "ALL" if role == 'ADMIN' else "MY_ENQUIRIES",
            "user_role": role,
            "pagination": {
                "page": page,
                "page_size": page_size,
                "total_records": total_count,
                "total_pages": total_pages,
                "has_next": page < total_pages,
                "has_previous": page > 1
            },
            "total_enquiries": total_count,
            "returned_enquiries": len(serializer.data),
            "fees_collected": fees_collected,
            "fee_unpaid": total_count - fees_collected,
            "qa_passed": qa_passed,
            "qa_failed": qa_failed,
            "qa_pending": qa_pending,
            "submitted_forms": submitted_forms,
            "pending_forms": pending_forms,
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
                "is_locked_for_user": True
            }
        }, status=status.HTTP_200_OK)


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

        submission.save(update_fields=updated_fields)
        logger.info(f"[AUTOSAVE] Submission {submission_id} progress autosaved at {submission.last_draft_saved_at}")

        return Response({
            "status": "success",
            "message": "Draft auto-saved successfully.",
            "submission_id": submission.id,
            "last_draft_saved_at": submission.last_draft_saved_at,
            "quiz_draft_saved": bool(quiz_draft is not None),
            "form_draft_saved": bool(form_draft is not None)
        }, status=status.HTTP_200_OK)


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

        applications = AdmissionApplication.objects.all().order_by("-created_at")

        # Role-based scoping for Submitted Applications:
        # ADMIN sees all applications.
        # Other roles (STAFF / ACADEMICIAN) see only applications originating from their enquiries.
        if role != 'ADMIN' and logged_in_admin:
            applications = applications.filter(submission__created_by=logged_in_admin)

        serializer = AdmissionApplicationSerializer(applications, many=True)
        return Response({
            "status": "success",
            "scope": "ALL" if role == 'ADMIN' else "MY_APPLICATIONS",
            "user_role": role,
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
            # 1. Save Application
            application = serializer.save(submission=submission, form_submitted_at=timezone.now())
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
                return Response({
                    "status": "success",
                    "message": "Application and enquiry updated successfully by Admin.",
                    "data": serializer.data
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
    GET: Retrieve applicant metadata, Part X calculation breakdown, and existing Part Y scores.
    POST: Save Part Y evaluation (Cognitive 5+5, Affective 10, Psychomotor 10), aggregate total 50 marks.
    Endpoint: /api/admin/evaluation/<application_id>/
    Header: Authorization: Bearer <ADMIN_PERMANENT_TOKEN>
    """
    def get(self, request, application_id):
        denied = enforce_admin_token(request)
        if denied:
            return denied

        application = AdmissionApplication.objects.select_related('submission', 'evaluator').filter(id=application_id).first()
        if not application:
            return Response({
                "status": "error",
                "message": f"Admission application {application_id} not found."
            }, status=status.HTTP_404_NOT_FOUND)

        # Pre-calculate or ensure Part X score is up to date
        from .evaluation_service import calculate_part_x_score
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

        return Response({
            "status": "success",
            "application_id": application.id,
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
                "max_marks": 20,
                "alak_malak_months": application.alak_malak_months,
                "alak_malak_score": application.alak_malak_score,
                "alak_malak_certificate_uploaded": bool(application.alak_malak_certificate),
                "mcq_score": application.mcq_score_part_x,
                "sibling_in_shreyas": application.has_sibling_in_shreyas,
                "sibling_opted_vk": application.sibling_opted_vk,
                "sibling_receipt_uploaded": bool(application.sibling_vikas_kendra_1_receipt or application.sibling_vikas_kendra_2_receipt),
                "sibling_score": application.sibling_score,
                "total_part_x_score": application.total_part_x_score
            },
            "part_y": {
                "max_marks": 30,
                "evaluator": application.evaluator.full_name or application.evaluator.username if application.evaluator else None,
                "m_tag": application.evaluator_m or "",
                "mi_tag": application.evaluator_mi or "",
                "cognitive_assessment_1": application.cognitive_assessment_1 or 0.0,
                "cognitive_assessment_2": application.cognitive_assessment_2 or 0.0,
                "affective_assessment": application.affective_assessment or 0.0,
                "psychomotor_assessment": application.psychomotor_assessment or 0.0,
                "total_part_y_score": application.total_part_y_score or 0.0
            },
            "final_aggregate_score": application.final_aggregate_score or 0.0,
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
        final_score = save_part_y_evaluation(
            application=application,
            evaluator_user=logged_in_admin,
            m_tag=vd.get('m_tag', ''),
            mi_tag=vd.get('mi_tag', ''),
            cognitive_1=vd.get('cognitive_assessment_1', 0.0),
            cognitive_2=vd.get('cognitive_assessment_2', 0.0),
            affective=vd.get('affective_assessment', 0.0),
            psychomotor=vd.get('psychomotor_assessment', 0.0)
        )

        return Response({
            "status": "success",
            "message": f"Digital assessment evaluated and saved successfully for {application.application_number}.",
            "application_id": application.id,
            "application_number": application.application_number,
            "part_x_score": application.total_part_x_score,
            "part_y_score": application.total_part_y_score,
            "final_aggregate_score": final_score,
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
        cutoff_seats = int(request.query_params.get("cutoff") or 80)
        recompute = request.query_params.get("recompute", "true").lower() in ["true", "1", "yes"]

        # If specific standard and medium provided, recompute for that slice
        if standard and medium and not standard.lower().startswith("all") and not medium.lower().startswith("all"):
            if recompute:
                summary = recompute_merit_rank_and_cutoff(standard=standard, medium=medium, cutoff_seats=cutoff_seats)
            else:
                summary = {
                    "standard": standard,
                    "medium": medium,
                    "cutoff_seats": cutoff_seats
                }
            ranked_apps = AdmissionApplication.objects.filter(
                standard__iexact=standard,
                medium__iexact=medium
            ).exclude(admission_status__in=['CANCELLED', 'REJECTED']).order_by('merit_rank', '-final_aggregate_score')
        else:
            # If "All Standards" or "All Mediums" is selected, rank across all standard x medium pairs
            all_pairs = AdmissionApplication.objects.values_list('standard', 'medium').distinct()
            summaries = []
            if recompute:
                # Rank candidates dynamically by score descending
                for std_val, med_val in all_pairs:
                    if std_val and med_val:
                        s = recompute_merit_rank_and_cutoff(standard=std_val, medium=med_val, cutoff_seats=cutoff_seats)
                        summaries.append(s)
            
            ranked_apps = AdmissionApplication.objects.exclude(
                admission_status__in=['CANCELLED', 'REJECTED']
            ).order_by('-final_aggregate_score', 'merit_rank', 'created_at')

            # Populate cross-cutting merit ranks if viewing All Standards
            assigned_rank = 1
            for app_obj in ranked_apps:
                app_obj.merit_rank = assigned_rank
                assigned_rank += 1

            summary = {
                "standard": standard or "All Standards",
                "medium": medium or "All Mediums",
                "total_ranked": ranked_apps.count(),
                "selected_seats": ranked_apps.filter(admission_status='SELECTED').count(),
                "waitlisted": ranked_apps.filter(admission_status='WAITLISTED').count(),
                "cutoff_seats": cutoff_seats
            }

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
        paged_ranked_apps = ranked_apps[offset:offset + page_size]

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


def generate_professional_school_admission_form_html(forms_list):
    """
    Renders an authentic, high-end, professional School Admission Physical Form (A4 printable).
    Features:
    - Official Shreyas Foundation & Vikas Kendra crest header
    - Form metadata, Passport photo box with dashed border
    - Standard tabular school layout (Student profile, Family bio, Residential address)
    - Part X & Part Y assessment breakdown table with marks
    - Signatures block (Mother, Father, Guardian, Principal/Head of School)
    - CSS @media print optimization (auto page break, crisp typography)
    """
    cards_html = ""
    for idx, item in enumerate(forms_list):
        sh = item.get("school_header", {})
        fd = item.get("form_details", {})
        sp = item.get("student_profile", {})
        pp = item.get("parent_profile", {})
        scores = item.get("assessment_scores", {})
        px = scores.get("part_x", {})
        py = scores.get("part_y", {})
        sigs = item.get("signatures_and_declarations", {})

        photo_html = f'<img src="{sp.get("photo_url")}" class="student-photo" alt="Student Photo" />' if sp.get("photo_url") else '<div class="photo-placeholder">Affix Recent<br>Passport Size<br>Photograph Here</div>'
        father_sig_html = f'<img src="{sigs.get("father_signature_url")}" class="sig-img" />' if sigs.get("father_signature_url") else '<div class="sig-space"></div>'
        mother_sig_html = f'<img src="{sigs.get("mother_signature_url")}" class="sig-img" />' if sigs.get("mother_signature_url") else '<div class="sig-space"></div>'

        break_class = "page-break" if idx > 0 else ""

        cards_html += f"""
        <div class="admission-sheet {break_class}">
            <!-- Header Section -->
            <div class="header-container">
                <div class="logo-area">
                    <div class="school-logo">🌿</div>
                </div>
                <div class="header-text">
                    <h1 class="trust-title">{sh.get('trust_name', 'Shreyas Foundation').upper()}</h1>
                    <h2 class="school-title">{sh.get('school_name', 'Vikas Kendra').upper()}</h2>
                    <p class="school-subtitle">Post Box No. 4070, Near Manekbaug Hall, Ambawadi, Ahmedabad – 380015</p>
                    <div class="badge-title">OFFICIAL ADMISSION APPLICATION & ASSESSMENT RECORD</div>
                    <div class="academic-year">ACADEMIC SESSION: {sh.get('academic_session', '2026 - 2027')}</div>
                </div>
                <div class="photo-container">
                    {photo_html}
                </div>
            </div>

            <!-- Meta Strip -->
            <table class="form-meta-table">
                <tr>
                    <td style="width: 25%;"><strong>Application No:</strong> <span class="highlight">{fd.get('application_number')}</span></td>
                    <td style="width: 25%;"><strong>Date of Submission:</strong> {fd.get('date_of_submission')}</td>
                    <td style="width: 25%;"><strong>Standard & Medium:</strong> {fd.get('standard')} ({fd.get('medium')})</td>
                    <td style="width: 25%;"><strong>Vibhag:</strong> {fd.get('vibhag')}</td>
                </tr>
                <tr>
                    <td><strong>Merit Rank:</strong> <span class="rank-badge">#{fd.get('merit_rank') or '-'}</span></td>
                    <td><strong>Admission Status:</strong> <span class="status-pill">{fd.get('admission_status')}</span></td>
                    <td colspan="2"><strong>Cutoff Qualification:</strong> Eligible for Enrollment</td>
                </tr>
            </table>

            <!-- Section 1: Student Information -->
            <div class="section-title">1. STUDENT PARTICULARS</div>
            <table class="grid-table">
                <tr>
                    <td class="lbl" style="width: 20%;">Student's Full Name:</td>
                    <td colspan="3" class="val" style="font-weight: bold; font-size: 13px;">{sp.get('first_name')} {sp.get('surname')}</td>
                </tr>
                <tr>
                    <td class="lbl">Date of Birth:</td>
                    <td class="val" style="width: 30%;">{sp.get('date_of_birth')}</td>
                    <td class="lbl" style="width: 20%;">Gender:</td>
                    <td class="val" style="width: 30%;">{sp.get('gender') or 'Not Specified'}</td>
                </tr>
                <tr>
                    <td class="lbl">Place of Birth:</td>
                    <td class="val">{sp.get('birth_place')}</td>
                    <td class="lbl">Blood Group:</td>
                    <td class="val">{sp.get('blood_group')}</td>
                </tr>
                <tr>
                    <td class="lbl">Mother Tongue:</td>
                    <td class="val">{sp.get('mother_tongue')}</td>
                    <td class="lbl">Nationality:</td>
                    <td class="val">{sp.get('nationality')}</td>
                </tr>
            </table>

            <!-- Section 2: Family Information -->
            <div class="section-title">2. PARENTS / GUARDIAN PARTICULARS</div>
            <table class="grid-table">
                <thead>
                    <tr class="sub-head">
                        <th style="width: 20%;">Particulars</th>
                        <th style="width: 40%;">Father / Primary Guardian</th>
                        <th style="width: 40%;">Mother</th>
                    </tr>
                </thead>
                <tbody>
                    <tr>
                        <td class="lbl">Full Name:</td>
                        <td class="val font-semibold">{pp.get('father_name')} {pp.get('father_surname')}</td>
                        <td class="val font-semibold">{pp.get('mother_name')} {pp.get('mother_surname')}</td>
                    </tr>
                    <tr>
                        <td class="lbl">Educational Qualification:</td>
                        <td class="val">{pp.get('father_qualification') or '-'}</td>
                        <td class="val">{pp.get('mother_qualification') or '-'}</td>
                    </tr>
                    <tr>
                        <td class="lbl">Occupation & Designation:</td>
                        <td class="val">{pp.get('father_occupation') or '-'}</td>
                        <td class="val">{pp.get('mother_occupation') or '-'}</td>
                    </tr>
                    <tr>
                        <td class="lbl">Annual Income:</td>
                        <td class="val">{pp.get('father_annual_income') or '-'}</td>
                        <td class="val">{pp.get('mother_annual_income') or '-'}</td>
                    </tr>
                    <tr>
                        <td class="lbl">Mobile / WhatsApp:</td>
                        <td class="val">{pp.get('father_mobile')}</td>
                        <td class="val">{pp.get('mother_mobile')}</td>
                    </tr>
                    <tr>
                        <td class="lbl">Email Address:</td>
                        <td class="val">{pp.get('father_email') or '-'}</td>
                        <td class="val">{pp.get('mother_email') or '-'}</td>
                    </tr>
                    <tr>
                        <td class="lbl">Residential Address:</td>
                        <td colspan="2" class="val">{pp.get('residential_address')}</td>
                    </tr>
                </tbody>
            </table>

            <!-- Section 3: Assessment Summary -->
            <div class="section-title">3. ASSESSMENT & MERIT EVALUATION SCORECARD</div>
            <table class="grid-table score-table">
                <thead>
                    <tr class="sub-head">
                        <th colspan="2">PART X: APPLICATION & BACKGROUND (MAX 20)</th>
                        <th colspan="2">PART Y: EDUCATOR INTERACTION ASSESSMENT (MAX 30)</th>
                    </tr>
                </thead>
                <tbody>
                    <tr>
                        <td class="lbl" style="width: 30%;">Alak Malak Pre-Toddler Bonus:</td>
                        <td class="val text-center" style="width: 20%; font-weight: bold;">{px.get('alak_malak', 0)} / 10</td>
                        <td class="lbl" style="width: 30%;">Cognitive Assessment (5 + 5):</td>
                        <td class="val text-center" style="width: 20%; font-weight: bold;">{(py.get('cognitive_1') or 0) + (py.get('cognitive_2') or 0)} / 10</td>
                    </tr>
                    <tr>
                        <td class="lbl">Parent Screening MCQ Score:</td>
                        <td class="val text-center" style="font-weight: bold;">{px.get('parent_mcq', 0)} / 7</td>
                        <td class="lbl">Affective Domain Assessment:</td>
                        <td class="val text-center" style="font-weight: bold;">{py.get('affective', 0)} / 10</td>
                    </tr>
                    <tr>
                        <td class="lbl">Sibling Shreyas VK Bonus:</td>
                        <td class="val text-center" style="font-weight: bold;">{px.get('sibling_vk', 0)} / 3</td>
                        <td class="lbl">Psychomotor Domain Assessment:</td>
                        <td class="val text-center" style="font-weight: bold;">{py.get('psychomotor', 0)} / 10</td>
                    </tr>
                    <tr>
                        <td class="lbl">Evaluation Observation Tags:</td>
                        <td class="val text-center text-xs">M: {py.get('m_category') or '-'} | MI: {py.get('mi_category') or '-'}</td>
                        <td class="lbl">Assigned Evaluator:</td>
                        <td class="val text-center text-xs">{py.get('evaluator') or 'Admissions Board'}</td>
                    </tr>
                    <tr class="total-row">
                        <td class="total-lbl">TOTAL PART X SCORE:</td>
                        <td class="total-val">{px.get('total_part_x', 0)} / 20</td>
                        <td class="total-lbl">TOTAL PART Y SCORE:</td>
                        <td class="total-val">{py.get('total_part_y', 0)} / 30</td>
                    </tr>
                    <tr class="grand-total-row">
                        <td colspan="2" class="grand-lbl">FINAL AGGREGATE MERIT SCORE:</td>
                        <td colspan="2" class="grand-val">{scores.get('final_score', 0)} / 50 Marks (Rank: #{fd.get('merit_rank') or '-'})</td>
                    </tr>
                </tbody>
            </table>

            <!-- Section 4: Declaration & Signatures -->
            <div class="declaration-block">
                <strong>Declaration:</strong> I/We hereby declare that all information furnished in this admission application is true and complete to the best of my/our knowledge. I/We agree to abide by the philosophy, rules, and code of conduct of Shreyas Foundation Vikas Kendra.
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
                    <div class="sig-space"></div>
                    <div class="sig-line">Educator / Evaluator</div>
                </div>
                <div class="sig-box">
                    <div class="sig-space"></div>
                    <div class="sig-line">Head of School / Director</div>
                </div>
            </div>

            <!-- Footer -->
            <div class="form-footer">
                <div>Document Generated On: {fd.get('date_of_submission')} | Vikas Kendra Admissions Control Center</div>
                <div>Page 1 of 1</div>
            </div>
        </div>
        """

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>Official Admission Form - Shreyas Foundation</title>
    <style>
        @page {{
            size: A4 portrait;
            margin: 10mm;
        }}
        * {{
            box-sizing: border-box;
            -webkit-print-color-adjust: exact !important;
            print-color-adjust: exact !important;
        }}
        body {{
            font-family: 'Segoe UI', Calibri, Arial, sans-serif;
            margin: 0;
            padding: 10px;
            background-color: #f1f5f9;
            color: #1e293b;
        }}
        .admission-sheet {{
            width: 210mm;
            min-height: 285mm;
            padding: 12mm 14mm;
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
            padding-bottom: 8px;
            margin-bottom: 8px;
        }}
        .logo-area {{
            width: 65px;
            text-align: center;
        }}
        .school-logo {{
            font-size: 42px;
            line-height: 1;
        }}
        .header-text {{
            flex: 1;
            text-align: center;
            padding: 0 10px;
        }}
        .trust-title {{
            margin: 0;
            font-size: 16px;
            font-weight: 800;
            letter-spacing: 1px;
            color: #0f3d2e;
        }}
        .school-title {{
            margin: 2px 0 0 0;
            font-size: 20px;
            font-weight: 900;
            letter-spacing: 1.5px;
            color: #8b1e28;
        }}
        .school-subtitle {{
            margin: 2px 0;
            font-size: 9px;
            color: #475569;
        }}
        .badge-title {{
            display: inline-block;
            background-color: #0f3d2e;
            color: #ffffff;
            font-size: 11px;
            font-weight: bold;
            padding: 3px 12px;
            border-radius: 3px;
            margin-top: 3px;
            letter-spacing: 0.5px;
        }}
        .academic-year {{
            font-size: 10px;
            font-weight: bold;
            color: #1e293b;
            margin-top: 2px;
        }}
        .photo-container {{
            width: 85px;
            height: 100px;
            border: 1.5px dashed #0f3d2e;
            display: flex;
            align-items: center;
            justify-content: center;
            background: #fafaf9;
            overflow: hidden;
        }}
        .photo-placeholder {{
            font-size: 8px;
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
            margin-bottom: 8px;
            font-size: 10.5px;
            background: #f8fafc;
            border: 1px solid #cbd5e1;
        }}
        .form-meta-table td {{
            padding: 4px 6px;
            border: 1px solid #cbd5e1;
        }}
        .highlight {{
            color: #8b1e28;
            font-weight: bold;
        }}
        .rank-badge {{
            background: #fef08a;
            color: #854d0e;
            padding: 1px 6px;
            font-weight: bold;
            border-radius: 3px;
        }}
        .status-pill {{
            background: #dcfce7;
            color: #166534;
            padding: 1px 6px;
            font-weight: bold;
            border-radius: 3px;
        }}
        .section-title {{
            background: #0f3d2e;
            color: #ffffff;
            font-size: 10.5px;
            font-weight: bold;
            padding: 3px 8px;
            letter-spacing: 0.5px;
            margin-top: 6px;
            border-left: 4px solid #8b1e28;
        }}
        .grid-table {{
            width: 100%;
            border-collapse: collapse;
            margin-bottom: 6px;
            font-size: 10px;
        }}
        .grid-table th, .grid-table td {{
            border: 1px solid #cbd5e1;
            padding: 4px 6px;
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
            font-size: 9.5px;
            text-align: center;
            font-weight: 700;
            padding: 4px;
        }}
        .text-center {{ text-align: center; }}
        .font-semibold {{ font-weight: 600; }}
        .text-xs {{ font-size: 9px; }}
        
        .score-table .total-row td {{
            background-color: #f8fafc;
            border-top: 2px solid #0f3d2e;
            font-weight: bold;
            font-size: 10px;
        }}
        .total-lbl {{ color: #0f3d2e; font-weight: bold; }}
        .total-val {{ text-align: center; font-weight: bold; color: #8b1e28; font-size: 11px; }}
        
        .grand-total-row td {{
            background-color: #f0fdf4 !important;
            border-top: 2px solid #16a34a !important;
            border-bottom: 2px solid #16a34a !important;
        }}
        .grand-lbl {{
            font-weight: 900;
            color: #166534;
            font-size: 11px;
            text-align: right;
            padding-right: 12px;
        }}
        .grand-val {{
            font-weight: 900;
            color: #166534;
            font-size: 12px;
            text-align: center;
        }}
        .declaration-block {{
            border: 1px dashed #cbd5e1;
            padding: 6px 8px;
            font-size: 8.5px;
            color: #475569;
            line-height: 1.3;
            margin-top: 6px;
            margin-bottom: 14px;
            background: #fafaf9;
        }}
        .signatures-container {{
            display: flex;
            justify-content: space-between;
            margin-top: 10px;
            padding-top: 5px;
        }}
        .sig-box {{
            width: 23%;
            text-align: center;
        }}
        .sig-space {{
            height: 35px;
        }}
        .sig-img {{
            max-height: 35px;
            max-width: 100%;
            display: block;
            margin: 0 auto;
        }}
        .sig-line {{
            border-top: 1px solid #475569;
            padding-top: 3px;
            font-size: 9px;
            font-weight: 600;
            color: #334155;
        }}
        .form-footer {{
            display: flex;
            justify-content: space-between;
            font-size: 8px;
            color: #94a3b8;
            border-top: 1px solid #e2e8f0;
            padding-top: 4px;
            margin-top: 15px;
        }}

        @media print {{
            body {{
                background-color: #ffffff;
                padding: 0;
            }}
            .admission-sheet {{
                border: 2px solid #0f3d2e;
                box-shadow: none;
                margin: 0;
                width: 100%;
                min-height: 100vh;
            }}
        }}
    </style>
</head>
<body onload="window.print()">
    {cards_html}
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
    Step 7: Render physical form layout for single application or bulk selected admissions.
    GET /api/admin/applications/<application_id>/print/?format=html (Opens crisp, professional printable form)
    GET /api/admin/applications/print-bulk/?standard=1&medium=Gujarati&status=SELECTED&format=html (Bulk print)
    """
    renderer_classes = [JSONRenderer, BrowsableAPIRenderer, PassthroughHtmlRenderer]
    def get(self, request, application_id=None):
        denied = enforce_admin_token(request)
        if denied:
            return denied

        if application_id:
            from django.db.models import Q
            if str(application_id).isdigit():
                applications = AdmissionApplication.objects.filter(Q(id=int(application_id)) | Q(application_number=str(application_id)))
            else:
                applications = AdmissionApplication.objects.filter(application_number=str(application_id))

            if not applications.exists():
                return Response({
                    "status": "error",
                    "message": f"Admission application {application_id} not found."
                }, status=status.HTTP_404_NOT_FOUND)
        else:
            standard = request.query_params.get("standard")
            medium = request.query_params.get("medium")
            app_status = request.query_params.get("status", "SELECTED")

            applications = AdmissionApplication.objects.all()
            if standard:
                applications = applications.filter(standard__iexact=standard)
            if medium:
                applications = applications.filter(medium__iexact=medium)
            if app_status:
                applications = applications.filter(admission_status__iexact=app_status)
            applications = applications.order_by('merit_rank', 'application_number')

        forms_data = []
        for app in applications:
            forms_data.append({
                "school_header": {
                    "trust_name": "Shreyas Foundation",
                    "school_name": "Vikas Kendra",
                    "location": "Ahmedabad, Gujarat",
                    "academic_session": "2026 - 2027"
                },
                "form_details": {
                    "application_number": app.application_number,
                    "date_of_submission": app.created_at.strftime("%d-%m-%Y") if app.created_at else "-",
                    "vibhag": app.vibhag,
                    "standard": app.standard,
                    "medium": app.medium,
                    "merit_rank": app.merit_rank,
                    "admission_status": app.admission_status
                },
                "student_profile": {
                    "first_name": app.child_first_name,
                    "surname": app.child_surname,
                    "date_of_birth": str(app.date_of_birth) if app.date_of_birth else "-",
                    "gender": getattr(app, 'child_gender', '-'),
                    "blood_group": getattr(app, 'child_blood_group', '-'),
                    "birth_place": f"{app.birth_place_city or ''} {app.birth_place_district or ''}".strip() or "-",
                    "mother_tongue": app.mother_tongue_mother or app.mother_tongue_father or "-",
                    "nationality": app.father_nationality or app.mother_nationality or "Indian",
                    "photo_url": app.photograph_child.url if app.photograph_child else None
                },
                "parent_profile": {
                    "father_name": app.father_name,
                    "father_surname": app.father_surname,
                    "father_qualification": app.father_education,
                    "father_occupation": app.father_occupation,
                    "father_annual_income": app.father_annual_income,
                    "father_mobile": app.father_whatsapp,
                    "father_email": app.father_email,
                    "mother_name": app.mother_name,
                    "mother_surname": app.mother_surname,
                    "mother_qualification": app.mother_education,
                    "mother_occupation": app.mother_occupation,
                    "mother_annual_income": app.mother_annual_income,
                    "mother_mobile": app.mother_whatsapp,
                    "mother_email": app.mother_email,
                    "residential_address": app.father_address_residence or app.mother_address_residence or "-",
                    "office_address": app.father_address_office or app.mother_address_office or "-"
                },
                "assessment_scores": {
                    "part_x": {
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
                    "agreed_to_rules": app.agreed_to_rules,
                    "terms_agreed_date": str(app.terms_agreed_date) if app.terms_agreed_date else "-",
                    "father_signature_url": app.father_signature.url if app.father_signature else None,
                    "mother_signature_url": app.mother_signature.url if app.mother_signature else None,
                    "guardian_signature_url": app.guardian_signature.url if app.guardian_signature else None
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
        assessment_summary_str = f"Part X: {px.get('total_part_x', 0)}/20 | Part Y: {py.get('total_part_y', 0)}/30 | Final Aggregate: {first_sc.get('final_score', 0)}/50"

        # Check if caller specifically requested raw JSON
        # If client explicitly sends Accept: application/json AND does not ask for html, OR query ?format=json
        print_format = (request.query_params.get("format") or request.query_params.get("view") or "").lower().strip()
        accept_header = request.headers.get('Accept', '').lower()

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

        # By default for print requests, return the ready-to-print official school admission form!
        return HttpResponse(html_content, content_type="text/html; charset=utf-8")


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
    1. GET: Handshake verification for Meta / WhatsApp SaaS (hub.challenge)
    2. POST: Receives real-time message updates, delivery statuses, and incoming chats.
             If a forward CRM URL is configured (or in request), it mirrors the payload
             directly into your SaaS CRM.
    Endpoint: /api/whatsapp/webhook/
    """
    authentication_classes = []
    permission_classes = []

    def get(self, request):
        import os
        mode = request.query_params.get("hub.mode")
        token = request.query_params.get("hub.verify_token")
        challenge = request.query_params.get("hub.challenge")

        expected_token = os.getenv("WHATSAPP_WEBHOOK_VERIFY_TOKEN", "shreyas_webhook_token_2026")

        if mode == "subscribe" and token == expected_token:
            logger.info(f"[WHATSAPP WEBHOOK] Subscription verified successfully with token: {token}")
            from django.http import HttpResponse
            return HttpResponse(challenge, content_type="text/plain", status=200)

        logger.warning(f"[WHATSAPP WEBHOOK] Verification failed. Mode: {mode}, Token: {token}")
        return Response({"error": "Verification token mismatch"}, status=status.HTTP_403_FORBIDDEN)

    def post(self, request):
        import os
        payload = request.data
        logger.info(f"[WHATSAPP WEBHOOK RECEIVED] Payload: {json.dumps(payload)[:500]}")

        # Optional: Forward to external WhatsApp SaaS CRM if configured
        crm_forward_url = os.getenv("WHATSAPP_CRM_FORWARD_URL", "").strip()
        if crm_forward_url:
            try:
                import threading
                import requests
                def _forward(url, data, headers):
                    try:
                        requests.post(url, json=data, headers=headers, timeout=10)
                        logger.info(f"[WHATSAPP FORWARD] Successfully forwarded webhook to {url}")
                    except Exception as fe:
                        logger.warning(f"[WHATSAPP FORWARD FAILED] Could not forward to {url}: {fe}")

                forward_headers = {"Content-Type": "application/json"}
                threading.Thread(target=_forward, args=(crm_forward_url, payload, forward_headers), daemon=True).start()
            except Exception as e:
                logger.error(f"[WHATSAPP WEBHOOK FORWARD ERROR] {e}")

        # Return 200 OK immediately as required by Meta / WhatsApp SaaS
        return Response({"status": "received", "success": True}, status=status.HTTP_200_OK)

