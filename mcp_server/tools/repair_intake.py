"""Server-side ordered field collection for createRepair.

Ordering rules written only in a tool docstring are soft. The LLM can still
reorder questions, merge two questions into one, or skip a field. This module
moves the order to the server: the draft repair request is kept in the session
store, and only the server decides which field is collected next.

Flow:
    startRepairRequest  -> resets the draft, returns the first step
    submitRepairField   -> accepts ONLY the expected next field, returns the next step
    (identity step)     -> handled by checkCustomer / setCustomerByPhoneAndName
    createRepair        -> reads every value from the draft, never from LLM arguments

The order can be configured with the REPAIR_FIELD_ORDER environment variable
(comma separated step names, see FIELD_SPECS and STEP_IDENTITY). OPTIONAL
fields can be left out of the list to stop asking them. REQUIRED fields and
the identity step must always be present.
"""

import os
import re

# Identity fields that MUST be present in the session before any repair
# operation can call the downstream iService API. These are written ONLY by
# setCustomerByPhoneAndName.
REQUIRED_IDENTITY_FIELDS = ("bu", "countryCode", "phoneNumber", "customerName")

# Pseudo step: phone number + name verification (setCustomerByPhoneAndName).
STEP_IDENTITY = "identity"
# Pseudo step returned when every field has been collected.
STEP_CONFIRM = "confirm"

DRAFT_KEY = "repairDraft"
MAX_REQUIRED_FAILURES = 3

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")
_ZIP_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 \-]{1,11}$")


def _validate_email(value):
    return None if _EMAIL_RE.match(value) else "invalid_email_format"


def _validate_zip(value):
    return None if _ZIP_RE.match(value) else "invalid_zip_code_format"


def _validate_description(value):
    return None if len(value) >= 2 else "description_too_short"


# ask: English hint for the question. The LLM translates it to responseLanguage.
FIELD_SPECS = {
    "productCategory": {
        "label": "Product category",
        "required": False,
        "ask": "Ask which product category the appliance is (for example refrigerator, air conditioner).",
    },
    "productModel": {
        "label": "Model",
        "required": False,
        "ask": "Ask for the product model number (usually on the nameplate label).",
    },
    "brand": {
        "label": "Brand",
        "required": False,
        "ask": "Ask which brand the appliance is.",
    },
    "serialNumber": {
        "label": "Serial number",
        "required": False,
        "ask": "Ask for the product serial number, if they have it.",
    },
    "description": {
        "label": "Issue",
        "required": True,
        "ask": "Ask the customer to describe the problem with the appliance.",
        "validate": _validate_description,
    },
    "email": {
        "label": "Email",
        "required": True,
        "ask": "Ask for the customer's email address.",
        "validate": _validate_email,
    },
    "addressRemark": {
        "label": "Service address",
        "required": False,
        "ask": "Ask for the service address where the appliance is located.",
    },
    "zipCode": {
        "label": "Zip code",
        "required": True,
        "ask": "Ask for the zip / postal code of the service address.",
        "validate": _validate_zip,
    },
}

# Product information first; phone number (identity), email and address last.
DEFAULT_STEP_ORDER = (
    "productCategory",
    "productModel",
    "brand",
    "serialNumber",
    "description",
    STEP_IDENTITY,
    "email",
    "addressRemark",
    "zipCode",
)

_step_order = None


def get_step_order():
    """Return the configured step order (parsed once, validated)."""
    global _step_order
    if _step_order is not None:
        return _step_order

    raw = os.environ.get("REPAIR_FIELD_ORDER", "").strip()
    order = (
        tuple(s.strip() for s in raw.split(",") if s.strip())
        if raw
        else DEFAULT_STEP_ORDER
    )

    known = set(FIELD_SPECS) | {STEP_IDENTITY}
    unknown = [s for s in order if s not in known]
    if unknown:
        raise ValueError(f"REPAIR_FIELD_ORDER has unknown steps: {unknown}")
    if len(set(order)) != len(order):
        raise ValueError("REPAIR_FIELD_ORDER has duplicate steps")
    must_have = {f for f, s in FIELD_SPECS.items() if s["required"]} | {STEP_IDENTITY}
    missing = sorted(must_have - set(order))
    if missing:
        raise ValueError(f"REPAIR_FIELD_ORDER is missing mandatory steps: {missing}")

    _step_order = order
    return _step_order


def is_verified(session):
    return bool(session) and all(session.get(f) for f in REQUIRED_IDENTITY_FIELDS)


def new_draft():
    return {"values": {}, "completed": [], "failures": 0}


def load_draft(session):
    draft = (session or {}).get(DRAFT_KEY)
    if not draft:
        return None
    return {
        "values": dict(draft.get("values") or {}),
        "completed": list(draft.get("completed") or []),
        # DynamoDB returns numbers as Decimal
        "failures": int(draft.get("failures") or 0),
    }


def next_step(draft, session):
    """First step in the configured order that is not done yet."""
    for step in get_step_order():
        if step == STEP_IDENTITY:
            if not is_verified(session):
                return STEP_IDENTITY
        elif step not in draft["completed"]:
            return step
    return STEP_CONFIRM


def summary(draft, session):
    """Ordered list of every collected value, used for the confirmation message."""
    items = []
    for step in get_step_order():
        if step == STEP_IDENTITY:
            items.append(
                {
                    "label": "Phone number",
                    "value": f"+{session.get('countryCode')} {session.get('phoneNumber')}",
                }
            )
            items.append({"label": "Name", "value": session.get("customerName")})
        else:
            value = draft["values"].get(step) or ""
            items.append(
                {"label": FIELD_SPECS[step]["label"], "value": value or "(not provided)"}
            )
    return items


def describe_step(step, draft, session):
    """Build the 'next' object that tells the LLM exactly what to do now."""
    if step == STEP_IDENTITY:
        return {
            "step": STEP_IDENTITY,
            "instruction": (
                "Verify the customer's phone number and name now. Call checkCustomer; "
                "if it returns customer_not_found, run the setCustomerByPhoneAndName "
                "verification flow. Its success response contains the next repair step."
            ),
        }
    if step == STEP_CONFIRM:
        return {
            "step": STEP_CONFIRM,
            "instruction": (
                "All fields are collected. Present this summary ONCE in a single "
                "message, in this order, and ask the customer to confirm. Call "
                "createRepair only after explicit confirmation."
            ),
            "summary": summary(draft, session),
        }
    spec = FIELD_SPECS[step]
    return {
        "step": step,
        "required": spec["required"],
        "instruction": spec["ask"]
        + (
            " REQUIRED."
            if spec["required"]
            else " OPTIONAL: if the customer does not know or wants to skip, submit with skipped=true."
        ),
    }
