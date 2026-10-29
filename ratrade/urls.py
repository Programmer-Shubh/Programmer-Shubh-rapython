from django.contrib import admin
from django.urls import path, include
from django.views.generic import TemplateView
from ratrade_app import views

urlpatterns = [
    path('admin/', admin.site.urls),
    path('', views.index_view, name='index'),
    path('api/', include('ratrade_app.api_urls')),
]
