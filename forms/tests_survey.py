from unittest.mock import patch
from django.test import TestCase
from rest_framework.test import APIClient

from .jwt_auth import generate_jwt_token, generate_survey_jwt_token
from .models import AdminUser, SurveyInvite, SurveyResponse


class SurveyAPITestCase(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.survey_token = generate_survey_jwt_token()

        self.admin_user = AdminUser.objects.create(
            username="testadmin",
            full_name="Test Admin",
            email="admin@example.com",
            role="ADMIN",
            is_active=True,
        )
        self.admin_user.set_pin("1234")
        self.admin_user.save()
        self.admin_token = generate_jwt_token(self.admin_user)

        self.staff_user = AdminUser.objects.create(
            username="teststaff",
            full_name="Test Staff",
            email="staff@example.com",
            role="STAFF",
            is_active=True,
        )
        self.staff_user.set_pin("1234")
        self.staff_user.save()
        self.staff_token = generate_jwt_token(self.staff_user)

    def test_survey_login_success_and_failure(self):
        # Login with username
        res = self.client.post("/api/survey/login/", {"identifier": "Romil", "pin": "2026"}, format="json")
        self.assertEqual(res.status_code, 200)
        self.assertIn("token", res.data)
        self.assertEqual(res.data["user"]["role"], "SURVEY")
        self.assertEqual(res.data["user"]["username"], "Romil")
        self.assertEqual(res.data["user"]["email"], "pranjalvejani19@gmail.com")

        # Login with email
        res2 = self.client.post(
            "/api/survey/login/",
            {"identifier": "pranjalvejani19@gmail.com", "pin": "2026"},
            format="json",
        )
        self.assertEqual(res2.status_code, 200)
        self.assertEqual(res2.data["user"]["role"], "SURVEY")

        # Invalid PIN
        res_bad = self.client.post("/api/survey/login/", {"identifier": "Romil", "pin": "0000"}, format="json")
        self.assertEqual(res_bad.status_code, 401)

    def test_survey_responses_public_post_and_protected_get(self):
        payload = {
            "language": "en",
            "department_value": "principal_vikas_kendra_lataben",
            "answers": {
                "Q01": "principal_vikas_kendra_lataben",
                "Q02": 4,
                "Q03": 5,
                "Q26": "rarely",
                "Q27": "often",
                "Q28": "sometimes",
            },
        }
        # Public POST (no auth)
        res = self.client.post("/api/survey/responses/", payload, format="json")
        self.assertEqual(res.status_code, 201)
        self.assertEqual(res.data["status"], "success")
        self.assertEqual(SurveyResponse.objects.count(), 1)

        # GET without token -> 401
        res_unauth = self.client.get("/api/survey/responses/")
        self.assertEqual(res_unauth.status_code, 401)

        # GET with ADMIN token -> 403
        res_admin = self.client.get(
            "/api/survey/responses/",
            HTTP_AUTHORIZATION=f"Bearer {self.admin_token}",
        )
        self.assertEqual(res_admin.status_code, 403)

        # GET with STAFF token -> 403
        res_staff = self.client.get(
            "/api/survey/responses/",
            HTTP_AUTHORIZATION=f"Bearer {self.staff_token}",
        )
        self.assertEqual(res_staff.status_code, 403)

        # GET with SURVEY token -> 200
        res_ok = self.client.get(
            "/api/survey/responses/",
            HTTP_AUTHORIZATION=f"Bearer {self.survey_token}",
        )
        self.assertEqual(res_ok.status_code, 200)
        self.assertEqual(res_ok.data["count"], 1)
        self.assertEqual(res_ok.data["results"][0]["department_value"], "principal_vikas_kendra_lataben")

    @patch("forms.survey_views.send_whatsapp_survey_link_message")
    def test_survey_invites_crud_and_resend(self, mock_wa):
        mock_wa.return_value = {"success": True, "recipient": "919876543210"}

        # POST invite with ADMIN token -> 403
        res_admin = self.client.post(
            "/api/survey/invites/",
            {"name": "Rameshbhai Patel", "phone": "9876543210"},
            format="json",
            HTTP_AUTHORIZATION=f"Bearer {self.admin_token}",
        )
        self.assertEqual(res_admin.status_code, 403)

        # POST invite with SURVEY token -> 201
        res = self.client.post(
            "/api/survey/invites/",
            {"name": "Rameshbhai Patel", "phone": "9876543210"},
            format="json",
            HTTP_AUTHORIZATION=f"Bearer {self.survey_token}",
        )
        self.assertEqual(res.status_code, 201)
        self.assertEqual(res.data["phone"], "919876543210")
        self.assertEqual(res.data["send_count"], 1)
        self.assertEqual(res.data["whatsapp_status"], "SENT")
        invite_id = res.data["id"]

        # GET invites -> 200
        res_list = self.client.get(
            "/api/survey/invites/",
            HTTP_AUTHORIZATION=f"Bearer {self.survey_token}",
        )
        self.assertEqual(res_list.status_code, 200)
        self.assertEqual(res_list.data["count"], 1)

        # POST resend -> 200, send_count == 2
        res_resend = self.client.post(
            f"/api/survey/invites/{invite_id}/resend/",
            HTTP_AUTHORIZATION=f"Bearer {self.survey_token}",
        )
        self.assertEqual(res_resend.status_code, 200)
        self.assertEqual(res_resend.data["send_count"], 2)

        # DELETE invite -> 200
        res_del = self.client.delete(
            f"/api/survey/invites/{invite_id}/",
            HTTP_AUTHORIZATION=f"Bearer {self.survey_token}",
        )
        self.assertEqual(res_del.status_code, 200)
        self.assertEqual(SurveyInvite.objects.count(), 0)

    def test_survey_token_rejected_on_admission_and_admin_endpoints(self):
        headers = {"HTTP_AUTHORIZATION": f"Bearer {self.survey_token}"}
        for endpoint in ["/api/admin/users/", "/api/enquiries/", "/api/admission-form/"]:
            res = self.client.get(endpoint, **headers)
            self.assertEqual(res.status_code, 403, f"Expected 403 on {endpoint}, got {res.status_code}")

    def test_admin_users_never_lists_survey_role(self):
        res = self.client.get(
            "/api/admin/users/",
            HTTP_AUTHORIZATION=f"Bearer {self.admin_token}",
        )
        self.assertEqual(res.status_code, 200)
        roles = [u["role"] for u in res.data.get("users", [])]
        self.assertNotIn("SURVEY", roles)
