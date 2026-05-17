"""
Azure AI Content Safety — Prompt Shields wrapper.

Feature-flagged: if CONTENT_SAFETY_ENDPOINT is unset, every call returns
{safe: True, skipped: True} so this module is safe to land before the
resource is provisioned.

When enabled, this calls the preview Prompt Shields endpoint
(/contentsafety/text:shieldPrompt) via the stable SDK's send_request
transport, so we get auth/retry/tracing without depending on a beta package.

Migration path: once Prompt Shields lands in the stable SDK, swap to
client.shield_prompt(...) and delete the raw HttpRequest.
"""
import os
import logging
from functools import lru_cache

from azure.ai.contentsafety import ContentSafetyClient
from azure.core.credentials import AzureKeyCredential
from azure.core.exceptions import HttpResponseError
from azure.core.rest import HttpRequest

logger = logging.getLogger(__name__)

SHIELD_API_VERSION = os.environ.get("CONTENT_SAFETY_API_VERSION", "2024-09-01")
FAIL_MODE = os.environ.get("CONTENT_SAFETY_FAIL_MODE", "open").lower()


@lru_cache(maxsize=1)
def _client() -> ContentSafetyClient | None:
    endpoint = os.environ.get("CONTENT_SAFETY_ENDPOINT")
    key      = os.environ.get("CONTENT_SAFETY_KEY")
    if not endpoint or not key:
        return None
    return ContentSafetyClient(endpoint=endpoint, credential=AzureKeyCredential(key))


def check_prompt_safety(user_prompt: str, documents: list[str] | None = None) -> dict:
    """
    Returns {"safe": bool, "skipped": bool, "reason": str | None, "details": dict | None}.

    safe=True means no attack detected (or check was skipped/failed-open).
    safe=False means Prompt Shields flagged either the user prompt or one of
    the documents as containing a jailbreak / indirect injection attempt.
    """
    client = _client()
    if client is None:
        return {"safe": True, "skipped": True, "reason": None, "details": None}

    body: dict = {"userPrompt": user_prompt}
    if documents:
        body["documents"] = documents

    try:
        request = HttpRequest(
            method="POST",
            url=f"/contentsafety/text:shieldPrompt?api-version={SHIELD_API_VERSION}",
            json=body,
        )
        response = client.send_request(request)
        response.raise_for_status()
        result = response.json()
    except HttpResponseError as exc:
        logger.warning("Content Safety call failed: %s. Fail mode: %s", exc, FAIL_MODE)
        return {
            "safe": FAIL_MODE != "closed",
            "skipped": False,
            "reason": f"shield_api_error: {exc.status_code}",
            "details": None,
        }
    except Exception as exc:
        logger.warning("Content Safety unreachable: %s. Fail mode: %s", exc, FAIL_MODE)
        return {
            "safe": FAIL_MODE != "closed",
            "skipped": False,
            "reason": "shield_api_unreachable",
            "details": None,
        }

    user_attack = (result.get("userPromptAnalysis") or {}).get("attackDetected", False)
    doc_attacks = [
        i for i, d in enumerate(result.get("documentsAnalysis") or [])
        if d.get("attackDetected")
    ]
    safe = not user_attack and not doc_attacks

    return {
        "safe": safe,
        "skipped": False,
        "reason": (
            "user_prompt_attack" if user_attack
            else f"document_attack:{doc_attacks}" if doc_attacks
            else None
        ),
        "details": result,
    }
