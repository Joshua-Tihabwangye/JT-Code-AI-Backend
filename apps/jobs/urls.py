from django.urls import include, path
from rest_framework.routers import DefaultRouter

from apps.jobs.views import (
    CallbackViewSet,
    JobStatusCallbackView,
    JobStepViewSet,
    JobViewSet,
    ResearchJobsView,
    WorkflowRunViewSet,
)

router = DefaultRouter()
router.register(r'jobs', JobViewSet, basename='job')
router.register(r'job-steps', JobStepViewSet, basename='job-step')
router.register(r'workflow-runs', WorkflowRunViewSet, basename='workflow-run')
router.register(r'callbacks', CallbackViewSet, basename='callback')

urlpatterns = [
    path('', include(router.urls)),
    path('research/jobs/', ResearchJobsView.as_view(), name='research-jobs'),
    path('jobs/<uuid:job_id>/status/', JobStatusCallbackView.as_view(), name='job-status-callback'),
]