from django.urls import path
from . import views

urlpatterns = [
    path('login/',       views.login_view,       name='login'),
    path('register/',    views.register_view,    name='register'),
    path('logout/',      views.logout_view,      name='logout'),
    path('role-select/', views.role_select_view, name='role_select'),
    path('master-data-option/', views.master_data_option_view, name='master_data_option'),
    path('document-converter/', views.document_converter_view, name='document_converter'),
    path('document-converter/process/', views.document_converter_process_view, name='document_converter_process'),
    path('master-data/', views.master_data_view, name='master_data'),
    path('master-data/personal/', views.master_data_personal_view, name='master_data_personal'),
    path('master-data/address/', views.master_data_address_view, name='master_data_address'),
    path('master-data/academic/', views.master_data_academic_view, name='master_data_academic'),
    path('master-data/college/', views.master_data_college_view, name='master_data_college'),
    path('master-data/bank/', views.master_data_bank_view, name='master_data_bank'),
    path('master-data/documents/', views.master_data_documents_view, name='master_data_documents'),
]
