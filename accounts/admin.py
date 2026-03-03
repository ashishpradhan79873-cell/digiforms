from django.contrib import admin

from .models import (
    Application,
    ChatMessage,
    PaymentSetting,
    PortalNews,
    UserDocument,
    UserProfile,
    Vacancy,
    WalletTransaction,
)

@admin.register(UserProfile)
class UserProfileAdmin(admin.ModelAdmin):
    list_display = ("id", "user", "full_name", "mobile", "chat_enabled")
    list_filter = ("chat_enabled", "gender", "category")
    search_fields = ("user__username", "full_name", "mobile")


admin.site.register(UserDocument)
admin.site.register(Application)
admin.site.register(ChatMessage)


@admin.register(Vacancy)
class VacancyAdmin(admin.ModelAdmin):
    list_display = ("title", "category", "organization", "last_date", "display_order", "is_active")
    list_filter = ("category", "is_active", "organization")
    search_fields = ("title", "organization")
    ordering = ("category", "display_order", "last_date")


@admin.register(PortalNews)
class PortalNewsAdmin(admin.ModelAdmin):
    list_display = ("title", "news_type", "target_portal", "event_date", "display_order", "is_active")
    list_filter = ("news_type", "target_portal", "is_active")
    search_fields = ("title", "details", "external_link")
    ordering = ("display_order", "-event_date", "-updated_at")


@admin.register(WalletTransaction)
class WalletTransactionAdmin(admin.ModelAdmin):
    list_display = ("id", "profile", "tx_type", "amount", "note", "created_at")
    list_filter = ("tx_type",)
    search_fields = ("profile__user__username", "profile__full_name", "note")
    ordering = ("-created_at",)


@admin.register(PaymentSetting)
class PaymentSettingAdmin(admin.ModelAdmin):
    list_display = ("upi_id", "payee_name", "amount", "is_active", "updated_at")
