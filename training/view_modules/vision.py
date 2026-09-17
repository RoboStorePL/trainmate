import json
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from typing import Any

from django.contrib.auth.decorators import login_required
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.cache import cache
from django.core.files.base import ContentFile
from django.db.models import QuerySet
from django.http import FileResponse, Http404, HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.views import generic
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from ..models import VisionAnalysis, VisionDevice


class VisionDashboard(LoginRequiredMixin, generic.ListView):
    model = VisionAnalysis
    template_name = "vision_dashboard.html"
    context_object_name = "analyses"

    def get_queryset(self) -> QuerySet[VisionAnalysis]:
        return VisionAnalysis.objects.filter(user=self.request.user).select_related(
            "device",
        )

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        devices = VisionDevice.objects.filter(
            owner=self.request.user, is_active=True,
        )
        context["devices"] = devices
        context["primary_device"] = devices.first()
        context["progress_analyses"] = list(self.get_queryset().exclude(
            pose_score__isnull=True,
        ).order_by("-captured_at")[:20])[::-1]
        return context


def _device_from_request(request: HttpRequest) -> VisionDevice | None:
    """Authenticate a Pi with a bearer token; never store the raw token."""
    authorization = request.headers.get("Authorization", "")
    scheme, _, raw_token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not raw_token:
        return None
    token_hash = sha256(raw_token.encode()).hexdigest()
    device = VisionDevice.objects.filter(
        token_hash=token_hash, is_active=True,
    ).select_related("owner").first()
    if device is None or not device.check_token(raw_token):
        return None
    return device


@csrf_exempt
@require_POST
def ingest_vision_analysis(request: HttpRequest) -> JsonResponse:
    """Receive a compact pose result from an authorised Raspberry Pi device."""
    if len(request.body) > 65_536:
        return JsonResponse({"detail": "Payload is too large."}, status=413)
    device = _device_from_request(request)
    if device is None:
        return JsonResponse({"detail": "Invalid device token."}, status=401)
    if device.owner is None:
        return JsonResponse({"detail": "Assign a device owner first."}, status=409)
    try:
        payload = json.loads(request.body)
    except (TypeError, json.JSONDecodeError):
        return JsonResponse({"detail": "Expected a JSON body."}, status=400)
    if not isinstance(payload, dict):
        return JsonResponse({"detail": "Expected a JSON object."}, status=400)

    activity = payload.get("activity")
    valid_activities = {choice for choice, _ in VisionAnalysis.Activity.choices}
    if activity not in valid_activities:
        return JsonResponse({"detail": "Unsupported activity."}, status=400)
    feedback = payload.get("feedback", "")
    landmarks = payload.get("landmarks", {})
    score = payload.get("pose_score")
    if not isinstance(feedback, str) or len(feedback) > 500:
        return JsonResponse({"detail": "Invalid feedback."}, status=400)
    if not isinstance(landmarks, dict):
        return JsonResponse({"detail": "Landmarks must be an object."}, status=400)
    try:
        pose_score = None if score is None else Decimal(str(score)).quantize(
            Decimal("0.01"),
        )
    except (InvalidOperation, ValueError):
        return JsonResponse({"detail": "Invalid pose score."}, status=400)
    if pose_score is not None and not Decimal("0") <= pose_score <= Decimal("100"):
        return JsonResponse({"detail": "Pose score must be 0–100."}, status=400)

    analysis = VisionAnalysis.objects.create(
        user=device.owner, device=device, activity=activity,
        pose_score=pose_score, feedback=feedback, landmarks=landmarks,
    )
    device.last_seen_at = timezone.now()
    device.save(update_fields=["last_seen_at"])
    return JsonResponse({"id": analysis.pk, "status": "created"}, status=201)


@csrf_exempt
@require_POST
def upload_vision_snapshot(request: HttpRequest, pk: int) -> JsonResponse:
    """Attach one optional, user-approved progress frame to an analysis."""
    device = _device_from_request(request)
    if device is None:
        return JsonResponse({"detail": "Invalid device token."}, status=401)
    analysis = get_object_or_404(VisionAnalysis, pk=pk, device=device)
    image = request.body
    if not image:
        return JsonResponse({"detail": "Expected a JPEG snapshot."}, status=400)
    if len(image) > 2 * 1024 * 1024:
        return JsonResponse({"detail": "Snapshot must be 2 MB or smaller."}, status=413)
    if request.content_type.split(";", 1)[0].lower() != "image/jpeg":
        return JsonResponse({"detail": "Snapshot must be JPEG."}, status=400)
    if not _is_jpeg(image):
        return JsonResponse({"detail": "Snapshot is not a valid JPEG."}, status=400)
    if analysis.snapshot:
        return JsonResponse({"detail": "A snapshot is already attached."}, status=409)
    analysis.snapshot.save(
        f"analysis-{analysis.pk}.jpg", ContentFile(image), save=True,
    )
    return JsonResponse({"status": "stored"}, status=201)


def _live_frame_cache_key(device_id: int) -> str:
    return f"trainmate:vision:live-frame:{device_id}"


def _is_jpeg(image: bytes) -> bool:
    """Reject arbitrary uploads while keeping Pi-side validation inexpensive."""
    return len(image) >= 4 and image.startswith(b"\xff\xd8") and image.endswith(b"\xff\xd9")


@csrf_exempt
@require_POST
def ingest_vision_live_frame(request: HttpRequest) -> HttpResponse:
    """Store a short-lived JPEG frame for an authenticated owner's dashboard."""
    device = _device_from_request(request)
    if device is None:
        return JsonResponse({"detail": "Invalid device token."}, status=401)
    frame = request.body
    if not frame:
        return JsonResponse({"detail": "Expected a JPEG frame."}, status=400)
    if len(frame) > 1 * 1024 * 1024:
        return JsonResponse({"detail": "Live frame must be 1 MB or smaller."}, status=413)
    if request.content_type.split(";", 1)[0].lower() != "image/jpeg":
        return JsonResponse({"detail": "Live frame must be JPEG."}, status=400)
    if not _is_jpeg(frame):
        return JsonResponse({"detail": "Live frame is not a valid JPEG."}, status=400)
    cache.set(_live_frame_cache_key(device.pk), frame, timeout=15)
    device.last_seen_at = timezone.now()
    device.save(update_fields=["last_seen_at"])
    return HttpResponse(status=204)


@login_required
@require_GET
def vision_live_frame(request: HttpRequest, pk: int) -> HttpResponse:
    """Expose the latest Pi frame only to the device owner."""
    device = get_object_or_404(
        VisionDevice, pk=pk, owner=request.user, is_active=True,
    )
    frame = cache.get(_live_frame_cache_key(device.pk))
    if frame is None:
        return HttpResponse(status=204)
    response = HttpResponse(frame, content_type="image/jpeg")
    response["Cache-Control"] = "no-store, max-age=0"
    return response


@login_required
@require_GET
def vision_snapshot(request: HttpRequest, pk: int) -> FileResponse:
    """Serve a saved progress frame only to the analysis owner."""
    analysis = get_object_or_404(VisionAnalysis, pk=pk, user=request.user)
    if not analysis.snapshot:
        raise Http404("No snapshot is available for this analysis.")
    response = FileResponse(analysis.snapshot.open("rb"), content_type="image/jpeg")
    response["Cache-Control"] = "private, no-store, max-age=0"
    return response



