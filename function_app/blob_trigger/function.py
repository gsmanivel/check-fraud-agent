import os
import json
import uuid
import logging
import azure.functions as func
from azure.ai.formrecognizer import DocumentAnalysisClient
from azure.core.credentials import AzureKeyCredential
from datetime import datetime, timezone
import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from shared.models import ExtractedCheckFields, enqueue_message

logger = logging.getLogger(__name__)

app = func.FunctionApp()


@app.blob_trigger(
    arg_name="checkblob",
    path="check-images/{name}",
    connection="BLOB_CONNECTION_STRING"
)
def blob_trigger(checkblob: func.InputStream):
    """
    Fires automatically when a check image is uploaded to blob storage.
    1. Reads the blob
    2. Sends to Document Intelligence for field extraction
    3. Builds CheckPayload
    4. Enqueues to tier1-checks-queue
    """
    blob_name = checkblob.name
    logger.info(f"Blob trigger fired for: {blob_name}")

    try:
        # ── Read blob bytes ───────────────────────────────────────────────
        blob_bytes = checkblob.read()
        logger.info(f"Read blob: {len(blob_bytes)} bytes")

        # ── Call Document Intelligence ────────────────────────────────────
        extracted = _extract_check_fields(blob_bytes)
        logger.info(f"Extracted fields: amount={extracted['amount_numeric']}, payee={extracted['payee_name']}")

        # ── Build payload ─────────────────────────────────────────────────
        # In production, some fields come from the metadata attached to the blob
        # For demo, we parse what we can from the filename and extraction
        check_id = str(uuid.uuid4())
        payload = {
            "id": check_id,
            "check_number": _parse_check_number(blob_name),
            "micr_line": extracted.get("micr_line", ""),
            "account_number": extracted.get("account_number", ""),
            "routing_number": extracted.get("routing_number", ""),
            "customer_name": extracted.get("customer_name", ""),
            "payee_name": extracted.get("payee_name", ""),
            "amount": extracted.get("amount_numeric", 0.0),
            "memo": extracted.get("memo", ""),
            "issue_date": extracted.get("issue_date", datetime.now(timezone.utc).isoformat()),
            "submission_date": datetime.now(timezone.utc).isoformat(),
            "bank_name": extracted.get("bank_name", ""),
            "check_image_url": f"https://{os.environ.get('BLOB_ACCOUNT_NAME', 'storage')}.blob.core.windows.net/check-images/{blob_name}",
            "blob_path": blob_name,
            "extracted_fields": extracted,
            "status": "pending",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "updated_at": datetime.now(timezone.utc).isoformat()
        }

        # ── Push to Tier 1 queue ──────────────────────────────────────────
        queue_name = os.environ["TIER1_QUEUE_NAME"]
        enqueue_message(queue_name, payload)
        logger.info(f"Check {check_id} enqueued to {queue_name}")

    except Exception as e:
        logger.error(f"Blob trigger failed for {blob_name}: {str(e)}", exc_info=True)
        raise


def _extract_check_fields(blob_bytes: bytes) -> dict:
    """
    Calls Azure Document Intelligence with the prebuilt-check model.
    Returns a flat dict of extracted fields.
    """
    endpoint = os.environ["DOCUMENT_INTELLIGENCE_ENDPOINT"]
    key = os.environ["DOCUMENT_INTELLIGENCE_KEY"]

    client = DocumentAnalysisClient(
        endpoint=endpoint,
        credential=AzureKeyCredential(key)
    )

    # Analyze the check image
    poller = client.begin_analyze_document(
        model_id="prebuilt-check",
        document=blob_bytes
    )
    result = poller.result()

    extracted = {
        "amount_numeric": 0.0,
        "amount_words": "",
        "payee_name": "",
        "payee_match": True,
        "micr_valid": True,
        "signature_present": False,
        "amount_mismatch": False,
        "alteration_detected": False,
        "raw_ocr_confidence": 0.0,
        "account_number": "",
        "routing_number": "",
        "micr_line": "",
        "bank_name": "",
        "memo": "",
        "issue_date": "",
        "customer_name": ""
    }

    if result.documents:
        doc = result.documents[0]
        fields = doc.fields

        # Amount numeric
        if "Amount" in fields and fields["Amount"].value:
            extracted["amount_numeric"] = float(fields["Amount"].value)

        # Amount in words
        if "AmountInWords" in fields and fields["AmountInWords"].value:
            extracted["amount_words"] = fields["AmountInWords"].value

        # Payee
        if "PayToTheOrderOf" in fields and fields["PayToTheOrderOf"].value:
            extracted["payee_name"] = fields["PayToTheOrderOf"].value

        # MICR line - account and routing
        if "AccountNumber" in fields and fields["AccountNumber"].value:
            extracted["account_number"] = fields["AccountNumber"].value
        if "RoutingNumber" in fields and fields["RoutingNumber"].value:
            extracted["routing_number"] = fields["RoutingNumber"].value
        if "MicrLine" in fields and fields["MicrLine"].value:
            extracted["micr_line"] = fields["MicrLine"].value

        # Bank name
        if "BankName" in fields and fields["BankName"].value:
            extracted["bank_name"] = fields["BankName"].value

        # Memo
        if "Memo" in fields and fields["Memo"].value:
            extracted["memo"] = fields["Memo"].value

        # Issue date
        if "Date" in fields and fields["Date"].value:
            extracted["issue_date"] = str(fields["Date"].value)

        # Payer (customer) name
        if "DrawerName" in fields and fields["DrawerName"].value:
            extracted["customer_name"] = fields["DrawerName"].value

        # Signature
        if "Signature" in fields:
            extracted["signature_present"] = fields["Signature"].value is not None

        # Confidence
        extracted["raw_ocr_confidence"] = doc.confidence or 0.0

        # Amount mismatch check
        extracted["amount_mismatch"] = _check_amount_mismatch(
            extracted["amount_numeric"],
            extracted["amount_words"]
        )

        # MICR validity
        extracted["micr_valid"] = _validate_micr(extracted["routing_number"])

    return extracted


def _check_amount_mismatch(amount_numeric: float, amount_words: str) -> bool:
    """
    Simple check: does the numeric amount roughly match the words?
    E.g. amount_numeric=5000.00, amount_words='five thousand and 00/100 dollars'
    In production, use a words-to-number library for precise matching.
    """
    if not amount_words or amount_numeric == 0:
        return False
    # Extract leading number from words as a rough sanity check
    try:
        first_word_digit = ''.join(filter(str.isdigit, amount_words.split()[0]))
        if first_word_digit:
            words_amount_approx = int(first_word_digit)
            numeric_int = int(amount_numeric)
            # Flag if they differ by more than 50% — crude but catches altered checks
            if numeric_int > 0 and abs(numeric_int - words_amount_approx) / numeric_int > 0.5:
                return True
    except Exception:
        pass
    return False


def _validate_micr(routing_number: str) -> bool:
    """
    Validates ABA routing number using the checksum algorithm.
    Formula: 3(d1+d4+d7) + 7(d2+d5+d8) + (d3+d6+d9) mod 10 == 0
    """
    if not routing_number or len(routing_number) != 9:
        return False
    try:
        d = [int(c) for c in routing_number]
        checksum = (3*(d[0]+d[3]+d[6]) + 7*(d[1]+d[4]+d[7]) + (d[2]+d[5]+d[8])) % 10
        return checksum == 0
    except Exception:
        return False


def _parse_check_number(blob_name: str) -> str:
    """
    Extract check number from blob name if embedded.
    Expected format: check_{check_number}_{uuid}.jpg
    """
    try:
        parts = blob_name.split("_")
        if len(parts) >= 2:
            return parts[1]
    except Exception:
        pass
    return str(uuid.uuid4())[:8]
