from rest_framework import serializers
from .models import QuizSubmission, AdmissionApplication

class SubmitQuizSerializer(serializers.Serializer):
    parent_name = serializers.CharField(required=False, allow_blank=True, default="")
    child_name = serializers.CharField(required=False, allow_blank=True, default="")
    vibhag = serializers.ChoiceField(
        choices=QuizSubmission.VIBHAG_CHOICES,
        default='Shishu',
        help_text="Specify Vibhag: Shishu / Arun / Kesari / Kishore"
    )
    language = serializers.CharField(
        required=False,
        default='en',
        allow_blank=True,
        help_text="Language: 'en' or 'gu'"
    )
    whatsapp_number = serializers.CharField(
        required=True,
        help_text="10-digit WhatsApp number to receive admission form link"
    )
    email = serializers.EmailField(
        required=False,
        allow_blank=True,
        default="",
        help_text="User email address for communication and form pre-fill"
    )
    frontend_base_url = serializers.CharField(
        required=False,
        default="",
        allow_blank=True,
        help_text="Optional Frontend domain, e.g., https://your-frontend.com"
    )
    answers = serializers.DictField(
        child=serializers.CharField(),
        help_text="Dictionary of question IDs and selected option, e.g. {'1': 'B', '2': 'C', ...}"
    )

    def validate_answers(self, value):
        if not value:
            raise serializers.ValidationError("Answers cannot be empty.")
        return value

    def validate_whatsapp_number(self, value):
        clean_num = "".join(filter(str.isdigit, str(value)))
        if len(clean_num) < 10:
            raise serializers.ValidationError("Please provide a valid 10-digit WhatsApp number.")
        return clean_num


class AdmissionApplicationSerializer(serializers.ModelSerializer):
    submission_id = serializers.IntegerField(write_only=True, required=False)
    child_first_name = serializers.CharField(required=False, allow_blank=True)
    vibhag = serializers.CharField(required=False, allow_blank=True)

    class Meta:
        model = AdmissionApplication
        fields = '__all__'
        read_only_fields = ('application_number', 'created_at')

    def validate(self, attrs):
        submission_id = attrs.get('submission_id')
        submission = None
        if submission_id:
            try:
                submission = QuizSubmission.objects.get(id=submission_id)
                if not submission.is_eligible:
                    raise serializers.ValidationError({
                        "submission_id": "You are not eligible to fill this form because quiz score is less than 4."
                    })
                attrs['submission'] = submission
            except QuizSubmission.DoesNotExist:
                raise serializers.ValidationError({"submission_id": "Invalid submission ID."})
        else:
            raise serializers.ValidationError({"submission_id": "submission_id is required."})

        # Terms & Conditions validation
        if not attrs.get('agreed_to_rules'):
            raise serializers.ValidationError({
                "agreed_to_rules": "You must agree to the Shreyas Rules and Regulations to submit the application."
            })

        # Signature validation (At least one parent/guardian must sign)
        if not (attrs.get('mother_signature') or attrs.get('father_signature') or attrs.get('guardian_signature')):
            raise serializers.ValidationError({
                "signatures": "At least one signature (Mother, Father, or Guardian) is required."
            })

        # Pre-fill automatic population & protection for users
        request = self.context.get('request')
        is_admin = bool(request and request.user and request.user.is_staff)
        
        if submission:
            # Fixed Vibhag copied from Quiz Submission
            attrs['vibhag'] = submission.vibhag

            if not is_admin:
                # If not admin, strictly overwrite with the verified submission data
                if submission.child_name:
                    attrs['child_first_name'] = submission.child_name
                
                if submission.parent_name:
                    # If father_name or mother_name is not provided, autofill with parent_name
                    if not attrs.get('father_name') and not attrs.get('mother_name'):
                        attrs['father_name'] = submission.parent_name
                    elif not attrs.get('father_name'):
                        attrs['father_name'] = submission.parent_name

                if submission.whatsapp_number:
                    attrs['father_whatsapp'] = submission.whatsapp_number
                    attrs['mother_whatsapp'] = attrs.get('mother_whatsapp') or submission.whatsapp_number
                
                if submission.email:
                    attrs['father_email'] = attrs.get('father_email') or submission.email
                    attrs['mother_email'] = attrs.get('mother_email') or submission.email
            else:
                # Even if admin, if they left them blank, autofill from submission
                if not attrs.get('child_first_name') and submission.child_name:
                    attrs['child_first_name'] = submission.child_name
                if not attrs.get('father_whatsapp') and submission.whatsapp_number:
                    attrs['father_whatsapp'] = submission.whatsapp_number
                if not attrs.get('father_email') and submission.email:
                    attrs['father_email'] = submission.email

        return attrs
