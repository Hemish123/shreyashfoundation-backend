from unittest.mock import patch

from django.test import TestCase
from rest_framework.test import APIClient

from .models import QuizSubmission


class AdmissionFormOTPFlowTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.submission = QuizSubmission.objects.create(
            standard="Nursery",
            medium="Gujarati",
            parent_name="Rajesh Patel",
            child_name="Aarav",
            surname="Patel",
            whatsapp_number="9876543210",
            email="rajesh@example.com",
            vibhag="Shishu",
            payment_status="RECEIVED",
            is_eligible=True,
            qa_status="PASSED",
            score=6,
        )

    @patch("forms.whatsapp.send_whatsapp_otp_template")
    def test_link_open_sends_otp_and_gates_form_until_verified(self, mock_send_otp):
        mock_send_otp.return_value = {
            "success": True,
            "template": "otp",
            "recipient": "919876543210",
            "message_id": "wamid.test123",
        }

        # 1. Opening link triggers OTP via WhatsApp template and blocks form access
        res = self.client.get(f"/api/admission-form/check-eligibility/?submission_id={self.submission.id}")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data["status"], "otp_required")
        self.assertTrue(res.data["otp_required"])
        self.assertFalse(res.data["can_fill_form"])
        self.assertTrue(res.data["otp_sent"])
        self.assertEqual(res.data["masked_phone"], "********3210")
        self.assertNotIn("prefilled_data", res.data)
        mock_send_otp.assert_called_once()

        self.submission.refresh_from_db()
        otp_code = self.submission.form_otp_code
        self.assertIsNotNone(otp_code)
        self.assertEqual(len(otp_code), 6)

        # 2. Autosave and Form Submit are blocked with 403 before OTP verification
        autosave_res = self.client.get(f"/api/admission-form/autosave/?submission_id={self.submission.id}")
        self.assertEqual(autosave_res.status_code, 403)
        self.assertEqual(autosave_res.data["error_code"], "OTP_VERIFICATION_REQUIRED")

        submit_res = self.client.post("/api/admission-form/", {"submission_id": self.submission.id}, format="json")
        self.assertEqual(submit_res.status_code, 403)
        self.assertEqual(submit_res.data["error_code"], "OTP_VERIFICATION_REQUIRED")

        # 3. Wrong OTP fails
        bad_verify = self.client.post(
            "/api/admission-form/verify-otp/",
            {"submission_id": self.submission.id, "otp": "000000" if otp_code != "000000" else "999999"},
            format="json",
        )
        self.assertEqual(bad_verify.status_code, 400)
        self.assertEqual(bad_verify.data["error_code"], "INVALID_OTP")

        # 4. Correct OTP verifies and unlocks form
        verify_res = self.client.post(
            "/api/admission-form/verify-otp/",
            {"submission_id": self.submission.id, "otp": otp_code},
            format="json",
        )
        self.assertEqual(verify_res.status_code, 200)
        self.assertTrue(verify_res.data["otp_verified"])
        self.assertTrue(verify_res.data["can_fill_form"])
        self.assertIn("form_access_token", verify_res.data)
        self.assertIn("prefilled_data", verify_res.data)

        # 5. Subsequent check-eligibility & autosave succeed without re-sending OTP
        res2 = self.client.get(f"/api/admission-form/check-eligibility/?submission_id={self.submission.id}")
        self.assertEqual(res2.status_code, 200)
        self.assertTrue(res2.data["can_fill_form"])
        self.assertFalse(res2.data["otp_required"])
        mock_send_otp.assert_called_once()

        autosave_ok = self.client.get(f"/api/admission-form/autosave/?submission_id={self.submission.id}")
        self.assertEqual(autosave_ok.status_code, 200)

    @patch("forms.whatsapp.send_whatsapp_otp_template")
    def test_explicit_send_otp_endpoint(self, mock_send_otp):
        mock_send_otp.return_value = {
            "success": True,
            "template": "otp",
            "recipient": "919876543210",
            "message_id": "wamid.resend123",
        }
        res = self.client.post(
            "/api/admission-form/send-otp/",
            {"submission_id": self.submission.id},
            format="json",
        )
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.data["otp_sent"])
        mock_send_otp.assert_called_once()

    @patch("forms.whatsapp.send_whatsapp_otp_template")
    def test_otp_sent_on_link_open_even_for_pending_payment_submission(self, mock_send_otp):
        pending_sub = QuizSubmission.objects.create(
            standard="Nursery",
            medium="Gujarati",
            parent_name="Test Parent",
            child_name="Test Child",
            surname="Shah",
            whatsapp_number="9988776655",
            payment_status="PENDING",
        )
        mock_send_otp.return_value = {
            "success": True,
            "template": "otp",
            "recipient": "919988776655",
            "message_id": "wamid.pending123",
        }
        res = self.client.get(f"/api/admission-form/check-eligibility/?submission_id={pending_sub.id}")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data["status"], "otp_required")
        self.assertTrue(res.data["otp_sent"])
        self.assertEqual(res.data["masked_phone"], "********6655")
        mock_send_otp.assert_called_once()
