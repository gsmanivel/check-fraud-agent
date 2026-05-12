import os, uuid, logging
import azure.functions as func
import azure.durable_functions as df
from datetime import datetime, timezone
from azure.ai.formrecognizer import DocumentAnalysisClient
from azure.core.credentials import AzureKeyCredential

from handlers.shared.cosmos import upsert_check
from handlers.shared.servicebus import enqueue_message
from handlers.shared.utils import validate_micr

logger = logging.getLogger(__name__)
bp = df.Blueprint()


@bp.blob_trigger(
    arg_name="checkblob",
    path="check-images/{name}",
    connection="BLOB_CONNECTION_STRING"
)
def blob_trigger(checkblob: func.InputStream):
    blob_name = checkblob.name
    logger.info(f"Blob trigger fired: {blob_name}")
    try:
        blob_bytes = checkblob.read()
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
    client   = DocumentAnalysisClient(endpoint=endpoint, credential=AzureKeyCredential(key))
    poller   = client.begin_analyze_document(model_id="prebuilt-check", document=blob_bytes)
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
        fields = doc.fields

        def fval(name):
            return fields[name].value if name in fields and fields[name].value else None

        extracted["amount_numeric"]     = float(fval("Amount") or 0)
        extracted["amount_words"]       = fval("AmountInWords") or ""
        extracted["payee_name"]         = fval("PayToTheOrderOf") or ""
        extracted["account_number"]     = fval("AccountNumber") or ""
        extracted["routing_number"]     = fval("RoutingNumber") or ""
        extracted["micr_line"]          = fval("MicrLine") or ""
        extracted["bank_name"]          = fval("BankName") or ""
        extracted["memo"]               = fval("Memo") or ""
        extracted["issue_date"]         = str(fval("Date") or "")
        extracted["customer_name"]      = fval("DrawerName") or ""
        extracted["signature_present"]  = fval("Signature") is not None
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
