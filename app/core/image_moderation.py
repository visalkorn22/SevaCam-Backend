from __future__ import annotations

from typing import Optional, Dict, Any
import httpx

from app.core.config import settings


_GOOGLE_LIKELIHOODS = (
    "UNKNOWN",
    "VERY_UNLIKELY",
    "UNLIKELY",
    "POSSIBLE",
    "LIKELY",
    "VERY_LIKELY",
)
_GOOGLE_LIKELIHOOD_TO_INDEX = {name: idx for idx, name in enumerate(_GOOGLE_LIKELIHOODS)}
_GOOGLE_CATEGORY_FIELDS = {
    "adult": "adult",
    "violence": "violence",
    "racy": "racy",
    "medical": "medical",
    "spoof": "spoof",
    "spoofed": "spoof",
}


def _format_rejection_reason(payload: Dict[str, Any]) -> Optional[str]:
    reason = payload.get("reason")
    if isinstance(reason, str) and reason.strip():
        return reason.strip()
    categories = payload.get("categories")
    if isinstance(categories, list):
        cleaned = [str(item) for item in categories if str(item).strip()]
        if cleaned:
            return ", ".join(cleaned)
    return None


def _parse_csv(value: Optional[str]) -> list[str]:
    if not value:
        return []
    return [part.strip() for part in value.split(",") if part.strip()]


def _normalize_token(value: str) -> str:
    return "".join(ch for ch in value.strip().upper() if ch.isalnum())


def _google_threshold_index(raw: Optional[str]) -> int:
    if not raw:
        return _GOOGLE_LIKELIHOOD_TO_INDEX["LIKELY"]
    normalized = raw.strip().upper()
    return _GOOGLE_LIKELIHOOD_TO_INDEX.get(normalized, _GOOGLE_LIKELIHOOD_TO_INDEX["LIKELY"])


def _google_likelihood_label(value: int) -> str:
    if 0 <= int(value) < len(_GOOGLE_LIKELIHOODS):
        return _GOOGLE_LIKELIHOODS[int(value)]
    return "UNKNOWN"


def _provider_unavailable(reason: str) -> tuple[bool, Optional[str]]:
    if settings.IMAGE_MODERATION_FAIL_CLOSED:
        return False, reason
    return True, None


def _moderate_webhook(
    *,
    content: bytes,
    filename: str,
    content_type: str,
) -> tuple[bool, Optional[str]]:
    if not settings.IMAGE_MODERATION_WEBHOOK_URL:
        return _provider_unavailable("Image moderation service is not configured")

    try:
        with httpx.Client(timeout=settings.IMAGE_MODERATION_TIMEOUT_SECONDS) as client:
            files = {"file": (filename, content, content_type)}
            res = client.post(settings.IMAGE_MODERATION_WEBHOOK_URL, files=files)
    except httpx.RequestError:
        return _provider_unavailable("Image moderation service unavailable")

    if res.status_code >= 400:
        return _provider_unavailable("Image moderation rejected the upload")

    try:
        payload = res.json()
    except ValueError:
        return _provider_unavailable("Invalid moderation response")

    allowed = payload.get("allowed")
    if isinstance(allowed, bool):
        if allowed:
            return True, None
        return False, _format_rejection_reason(payload)

    return _provider_unavailable("Invalid moderation response")


def _moderate_google(
    *,
    content: bytes,
) -> tuple[bool, Optional[str]]:
    try:
        from google.cloud import vision  # type: ignore
    except Exception:
        return _provider_unavailable("Google Vision client is not installed")

    try:
        client = vision.ImageAnnotatorClient()
        image = vision.Image(content=content)
        response = client.safe_search_detection(image=image)
    except Exception:
        return _provider_unavailable("Google Vision moderation failed")

    if getattr(response, "error", None) and getattr(response.error, "message", None):
        return _provider_unavailable("Google Vision moderation failed")

    safe = response.safe_search_annotation
    threshold_index = _google_threshold_index(settings.IMAGE_MODERATION_GOOGLE_THRESHOLD)
    categories = _parse_csv(settings.IMAGE_MODERATION_GOOGLE_BLOCK_CATEGORIES)
    if not categories:
        categories = list(_GOOGLE_CATEGORY_FIELDS.keys())

    hits: list[str] = []
    for category in categories:
        field = _GOOGLE_CATEGORY_FIELDS.get(category.strip().lower())
        if not field:
            continue
        value = getattr(safe, field, None)
        if value is None:
            continue
        score = int(value)
        if score >= threshold_index:
            hits.append(f"{category.lower()}={_google_likelihood_label(score)}")

    if hits:
        return False, "Google SafeSearch flagged: " + ", ".join(hits)
    return True, None


def _moderate_aws(
    *,
    content: bytes,
) -> tuple[bool, Optional[str]]:
    try:
        import boto3  # type: ignore
        from botocore.exceptions import BotoCoreError, ClientError  # type: ignore
    except Exception:
        return _provider_unavailable("AWS Rekognition client is not installed")

    try:
        kwargs: dict[str, Any] = {}
        if settings.IMAGE_MODERATION_AWS_REGION:
            kwargs["region_name"] = settings.IMAGE_MODERATION_AWS_REGION
        client = boto3.client("rekognition", **kwargs)
        response = client.detect_moderation_labels(
            Image={"Bytes": content},
            MinConfidence=float(settings.IMAGE_MODERATION_AWS_MIN_CONFIDENCE),
        )
    except (BotoCoreError, ClientError, Exception):
        return _provider_unavailable("AWS Rekognition moderation failed")

    labels = response.get("ModerationLabels", []) or []
    block_list = {label.lower() for label in _parse_csv(settings.IMAGE_MODERATION_AWS_BLOCK_LABELS)}
    hits: list[str] = []

    for label in labels:
        name = str(label.get("Name") or "").strip()
        parent = str(label.get("ParentName") or "").strip()
        confidence = label.get("Confidence")
        compare = name.lower()
        compare_parent = parent.lower()
        if not block_list:
            hits.append(name)
            continue
        if compare in block_list or compare_parent in block_list:
            if isinstance(confidence, (int, float)):
                hits.append(f"{name}({confidence:.1f}%)")
            else:
                hits.append(name)

    if hits:
        return False, "AWS Rekognition flagged: " + ", ".join(hits)
    return True, None


def _moderate_azure(
    *,
    content: bytes,
) -> tuple[bool, Optional[str]]:
    endpoint = settings.AZURE_CONTENT_SAFETY_ENDPOINT
    key = settings.AZURE_CONTENT_SAFETY_KEY
    if not endpoint or not key:
        return _provider_unavailable("Azure Content Safety is not configured")

    try:
        from azure.ai.contentsafety import ContentSafetyClient  # type: ignore
        from azure.ai.contentsafety.models import AnalyzeImageOptions, ImageData  # type: ignore
        from azure.core.credentials import AzureKeyCredential  # type: ignore
        from azure.core.exceptions import HttpResponseError  # type: ignore
    except Exception:
        return _provider_unavailable("Azure Content Safety client is not installed")

    try:
        client = ContentSafetyClient(endpoint, AzureKeyCredential(key))
        request = AnalyzeImageOptions(image=ImageData(content=content))
        response = client.analyze_image(request)
    except HttpResponseError:
        return _provider_unavailable("Azure Content Safety moderation failed")
    except Exception:
        return _provider_unavailable("Azure Content Safety moderation failed")

    threshold = int(settings.IMAGE_MODERATION_AZURE_SEVERITY_THRESHOLD)
    raw_categories = _parse_csv(settings.IMAGE_MODERATION_AZURE_CATEGORIES)
    normalized_filter: set[str] = set()
    for category in raw_categories:
        normalized_filter.add(_normalize_token(category))

    hits: list[str] = []
    for item in response.categories_analysis or []:
        category_obj = getattr(item, "category", None)
        if category_obj is None:
            continue
        category_name = getattr(category_obj, "name", None) or str(category_obj)
        if "." in category_name:
            category_name = category_name.split(".")[-1]
        normalized = _normalize_token(category_name)
        if normalized_filter and normalized not in normalized_filter:
            continue
        severity = int(getattr(item, "severity", 0) or 0)
        if severity >= threshold:
            hits.append(f"{category_name}={severity}")

    if hits:
        return False, "Azure Content Safety flagged: " + ", ".join(hits)
    return True, None


def moderate_image(
    *,
    content: bytes,
    filename: str,
    content_type: str,
) -> tuple[bool, Optional[str]]:
    """
    Returns (allowed, reason).

    The webhook should return JSON like:
    {"allowed": true/false, "reason": "...", "categories": ["..."]}
    """
    if not settings.IMAGE_MODERATION_ENABLED:
        return True, None
    provider = settings.IMAGE_MODERATION_PROVIDER.strip().lower()

    if provider == "webhook":
        return _moderate_webhook(
            content=content,
            filename=filename,
            content_type=content_type,
        )
    if provider == "google":
        return _moderate_google(content=content)
    if provider == "aws":
        return _moderate_aws(content=content)
    if provider == "azure":
        return _moderate_azure(content=content)

    return _provider_unavailable("Image moderation provider is not configured")
