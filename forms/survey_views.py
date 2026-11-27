from django.utils import timezone
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from .jwt_auth import (
    SURVEY_ACCOUNT,
    generate_survey_jwt_token,
    get_admin_user_from_token,
    get_survey_user_from_token,
)
from .models import SurveyInvite, SurveyResponse
from .whatsapp import normalize_phone_number, send_whatsapp_survey_link_message


def _extract_bearer_token(request) -> str:
    auth_header = request.headers.get('Authorization') or request.META.get('HTTP_AUTHORIZATION') or ""
    if auth_header and auth_header.lower().startswith('bearer '):
        return auth_header.strip()
    query_token = (
        request.query_params.get('token')
        or request.query_params.get('access_token')
        or request.GET.get('token')
        or ""
    ).strip()
    return query_token


def enforce_survey_token(request):
    """
    Strictly enforces that the request carries a valid SURVEY-scoped JWT token.
    - Missing or invalid token -> 401 Unauthorized
    - Valid non-SURVEY token (ADMIN / STAFF / ACADEMICIAN) -> 403 Forbidden
    - Valid SURVEY token -> None (allowed)
    """
    token = _extract_bearer_token(request)
    if not token:
        return Response(
            {
                "status": "error",
                "message": "Authentication required. Please provide a valid SURVEY Bearer token.",
            },
            status=status.HTTP_401_UNAUTHORIZED,
        )

    if get_survey_user_from_token(token):
        return None

    # Explicitly return 403 Forbidden for ADMIN, STAFF, or ACADEMICIAN tokens
    admin_user = get_admin_user_from_token(token)
    if admin_user is not None:
        return Response(
            {
                "status": "error",
                "message": "Access forbidden. Only the SURVEY role is permitted to access this endpoint.",
            },
            status=status.HTTP_403_FORBIDDEN,
        )

    return Response(
        {
            "status": "error",
            "message": "Invalid or unauthorized token.",
        },
        status=status.HTTP_401_UNAUTHORIZED,
    )


def _serialize_invite(invite: SurveyInvite) -> dict:
    return {
        "id": invite.id,
        "name": invite.name,
        "phone": invite.phone,
        "sent_at": invite.sent_at.isoformat().replace("+00:00", "Z") if invite.sent_at else None,
        "last_sent_at": invite.last_sent_at.isoformat().replace("+00:00", "Z") if invite.last_sent_at else None,
        "send_count": invite.send_count,
        "whatsapp_status": invite.whatsapp_status,
    }


class SurveyLoginView(APIView):
    """
    POST /api/survey/login/ (Public)
    Validates the single code-only SURVEY account (Romil / pranjalvejani19@gmail.com, PIN 2026)
    and returns a SURVEY-scoped JWT token.
    """
    def post(self, request):
        identifier = str(
            request.data.get("identifier")
            or request.data.get("username")
            or request.data.get("email")
            or ""
        ).strip()
        pin = str(request.data.get("pin") or "").strip()

        valid_identifiers = {
            SURVEY_ACCOUNT["username"].lower(),
            SURVEY_ACCOUNT["email"].lower(),
        }

        if identifier.lower() not in valid_identifiers or pin != SURVEY_ACCOUNT["pin"]:
            return Response(
                {
                    "status": "error",
                    "message": "Invalid survey credentials.",
                },
                status=status.HTTP_401_UNAUTHORIZED,
            )

        token = generate_survey_jwt_token()
        return Response(
            {
                "token": token,
                "user": {
                    "username": SURVEY_ACCOUNT["username"],
                    "email": SURVEY_ACCOUNT["email"],
                    "role": SURVEY_ACCOUNT["role"],
                },
            },
            status=status.HTTP_200_OK,
        )


class SurveyResponsesView(APIView):
    """
    POST /api/survey/responses/ (Public — No Auth)
    GET  /api/survey/responses/ (Protected — SURVEY role ONLY)
    """
    def post(self, request):
        language = str(request.data.get("language") or "en").strip().lower()
        if language not in ("en", "gu"):
            return Response(
                {"status": "error", "message": "Invalid language. Must be 'en' or 'gu'."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        answers = request.data.get("answers")
        if not isinstance(answers, dict) or not answers:
            return Response(
                {"status": "error", "message": "Field 'answers' must be a non-empty JSON object."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        department_value = str(
            request.data.get("department_value") or answers.get("Q01") or ""
        ).strip()
        if not department_value:
            return Response(
                {"status": "error", "message": "Field 'department_value' is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        SurveyResponse.objects.create(
            language=language,
            department_value=department_value,
            answers=answers,
        )

        return Response(
            {"status": "success", "message": "Survey submitted successfully"},
            status=status.HTTP_201_CREATED,
        )

    def get(self, request):
        denied = enforce_survey_token(request)
        if denied:
            return denied

        responses = SurveyResponse.objects.all().order_by('id')
        results = [
            {
                "id": r.id,
                "language": r.language,
                "department_value": r.department_value,
                "answers": r.answers,
            }
            for r in responses
        ]
        return Response(
            {
                "count": len(results),
                "results": results,
            },
            status=status.HTTP_200_OK,
        )


class SurveyInvitesView(APIView):
    """
    POST /api/survey/invites/ (Protected — SURVEY role ONLY)
    GET  /api/survey/invites/ (Protected — SURVEY role ONLY)
    """
    def get(self, request):
        denied = enforce_survey_token(request)
        if denied:
            return denied

        invites = SurveyInvite.objects.all().order_by('-last_sent_at', '-id')
        results = [_serialize_invite(inv) for inv in invites]
        return Response(
            {
                "count": len(results),
                "results": results,
            },
            status=status.HTTP_200_OK,
        )

    def post(self, request):
        denied = enforce_survey_token(request)
        if denied:
            return denied

        name = str(request.data.get("name") or "").strip()
        raw_phone = str(request.data.get("phone") or "").strip()

        if not name:
            return Response(
                {"status": "error", "message": "Field 'name' is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if not raw_phone:
            return Response(
                {"status": "error", "message": "Field 'phone' is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        clean_phone = normalize_phone_number(raw_phone)
        if len(clean_phone) < 10:
            return Response(
                {"status": "error", "message": "Please provide a valid phone number."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        wa_result = send_whatsapp_survey_link_message(clean_phone, recipient_name=name)
        wa_status = "SENT" if wa_result.get("success") else "FAILED"

        invite = SurveyInvite.objects.create(
            name=name,
            phone=clean_phone,
            send_count=1,
            whatsapp_status=wa_status,
        )

        return Response(_serialize_invite(invite), status=status.HTTP_201_CREATED)


class SurveyInviteDetailView(APIView):
    """
    DELETE /api/survey/invites/<id>/ (Protected — SURVEY role ONLY)
    """
    def delete(self, request, invite_id: int):
        denied = enforce_survey_token(request)
        if denied:
            return denied

        invite = SurveyInvite.objects.filter(id=invite_id).first()
        if not invite:
            return Response(
                {"status": "error", "message": "Survey invite not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        invite.delete()
        return Response(
            {
                "status": "success",
                "message": "Survey invite deleted successfully",
                "id": invite_id,
            },
            status=status.HTTP_200_OK,
        )


class SurveyInviteResendView(APIView):
    """
    POST /api/survey/invites/<id>/resend/ (Protected — SURVEY role ONLY)
    """
    def post(self, request, invite_id: int):
        denied = enforce_survey_token(request)
        if denied:
            return denied

        invite = SurveyInvite.objects.filter(id=invite_id).first()
        if not invite:
            return Response(
                {"status": "error", "message": "Survey invite not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        wa_result = send_whatsapp_survey_link_message(invite.phone, recipient_name=invite.name)
        invite.send_count += 1
        invite.last_sent_at = timezone.now()
        invite.whatsapp_status = "SENT" if wa_result.get("success") else "FAILED"
        invite.save(update_fields=["send_count", "last_sent_at", "whatsapp_status"])

        return Response(_serialize_invite(invite), status=status.HTTP_200_OK)
