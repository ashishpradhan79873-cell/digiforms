from django.contrib import admin

from .models import Application, ChatMessage, UserDocument, UserProfile, Vacancy

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
