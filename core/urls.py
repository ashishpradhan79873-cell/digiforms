from django.urls import path
from . import views

urlpatterns = [
    path('', views.home_router, name='home'),
    path('government-vacancies/', views.dashboard, name='dashboard'),
    path("news/", views.news_hub, name="news_hub"),
    path("news/<int:news_id>/", views.news_detail, name="news_detail"),
    path("send-to-admin/", views.confirm_send_to_admin, name="confirm_send_to_admin"),
    path("send-to-admin/profile/", views.apply_profile_preview, name="apply_profile_preview"),
    path("chat/", views.user_chat, name="user_chat"),
    path("chat/clear/", views.user_chat_clear_thread, name="user_chat_clear_thread"),
    path("chat/delete-selected/", views.user_chat_delete_selected, name="user_chat_delete_selected"),
    path("chat/message/<int:message_id>/delete/", views.user_chat_delete_message, name="user_chat_delete_message"),
    path("chat/message/<int:message_id>/attachment/download/", views.chat_attachment_download, name="chat_attachment_download"),
    path("student-services/", views.student_services_dashboard, name="student_services_dashboard"),
    path(
        "student-services/apply/<int:vacancy_id>/",
        views.apply_student_service,
        name="apply_student_service",
    ),
    path("admin-panel/enter/", views.enter_admin_panel, name="enter_admin_panel"),
    path("apply/<int:vacancy_id>/", views.apply_vacancy, name="apply_vacancy"),
    path("application/<int:application_id>/cancel/", views.cancel_own_application, name="cancel_own_application"),

    path("admin-panel/applicants/", views.admin_applicants, name="admin_applicants"),
    path(
        "admin-panel/applicants/<int:application_id>/update/",
        views.admin_update_application,
        name="admin_update_application",
    ),
    path(
        "admin-panel/applicants/<int:application_id>/remove/",
        views.admin_remove_application,
        name="admin_remove_application",
    ),
    path(
        "admin-panel/applicants/history/<int:history_id>/remove/",
        views.admin_remove_history_entry,
        name="admin_remove_history_entry",
    ),
    path(
        "admin-panel/applicants/history/clear/",
        views.admin_clear_history,
        name="admin_clear_history",
    ),
    path(
        "admin-panel/applicants/<int:application_id>/detail-json/",
        views.admin_applicant_detail_json,
        name="admin_applicant_detail_json",
    ),
    path("admin-panel/export/csv/", views.admin_export_csv, name="admin_export_csv"),
    path(
        "admin-panel/applicants/<int:application_id>/export/csv/",
        views.admin_export_single_csv,
        name="admin_export_single_csv",
    ),
    path(
        "admin-panel/applicants/<int:application_id>/pdf/",
        views.admin_applicant_pdf,
        name="admin_applicant_pdf",
    ),
    path(
        "admin-panel/applicants/<int:application_id>/extension-file/",
        views.admin_applicant_extension_file,
        name="admin_applicant_extension_file",
    ),
    path(
        "admin-panel/applicants/<int:application_id>/documents/download-all/",
        views.admin_download_all_documents,
        name="admin_download_all_documents",
    ),
    path(
        "admin-panel/options/<str:category>/",
        views.admin_option_control,
        name="admin_option_control",
    ),
    path(
        "admin-panel/master-data/",
        views.admin_master_data_control,
        name="admin_master_data_control",
    ),
    path("admin-panel/documents/", views.admin_documents, name="admin_documents"),
    path("admin-panel/chat/", views.admin_chat, name="admin_chat"),
    path("admin-panel/news/", views.admin_news, name="admin_news"),
    path("admin-panel/payment/", views.admin_payment, name="admin_payment"),
    path("admin-panel/chat/send/", views.admin_chat_send, name="admin_chat_send"),
    path(
        "admin-panel/chat/<int:profile_id>/toggle/",
        views.admin_chat_toggle,
        name="admin_chat_toggle",
    ),
    path(
        "admin-panel/chat/message/<int:message_id>/delete/",
        views.admin_chat_delete_message,
        name="admin_chat_delete_message",
    ),
    path(
        "admin-panel/chat/delete-selected/",
        views.admin_chat_delete_selected,
        name="admin_chat_delete_selected",
    ),
    path(
        "admin-panel/chat/<int:profile_id>/clear/",
        views.admin_chat_clear_thread,
        name="admin_chat_clear_thread",
    ),
    path("admin-panel/options/save/", views.admin_save_vacancy, name="admin_save_vacancy"),
    path(
        "admin-panel/options/<int:vacancy_id>/delete/",
        views.admin_delete_vacancy,
        name="admin_delete_vacancy",
    ),
    path(
        "admin-panel/options/<int:vacancy_id>/update/",
        views.admin_update_vacancy,
        name="admin_update_vacancy",
    ),
    path(
        "admin-panel/applicants/<int:application_id>/documents/demo/<str:doc_type>/",
        views.admin_demo_document_download,
        name="admin_demo_document_download",
    ),
]
