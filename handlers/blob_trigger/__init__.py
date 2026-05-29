import os, uuid, logging
import azure.functions as func
from datetime import datetime, timezone
from azure.ai.documentintelligence import DocumentIntelligenceClient
from azure.ai.documentintelligence.models import AnalyzeDocumentRequest, DocumentSignatureType
from azure.core.credentials import AzureKeyCredential

from handlers.shared.cosmos import upsert_check
from handlers.shared.servicebus import enqueue_message
from handlers.shared.utils import validate_micr

DOC_INTELLIGENCE_MODEL = os.environ.get("DOCUMENT_INTELLIGENCE_MODEL", "prebuilt-check.us")

logger = logging.getLogger(__name__)
bp = func.Blueprint()


@bp.blob_trigger(
    arg_name="checkblob",
    path="check-images/{name}",
    connection="AzureWebJobsStorage",
    source="EventGrid",
)
def blob_trigger(checkblob: func.InputStream):
    blob_name = checkblob.name
    logger.info(f"Blob trigger fired: {blob_name}")
    try:
        blob_bytes = checkblob.read()
        if len(blob_bytes) > 5_000_000:
            logger.error(f"Blob {blob_name} exceeds 5MB limit ({len(blob_bytes)} bytes); skipping")
            return
        extracted  = _extract_check_fields(blob_bytes)
        check_id   = str(uuid.uuid4())
        payload = {
            "id":               check_id,
            "check_number":     _parse_check_number(blob_name),
            "micr_line":        extracted.get("micr_line", ""),
            "account_number":   extracted.get("account_number", ""),
            "routing_number":   extracted.get("routing_number", ""),
            "customer_name":    extracted.get("customer_name", ""),
            "payee_name":       extracted.get("payee_name", ""),
            "amount":           extracted.get("amount_numeric", 0.0),
            "memo":             extracted.get("memo", ""),
            "issue_date":       extracted.get("issue_date", datetime.now(timezone.utc).isoformat()),
            "submission_date":  datetime.now(timezone.utc).isoformat(),
            "bank_name":        extracted.get("bank_name", ""),
            "check_image_url":  f"https://{os.environ.get('BLOB_ACCOUNT_NAME','storage')}.blob.core.windows.net/check-images/{blob_name}",
            "blob_path":        blob_name,
            "extracted_fields": extracted,
            "status":           "pending",
            "created_at":       datetime.now(timezone.utc).isoformat(),
            "updated_at":       datetime.now(timezone.utc).isoformat()
        }
        enqueue_message(os.environ["TIER1_QUEUE_NAME"], payload)
        logger.info(f"Check {check_id} enqueued to Tier 1")
    except Exception as e:
        logger.error(f"Blob trigger failed for {blob_name}: {e}", exc_info=True)
        raise


def _extract_check_fields(blob_bytes: bytes) -> dict:
    endpoint = os.environ["DOCUMENT_INTELLIGENCE_ENDPOINT"]
    key      = os.environ["DOCUMENT_INTELLIGENCE_KEY"]
    client   = DocumentIntelligenceClient(endpoint=endpoint, credential=AzureKeyCredential(key))
    poller   = client.begin_analyze_document(
        DOC_INTELLIGENCE_MODEL,
        AnalyzeDocumentRequest(bytes_source=blob_bytes),
    )
    result   = poller.result()
    extracted = {
        "amount_numeric": 0.0, "amount_words": "", "payee_name": "",
        "payee_match": True, "micr_valid": True, "signature_present": False,
        "amount_mismatch": False, "alteration_detected": False,
        "raw_ocr_confidence": 0.0, "account_number": "", "routing_number": "",
        "micr_line": "", "bank_name": "", "memo": "", "issue_date": "", "customer_name": ""
    }
    if result.documents:
        doc    = result.documents[0]
        fields = doc.fields or {}

        def get_string(name: str) -> str:
            f = fields.get(name)
            if not f:
                return ""
            return f.value_string or f.content or ""

        amount_field = fields.get("Amount")
        if amount_field and amount_field.value_currency and amount_field.value_currency.amount is not None:
            extracted["amount_numeric"] = float(amount_field.value_currency.amount)

        extracted["amount_words"]   = get_string("AmountInWords")
        extracted["payee_name"]     = get_string("PayToTheOrderOf")
        extracted["account_number"] = get_string("AccountNumber")
        extracted["routing_number"] = get_string("RoutingNumber")
        extracted["micr_line"]      = get_string("MicrLine")
        extracted["bank_name"]      = get_string("BankName")
        extracted["memo"]           = get_string("Memo")
        extracted["customer_name"]  = get_string("DrawerName")

        date_field = fields.get("Date")
        if date_field and date_field.value_date:
            extracted["issue_date"] = date_field.value_date.isoformat()

        sig_field = fields.get("Signature")
        extracted["signature_present"] = bool(
            sig_field and sig_field.value_signature == DocumentSignatureType.SIGNED
        )

        extracted["raw_ocr_confidence"] = doc.confidence or 0.0
        extracted["micr_valid"]         = validate_micr(extracted["routing_number"])
        extracted["amount_mismatch"]    = _check_amount_mismatch(
            extracted["amount_numeric"], extracted["amount_words"]
        )
    return extracted


def _check_amount_mismatch(amount_numeric: float, amount_words: str) -> bool:
    if not amount_words or amount_numeric == 0:
        return False
    try:
        first_word_digit = ''.join(filter(str.isdigit, amount_words.split()[0]))
        if first_word_digit:
            words_int   = int(first_word_digit)
            numeric_int = int(amount_numeric)
            if numeric_int > 0 and abs(numeric_int - words_int) / numeric_int > 0.5:
                return True
    except Exception:
        pass
    return False


def _parse_check_number(blob_name: str) -> str:
    try:
        parts = blob_name.split("_")
        if len(parts) >= 2:
            return parts[1]
    except Exception:
        pass
    return str(uuid.uuid4())[:8]
