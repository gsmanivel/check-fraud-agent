"""
Canned data for the Tier-2 agent evaluation harness.

The eval needs reproducible inputs to tools (customer_lookup, velocity_check,
fraud_pattern_search). Hitting live Cosmos would couple eval results to whatever
state the DB happens to be in. Instead, we monkeypatch the tool implementations
to return canned data from this module — the LLM still hits real Azure OpenAI,
so agent reasoning is measured genuinely, but tool outputs are deterministic.
"""

CUSTOMERS: dict[str, dict] = {
    "4645120421": {
        "account_number": "4645120421",
        "customer_name": "Steven Wilson",
        "account_status": "active",
        "kyc_verified": True,
        "synthetic_identity_risk": False,
        "opened_date": "2021-05-30T01:08:43Z",
        "avg_monthly_balance": 1105.61,
        "linked_accounts": [],
        "flags": [],
    },
    "1478163327": {
        "account_number": "1478163327",
        "customer_name": "David Mitchell",
        "account_status": "active",
        "kyc_verified": True,
        "synthetic_identity_risk": False,
        "opened_date": "2022-01-23T01:08:43Z",
        "avg_monthly_balance": 2557.35,
        "linked_accounts": [],
        "flags": [],
    },
    "7857221324": {
        "account_number": "7857221324",
        "customer_name": "Ashley Hill",
        "account_status": "active",
        "kyc_verified": True,
        "synthetic_identity_risk": False,
        "opened_date": "2026-04-01T01:08:43Z",
        "avg_monthly_balance": 13481.40,
        "linked_accounts": [],
        "flags": [],
    },
    "9190197115": {
        "account_number": "9190197115",
        "customer_name": "Patricia Adams",
        "account_status": "active",
        "kyc_verified": False,
        "synthetic_identity_risk": True,
        "opened_date": "2026-01-27T01:08:43Z",
        "avg_monthly_balance": 3463.95,
        "linked_accounts": ["6237376063", "6204041308", "2503068227", "5070585433"],
        "flags": ["synthetic_identity_suspected"],
    },
    "7803990970": {
        "account_number": "7803990970",
        "customer_name": "Linda Allen",
        "account_status": "active",
        "kyc_verified": True,
        "synthetic_identity_risk": False,
        "opened_date": "2026-02-27T01:08:43Z",
        "avg_monthly_balance": 34085.42,
        "linked_accounts": [],
        "flags": [],
    },
    "6237376063": {
        "account_number": "6237376063",
        "customer_name": "Anthony Perez",
        "account_status": "active",
        "kyc_verified": False,
        "synthetic_identity_risk": True,
        "opened_date": "2025-11-01T01:08:43Z",
        "avg_monthly_balance": 49055.38,
        "linked_accounts": ["9190197115", "6204041308", "2503068227", "5070585433"],
        "flags": ["synthetic_identity_suspected"],
    },
}


VELOCITY_DATA: dict[str, dict] = {
    "_default": {"count": 0, "total": 0.0, "near_ctr": 0, "recent_txns": []},
    "7857221324": {
        "count": 4,
        "total": 36800.0,
        "near_ctr": 4,
        "recent_txns": [
            {"amount": 9200, "submission_date": "2026-05-17T10:00:00Z"},
            {"amount": 9500, "submission_date": "2026-05-16T14:00:00Z"},
            {"amount": 8900, "submission_date": "2026-05-15T09:00:00Z"},
            {"amount": 9200, "submission_date": "2026-05-14T16:00:00Z"},
        ],
    },
    "9190197115": {
        "count": 2,
        "total": 6500.0,
        "near_ctr": 0,
        "recent_txns": [
            {"amount": 3500, "submission_date": "2026-05-16T10:00:00Z"},
            {"amount": 3000, "submission_date": "2026-05-15T14:00:00Z"},
        ],
    },
}


FRAUD_CASES: list[dict] = [
    {
        "id": "fc-structuring",
        "pattern": "structuring",
        "description": "Multiple checks just under $10,000 within a short window to evade CTR reporting",
        "agent_signals": [
            "amount_near_ctr_threshold",
            "high_velocity_5plus_24h",
            "elevated_velocity_3plus_24h",
            "cumulative_amount_near_ctr_24h",
        ],
    },
    {
        "id": "fc-altered",
        "pattern": "altered_check",
        "description": "Physical or digital alteration of check fields",
        "agent_signals": [
            "alteration_detected",
            "amount_words_numeric_mismatch",
            "invalid_micr_checksum",
            "low_ocr_confidence",
            "payee_name_mismatch",
        ],
    },
    {
        "id": "fc-synthetic",
        "pattern": "synthetic_identity",
        "description": "Account opened with fabricated identity using a mix of real and fake info",
        "agent_signals": [
            "synthetic_identity_flag",
            "kyc_not_verified",
            "new_account_large_amount",
            "flag_synthetic_identity_suspected",
        ],
    },
]


class _MockCosmosContainer:
    def __init__(self, items: list[dict]):
        self._items = items

    def read_all_items(self):
        return iter(self._items)


def install(monkeypatch=None):
    """
    Replace cosmos/velocity functions in the Tier-2 engine modules with
    fixture-backed versions. Idempotent.

    If `monkeypatch` (pytest fixture) is provided, uses it so changes
    auto-revert at test teardown. Otherwise mutates module attrs directly.
    """
    import handlers.shared.cosmos
    import handlers.shared.velocity
    import handlers.tier2.native

    def _get_customer(account_number: str):
        return CUSTOMERS.get(account_number)

    def _query_velocity(account_number: str, days_back: int = 1, exclude_check_id: str = ""):
        return VELOCITY_DATA.get(account_number, VELOCITY_DATA["_default"])

    def _get_container(name: str):
        if name == "fraud_cases":
            return _MockCosmosContainer(FRAUD_CASES)
        return _MockCosmosContainer([])

    targets = [
        (handlers.shared.cosmos,     "get_customer",         _get_customer),
        (handlers.shared.cosmos,     "get_cosmos_container", _get_container),
        (handlers.shared.velocity,   "query_velocity",       _query_velocity),
        (handlers.tier2.native,      "get_customer",         _get_customer),
        (handlers.tier2.native,      "get_cosmos_container", _get_container),
        (handlers.tier2.native,      "query_velocity",       _query_velocity),
    ]
    for module, attr, value in targets:
        if monkeypatch:
            monkeypatch.setattr(module, attr, value)
        else:
            setattr(module, attr, value)
