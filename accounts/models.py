from django.db import models
from django.contrib.auth.models import User


class UserProfile(models.Model):
    user          = models.OneToOneField(User, on_delete=models.CASCADE, related_name='profile')
    full_name     = models.CharField(max_length=150, blank=True)
    father_name   = models.CharField(max_length=150, blank=True)
    mother_name   = models.CharField(max_length=150, blank=True)
    dob           = models.DateField(null=True, blank=True)
    GENDER_CHOICES = [('M','Male'),('F','Female'),('O','Other')]
    gender        = models.CharField(max_length=1, choices=GENDER_CHOICES, blank=True)
    CATEGORY_CHOICES = [('General','General'),('OBC','OBC'),('SC','SC'),('ST','ST')]
    category      = models.CharField(max_length=20, choices=CATEGORY_CHOICES, blank=True)
    mobile        = models.CharField(max_length=15, blank=True)
    email         = models.EmailField(blank=True)

    # Address details
    present_state = models.CharField(max_length=100, blank=True)
    present_district = models.CharField(max_length=100, blank=True)
    present_city = models.CharField(max_length=100, blank=True)
    present_pincode = models.CharField(max_length=10, blank=True)
    present_address = models.TextField(blank=True)
    permanent_same_as_present = models.BooleanField(default=False)
    permanent_state = models.CharField(max_length=100, blank=True)
    permanent_district = models.CharField(max_length=100, blank=True)
    permanent_pincode = models.CharField(max_length=10, blank=True)
    permanent_full_address = models.TextField(blank=True)

    permanent_address = models.TextField(blank=True)
    district      = models.CharField(max_length=100, blank=True)
    state         = models.CharField(max_length=100, default='Chhattisgarh', blank=True)
    pincode       = models.CharField(max_length=10, blank=True)
    aadhar        = models.CharField(max_length=20, blank=True)
    samagra_id    = models.CharField(max_length=20, blank=True)
    caste_cert_no = models.CharField(max_length=50, blank=True)
    income_cert_no= models.CharField(max_length=50, blank=True)
    tenth_board   = models.CharField(max_length=100, blank=True)
    tenth_roll_number = models.CharField(max_length=50, blank=True)
    tenth_percentage = models.CharField(max_length=20, blank=True)
    tenth_result  = models.CharField(max_length=50, blank=True)
    twelfth_board = models.CharField(max_length=100, blank=True)
    twelfth_roll_number = models.CharField(max_length=50, blank=True)
    twelfth_percentage = models.CharField(max_length=20, blank=True)
    twelfth_result= models.CharField(max_length=50, blank=True)
    graduation    = models.CharField(max_length=100, blank=True)
    university    = models.CharField(max_length=150, blank=True)

    # College details
    college_name = models.CharField(max_length=200, blank=True)
    university_name = models.CharField(max_length=200, blank=True)
    course = models.CharField(max_length=150, blank=True)
    year_semester = models.CharField(max_length=100, blank=True)
    enrollment_number = models.CharField(max_length=100, blank=True)

    # Bank details
    account_holder_name = models.CharField(max_length=150, blank=True)
    bank_name = models.CharField(max_length=150, blank=True)
    account_number = models.CharField(max_length=50, blank=True)
    ifsc_code = models.CharField(max_length=20, blank=True)
    branch_name = models.CharField(max_length=150, blank=True)
    aadhaar_linked = models.CharField(max_length=3, blank=True, choices=[('yes', 'Yes'), ('no', 'No')])
    personal_extra_rows = models.JSONField(default=list, blank=True)
    address_extra_rows = models.JSONField(default=list, blank=True)
    academic_extra_rows = models.JSONField(default=list, blank=True)
    college_extra_rows = models.JSONField(default=list, blank=True)
    bank_extra_rows = models.JSONField(default=list, blank=True)

    photo         = models.ImageField(upload_to='profile_photos/', null=True, blank=True)
    signature     = models.ImageField(upload_to='profile_signatures/', null=True, blank=True)
    chat_enabled  = models.BooleanField(default=False)

    def __str__(self):
        return f"{self.full_name} ({self.user.username})"

    @property
    def completion_percent(self):
        fields = ['full_name','father_name','mother_name','dob','gender',
                  'category','mobile','email','permanent_address','district',
                  'aadhar','tenth_board','tenth_result','twelfth_board','twelfth_result']
        filled = sum(1 for f in fields if getattr(self, f))
        return int((filled / len(fields)) * 100)


class UserDocument(models.Model):
    profile = models.ForeignKey(UserProfile, on_delete=models.CASCADE, related_name='documents')
    title   = models.CharField(max_length=120, blank=True)
    file    = models.FileField(upload_to='profile_documents/')
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.title or self.file.name


class Vacancy(models.Model):
    CATEGORY_GOVERNMENT = "government"
    CATEGORY_STUDENT = "student"
    CATEGORY_CHOICES = [
        (CATEGORY_GOVERNMENT, "Government"),
        (CATEGORY_STUDENT, "Student"),
    ]

    category = models.CharField(max_length=20, choices=CATEGORY_CHOICES, default=CATEGORY_GOVERNMENT)
    title = models.CharField(max_length=200)
    organization = models.CharField(max_length=200)
    last_date = models.DateField()
    icon_name = models.CharField(max_length=50, blank=True, default="description")
    image = models.ImageField(upload_to="vacancy_images/", null=True, blank=True)
    display_order = models.PositiveIntegerField(default=0)
    is_active = models.BooleanField(default=True)
    required_documents = models.JSONField(default=list, blank=True)
    required_profile_fields = models.JSONField(default=list, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["display_order", "last_date", "id"]

    def __str__(self):
        return f"{self.get_category_display()} - {self.title}"


class Application(models.Model):
    STATUS_PENDING = "pending"
    STATUS_APPROVED = "approved"
    STATUS_UNDER_REVIEW = "under_review"
    STATUS_REJECTED = "rejected"
    STATUS_CANCELLED = "cancelled"

    STATUS_CHOICES = [
        (STATUS_PENDING, "Pending"),
        (STATUS_APPROVED, "Approved"),
        (STATUS_UNDER_REVIEW, "Under Review"),
        (STATUS_REJECTED, "Rejected"),
        (STATUS_CANCELLED, "Cancelled"),
    ]

    profile = models.ForeignKey(UserProfile, on_delete=models.CASCADE, related_name="applications")
    vacancy = models.ForeignKey(Vacancy, on_delete=models.CASCADE, related_name="applications")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_PENDING)
    remarks = models.TextField(blank=True)
    applied_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    cancelled_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-applied_at"]
        constraints = [
            models.UniqueConstraint(fields=["profile", "vacancy"], name="unique_application_per_profile_vacancy"),
        ]

    def __str__(self):
        return f"{self.profile.full_name or self.profile.user.username} - {self.vacancy.title}"


class ChatMessage(models.Model):
    profile = models.ForeignKey(UserProfile, on_delete=models.CASCADE, related_name="chat_messages")
    from_admin = models.BooleanField(default=False)
    message = models.TextField(blank=True)
    attachment = models.FileField(upload_to="chat_attachments/", null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at", "id"]

    def __str__(self):
        who = "Admin" if self.from_admin else (self.profile.full_name or self.profile.user.username)
        return f"{who} - {self.created_at:%Y-%m-%d %H:%M}"


class DocumentRule(models.Model):
    KIND_ANY = "any"
    KIND_IMAGE = "image"
    KIND_PDF = "pdf"
    KIND_CHOICES = [
        (KIND_ANY, "Any"),
        (KIND_IMAGE, "Image"),
        (KIND_PDF, "PDF"),
    ]

    name = models.CharField(max_length=140, unique=True)
    min_kb = models.PositiveIntegerField(default=1)
    max_kb = models.PositiveIntegerField(default=500)
    exact_kb = models.PositiveIntegerField(null=True, blank=True)
    exact_width = models.PositiveIntegerField(null=True, blank=True)
    exact_height = models.PositiveIntegerField(null=True, blank=True)
    kind = models.CharField(max_length=10, choices=KIND_CHOICES, default=KIND_ANY)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return f"{self.name} ({self.min_kb}-{self.max_kb} KB)"
