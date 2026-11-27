import json
import logging
from django.http import JsonResponse
from django.utils.deprecation import MiddlewareMixin
from .models import AuditLog, AdminUser

logger = logging.getLogger(__name__)

class AuditLogMiddleware(MiddlewareMixin):
    """
    Middleware that:
    1. Strictly blocks SURVEY-scoped JWT tokens from accessing any non-survey /api/* endpoint (403 Forbidden).
    2. Captures /api/ calls and creates an AuditLog record.
    """
    def process_request(self, request):
        if request.path.startswith('/api/') and not request.path.startswith('/api/survey/'):
            auth_header = request.headers.get('Authorization') or request.META.get('HTTP_AUTHORIZATION') or ""
            query_token = (request.GET.get('token') or request.GET.get('access_token') or "").strip()
            candidate_token = auth_header if auth_header.lower().startswith('bearer ') else query_token
            if candidate_token:
                from .jwt_auth import get_survey_user_from_token
                if get_survey_user_from_token(candidate_token):
                    return JsonResponse(
                        {
                            "status": "error",
                            "message": "Access forbidden. SURVEY role tokens cannot access admission or admin endpoints.",
                        },
                        status=403,
                    )
        return None

    def process_response(self, request, response):
        # Only log /api/ requests
        if not request.path.startswith('/api/'):
            return response

        # Do not recursively log GET calls to the audit log itself or high-frequency autosave/webhook pings
        if (request.path == '/api/admin/audit-logs/' and request.method == 'GET') or request.path in ['/api/whatsapp/webhook/', '/api/admission-form/autosave/']:
            return response

        # Extract client IP
        x_forwarded_for = request.META.get('HTTP_X_FORWARDED_FOR')
        if x_forwarded_for:
            ip = x_forwarded_for.split(',')[0].strip()
        else:
            ip = request.META.get('REMOTE_ADDR', '')

        # Identify User from JWT Authorization Bearer or Headers
        auth_header = request.headers.get('Authorization') or request.META.get('HTTP_AUTHORIZATION') or ""
        actor_username = request.headers.get('X-Admin-User') or ""
        actor_email = request.headers.get('X-Admin-Email') or ""
        actor_role = "GUEST"

        if auth_header and auth_header.lower().startswith('bearer '):
            from .jwt_auth import get_admin_user_from_token, get_survey_user_from_token
            survey_user = get_survey_user_from_token(auth_header)
            if survey_user:
                actor_username = survey_user["username"]
                actor_email = survey_user["email"]
                actor_role = survey_user["role"]
            else:
                jwt_user = get_admin_user_from_token(auth_header)
                if jwt_user:
                    actor_username = jwt_user.username
                    actor_email = jwt_user.email
                    actor_role = jwt_user.role

        if not actor_email and (actor_username or request.headers.get('X-Admin-Email')):
            user = AdminUser.objects.filter(
                email__iexact=actor_email or actor_username
            ).first() or AdminUser.objects.filter(
                username__iexact=actor_username or actor_email
            ).first()

            if user:
                actor_username = user.username
                actor_email = user.email
                actor_role = user.role
        elif not actor_email and hasattr(request, 'user') and request.user.is_authenticated:
            actor_username = request.user.username
            actor_email = getattr(request.user, 'email', '')
            actor_role = "DJANGO_ADMIN" if request.user.is_staff else "USER"

        # Capture request body safely (masking sensitive fields like pin/password)
        body_str = ""
        extracted_sub_id = None
        extracted_child_name = ""

        if request.method in ['POST', 'PATCH', 'PUT']:
            try:
                if request.content_type == 'application/json' and request.body:
                    body_json = json.loads(request.body.decode('utf-8', errors='ignore'))
                    if isinstance(body_json, dict):
                        extracted_sub_id = body_json.get('submission_id') or body_json.get('id')
                        extracted_child_name = body_json.get('child_name') or body_json.get('child_first_name') or ""
                        masked_json = body_json.copy()
                        for secret_key in ['pin', 'new_pin', 'password', 'otp']:
                            if secret_key in masked_json:
                                masked_json[secret_key] = "******"
                        body_str = json.dumps(masked_json)[:2000]
                    else:
                        body_str = str(body_json)[:2000]
                elif request.POST:
                    post_dict = request.POST.dict()
                    extracted_sub_id = post_dict.get('submission_id') or post_dict.get('id')
                    extracted_child_name = post_dict.get('child_name') or post_dict.get('child_first_name') or ""
                    for secret_key in ['pin', 'new_pin', 'password', 'otp']:
                        if secret_key in post_dict:
                            post_dict[secret_key] = "******"
                    body_str = json.dumps(post_dict)[:2000]
            except Exception:
                body_str = "[Binary or Non-JSON payload]"

        # Also check query params if not found in body
        if not extracted_sub_id:
            extracted_sub_id = request.GET.get('submission_id') or request.GET.get('id')

        # If this is a Guest request (e.g. Parent submitting quiz or admission form)
        if actor_role == "GUEST":
            if extracted_sub_id and str(extracted_sub_id).isdigit():
                from .models import QuizSubmission
                sub = QuizSubmission.objects.filter(id=int(extracted_sub_id)).first()
                if sub:
                    c_name = sub.child_name or extracted_child_name
                    actor_username = f"Applicant #{sub.id} ({c_name})" if c_name else f"Applicant #{sub.id}"
                    actor_email = sub.email or sub.whatsapp_number or ""
                    actor_role = f"GUEST (ID: {sub.id})"
                else:
                    actor_username = f"Applicant #{extracted_sub_id}"
                    actor_role = f"GUEST (ID: {extracted_sub_id})"
            elif extracted_sub_id:
                actor_username = f"Applicant #{extracted_sub_id}"
                actor_role = f"GUEST"
            elif response.status_code in [200, 201] and getattr(response, 'data', None) and isinstance(response.data, dict):
                # If newly created submission ID in response
                resp_sub_id = response.data.get('submission_id') or response.data.get('id')
                if resp_sub_id:
                    actor_username = f"Applicant #{resp_sub_id}"
                    actor_role = f"GUEST (ID: {resp_sub_id})"

        query_params = request.META.get('QUERY_STRING', '')
        user_agent = request.META.get('HTTP_USER_AGENT', '')[:250]

        try:
            AuditLog.objects.create(
                actor_username=actor_username or "Anonymous",
                actor_email=actor_email,
                actor_role=actor_role,
                method=request.method,
                endpoint=request.path,
                query_params=query_params[:500],
                request_body=body_str,
                response_status=response.status_code,
                ip_address=ip[:45],
                user_agent=user_agent
            )
        except Exception as e:
            logger.error(f"Failed to record audit log: {e}")

        return response

