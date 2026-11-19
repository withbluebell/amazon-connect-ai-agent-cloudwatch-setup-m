"""Repair Service Tools

This module provides repair service management functionality.
"""

import json
import logging
from util import call_iservice
from . import repair_intake as intake
from .repair_intake import REQUIRED_IDENTITY_FIELDS as _REQUIRED_IDENTITY_FIELDS

log = logging.getLogger("mcp_server")


def _get_verified_session(session_client, contactId):
    """Load the session and enforce that identity verification is complete.

    Returns:
        (session, error_response)
        - On success: (session_dict, None)
        - On failure: (None, json_error_string) where the error string is a
          ready-to-return tool result with status "failed".

    Failure cases:
        - Session does not exist              -> message "customer_not_found"
        - Session exists but identity missing -> message "customer_not_verified"
    """
    session = session_client.get(contactId)
    if not session:
        return None, json.dumps({"status": "failed", "message": "customer_not_found"})

    missing = [f for f in _REQUIRED_IDENTITY_FIELDS if not session.get(f)]
    if missing:
        log.info(
            f"identity verification incomplete: contactId='{contactId}', "
            f"missing={missing}"
        )
        return None, json.dumps(
            {"status": "failed", "message": "customer_not_verified"}
        )

    return session, None


def register_repair_tools(mcp, session_client):
    """Register repair service tools to the MCP server.

    Args:
        mcp: FastMCP server instance
        session_client: session store client
    """

    # Fail fast at startup if REPAIR_FIELD_ORDER is misconfigured.
    step_order = intake.get_step_order()
    log.info(f"repair intake order: {list(step_order)}")

    def _next_response(status, draft, session, **extra):
        step = intake.next_step(draft, session)
        body = {"status": status, **extra, "next": intake.describe_step(step, draft, session)}
        return json.dumps(body, ensure_ascii=False)

    @mcp.tool(structured_output=False)
    async def startRepairRequest(contactId: str) -> str:
        """Start collecting the information for a new repair service request.

        ═══════════════════════════════════════════════════════════════════════════════════
        INVOCATION TRIGGER (CRITICAL)
        ═══════════════════════════════════════════════════════════════════════════════════

        ONLY call this tool when the customer EXPLICITLY requests to create a repair order. Do NOT offer, suggest, or proactively propose creating a repair order.

        Examples of explicit requests:
        - "I need to create a repair order"
        - "I want to submit a repair request"
        - "Can you help me set up a repair?"

        The following situations DO NOT constitute a request to create a repair order, and you MUST NOT proactively propose creating one in any of them:
        - faqSearch returned no match / no relevant information.
        - The customer said the FAQ answer did not help, or answered "no" to "Did this solve your problem?".
        - The customer describes a malfunction, defect, or problem with a product (describing a problem is NOT the same as asking to book a repair).
        - You cannot otherwise help and are considering next steps.

        NEVER bridge from "I couldn't find an answer" to "would you like to create a repair order?". Only the customer may open that door, with an explicit request.

        ═══════════════════════════════════════════════════════════════════════════════════
        SERVER-CONTROLLED COLLECTION ORDER (CRITICAL)
        ═══════════════════════════════════════════════════════════════════════════════════

        The collection order is decided by the server, NOT by you. Do NOT plan your own order, and do NOT ask for any field before the server tells you to.

        This tool starts a fresh draft (any previous unfinished draft in this contact is discarded) and returns "next": the ONLY thing you may ask the customer now.

        Loop:
        1. Read "next.step" and "next.instruction" from the latest tool result.
        2. If next.step is a field name: ask the customer for THAT field only, ONE question in ONE <message>, then STOP and wait for the answer.
        3. Submit the answer with submitRepairField(field=next.step, ...). Its result contains the new "next".
        4. If next.step is "identity": verify phone number and name with checkCustomer / setCustomerByPhoneAndName. The setCustomerByPhoneAndName result contains the new "next".
        5. If next.step is "confirm": follow the createRepair CONFIRMATION PROTOCOL using next.summary.

        If you are unsure where you are (for example after an interruption or topic change), call getRepairNextStep instead of guessing.

        Args:
            contactId: The current contact ID from system-variables ({{$.contactId}}).

        Returns:
            JSON: {"status": "started", "next": {...}}
        """
        log.info(f"startRepairRequest: contactId='{contactId}'")
        draft = intake.new_draft()
        session_client.update(contactId, {intake.DRAFT_KEY: draft})
        session = session_client.get(contactId) or {}
        return _next_response("started", draft, session)

    @mcp.tool(structured_output=False)
    async def submitRepairField(
        contactId: str, field: str, value: str = "", skipped: bool = False
    ) -> str:
        """Submit the customer's answer for the CURRENT repair field.

        INTERNAL TOOL - Do NOT mention this tool to the customer.

        Rules:
        - field MUST equal "next.step" from the latest startRepairRequest / submitRepairField / getRepairNextStep / setCustomerByPhoneAndName result. Any other field is rejected with status "wrong_field" and nothing is saved.
        - Submit exactly what the customer said for that field. Normalize spoken formats before submitting (for example "john dot smith at example dot com" -> "john.smith@example.com").
        - If the customer already mentioned this value earlier in the conversation, you may confirm it with the customer ("You mentioned a refrigerator, correct?") instead of asking from scratch, then submit it when the server asks for this field.
        - OPTIONAL field and the customer does not know / wants to skip: call with skipped=true. Do NOT re-ask it.
        - REQUIRED field and the customer cannot provide a valid value: submit what they said (or skipped=true). The server counts failed attempts.

        Args:
            contactId: The current contact ID from system-variables ({{$.contactId}}).
            field: The field name from next.step.
            value: The customer's answer for that field.
            skipped: true if the customer does not know / does not want to provide it.

        Returns:
            JSON with "status" and, except for "escalate", a "next" object:
            - "accepted": Value saved. Continue with "next".
            - "invalid": Value rejected (see "reason"). Ask for the SAME field again; "attemptsLeft" shows how many failed attempts remain for REQUIRED fields.
            - "escalate": REQUIRED fields failed 3 times in total. Stop collecting and transfer to a human agent (see escalation-rules).
            - "wrong_field": You submitted a field out of order. Nothing saved. Ask for next.step instead.
            - "identity_required": Phone number / name verification must happen now (see next.instruction).
            - "failed" with message "intake_not_started": call startRepairRequest first.
        """
        log.info(
            f"submitRepairField: contactId='{contactId}', field='{field}', skipped={skipped}"
        )
        session = session_client.get(contactId) or {}
        draft = intake.load_draft(session)
        if draft is None:
            return json.dumps({"status": "failed", "message": "intake_not_started"})

        expected = intake.next_step(draft, session)
        field = (field or "").strip()
        value = (value or "").strip()

        if expected == intake.STEP_IDENTITY:
            return _next_response("identity_required", draft, session)
        if expected == intake.STEP_CONFIRM:
            return _next_response("all_fields_collected", draft, session)
        if field != expected:
            return _next_response("wrong_field", draft, session, submittedField=field)

        spec = intake.FIELD_SPECS[field]
        reason = None
        if skipped or not value:
            if spec["required"]:
                reason = "required_field_missing"
            else:
                value = ""
        elif spec.get("validate"):
            reason = spec["validate"](value)

        if reason:
            draft["failures"] += 1
            session_client.update(contactId, {intake.DRAFT_KEY: draft})
            if draft["failures"] >= intake.MAX_REQUIRED_FAILURES:
                log.info(f"submitRepairField escalate: contactId='{contactId}'")
                return json.dumps({"status": "escalate", "reason": reason})
            return _next_response(
                "invalid",
                draft,
                session,
                reason=reason,
                attemptsLeft=intake.MAX_REQUIRED_FAILURES - draft["failures"],
            )

        draft["values"][field] = value
        draft["completed"].append(field)
        session_client.update(contactId, {intake.DRAFT_KEY: draft})
        return _next_response("accepted", draft, session)

    @mcp.tool(structured_output=False)
    async def getRepairNextStep(contactId: str) -> str:
        """Return the current step of an in-progress repair request (read-only).

        INTERNAL TOOL - Do NOT mention this tool to the customer.

        Call this when you are not sure which repair field to ask next, for example after an interruption, a barge-in, or a short topic change during repair collection. Never guess the next field.

        Args:
            contactId: The current contact ID from system-variables ({{$.contactId}}).

        Returns:
            JSON: {"status": "in_progress", "next": {...}} or
            {"status": "failed", "message": "intake_not_started"}.
        """
        session = session_client.get(contactId) or {}
        draft = intake.load_draft(session)
        if draft is None:
            return json.dumps({"status": "failed", "message": "intake_not_started"})
        return _next_response("in_progress", draft, session)

    @mcp.tool(structured_output=False)
    async def createRepair(
        contactId: str,
        srType: str = "Inquiry",
        srSubType: str = "Product",
    ) -> str:
        """Create the repair service request from the collected draft.

        ═══════════════════════════════════════════════════════════════════════════════════
        PREREQUISITE
        ═══════════════════════════════════════════════════════════════════════════════════

        Do NOT collect repair fields yourself for this tool. All values (product category, model, brand, serial number, issue, email, service address, zip code, phone number, name) are collected step by step through startRepairRequest / submitRepairField, in the order the server dictates, and are read by this tool from the session. This tool takes no field values.

        Only call this tool after:
        1. The latest repair tool result returned next.step = "confirm", AND
        2. You presented next.summary to the customer and received explicit confirmation.

        If called too early, it returns status "failed" with message "intake_not_completed" and a "next" object. Continue with that "next" step.

        ═══════════════════════════════════════════════════════════════════════════════════
        CONFIRMATION PROTOCOL
        ═══════════════════════════════════════════════════════════════════════════════════

        This tool requires user confirmation. When next.step is "confirm", present EVERY item of next.summary (label: value), in the given order, in a single <message></message>, then ask the customer to confirm. Only call the tool after receiving explicit confirmation.

        The confirmation summary is presented EXACTLY ONCE. There are two and only two outcomes:
        - Customer says "Yes" or equivalent approval: Proceed immediately with the tool call. Do NOT ask for confirmation again.
        - Customer says "No", indicates something is wrong, or wants to change anything: Tell the customer you cannot modify the details here in this conversation and transfer to a human agent to help with modifications.

        ═══════════════════════════════════════════════════════════════════════════════════
        PARAMETERS
        ═══════════════════════════════════════════════════════════════════════════════════

            contactId (str, required)
                Source: system-variables ({{$.contactId}})

            srType (str, fixed) - always "Inquiry". Do NOT ask the customer.
            srSubType (str, fixed) - always "Product". Do NOT ask the customer.

        Returns:
            JSON string with one of the following statuses:
            - "success": Repair request created successfully, includes srNumber (the service request number assigned to this order)
            - "failed": Creation failed. Message "intake_not_completed" means collection is not finished (continue with "next"). Message "customer_not_verified" means the identity step is not done.

        POST-CALL: After a SUCCESSFUL tool call, append the questionFinish placeholder ({{$.Custom.questionFinish}}) at the end of your customer-facing message, separated by one blank line. Do NOT append it if the call failed or returned an error. Appending the questionFinish placeholder counts as your single question for that turn.
        """
        log.info(f"createRepair: contactId='{contactId}'")

        session, err = _get_verified_session(session_client, contactId)
        if err:
            return err

        draft = intake.load_draft(session)
        if draft is None:
            return json.dumps({"status": "failed", "message": "intake_not_started"})
        if intake.next_step(draft, session) != intake.STEP_CONFIRM:
            return _next_response("failed", draft, session, message="intake_not_completed")

        values = draft["values"]
        productCategory = values.get("productCategory", "")
        description = values.get("description", "")
        srType = srType.strip() if srType else "Inquiry"
        srSubType = srSubType.strip() if srSubType else "Product"

        # Format description with productCategory if provided
        formatted_description = (
            f"[{productCategory}] {description}" if productCategory else description
        )

        api_result = await call_iservice(
            "/api/iservice-ccm/bot/api/chatBotCreateDraftSr",
            {
                "bu": session.get("bu"),
                "countryCode": session.get("countryCode"),
                "phoneNumber": session.get("phoneNumber"),
                "customerName": session.get("customerName"),
                "srType": srType,
                "srSubType": srSubType,
                "productModel": values.get("productModel", ""),
                "serialNumber": values.get("serialNumber", ""),
                "brand": values.get("brand", ""),
                "addressRemark": values.get("addressRemark", ""),
                "description": formatted_description,
                "zipCode": values.get("zipCode", ""),
                "email": values.get("email", ""),
                "contactId": contactId,
                "status": "Draft",
            },
        )

        error_msg = None
        if api_result.get("resultCode") == "ISC-000":
            api_data = api_result.get("data", {})
            if api_data.get("success"):
                sr_number = api_data.get("srNumber", "Unknown")
                # Clear the draft so a second repair in this contact starts fresh
                # and a repeated createRepair call cannot create a duplicate.
                session_client.remove_keys(contactId, [intake.DRAFT_KEY])
                return json.dumps(
                    {
                        "status": "success",
                        "message": f"Your repair order has been created successfully. ",
                        "srNumber": sr_number,
                    }
                )
            else:
                error_msg = api_data.get("message", "Unknown error")
        else:
            error_msg = api_result.get("resultMsg", "Unknown error")

        log.info(f"createRepair error: {error_msg}")
        return json.dumps(
            {"status": "failed", "message": "Failed to create repair order."}
        )

    @mcp.tool(structured_output=False)
    async def listRepairs(contactId: str) -> str:
        """Query the list of customer's repair service requests.

        WHEN TO CALL: ONLY call this tool when the customer explicitly requests to QUERY or VIEW their repair/service order list.
        Trigger phrases include: "show my repairs", "list my service requests", "what repairs do I have", "check my orders", etc.

        MANDATORY IDENTITY GATE: This tool requires a verified customer identity.
        Call checkCustomer FIRST (before asking the customer anything). If it
        returns "customer_not_found", complete setCustomerByPhoneAndName until it
        returns "success" before calling this tool. If identity is not verified,
        this tool returns status "failed" with message "customer_not_verified" and
        performs no action (enforced server-side). If a prior interruption leaves
        verification status uncertain, re-run checkCustomer rather than assuming.

        ═══════════════════════════════════════════════════════════════════════════════════
        FIELD COLLECTION FLOW
        ═══════════════════════════════════════════════════════════════════════════════════

        Collection priority:
        1. Brand (OPTIONAL) — filter by brand; if not provided, list all orders for this contact
        2. Status filter (OPTIONAL) — e.g. "pending", "completed", "in-progress"; if not specified, return all

        Confirmation: NO — Attempt the tool call immediately with best interpretation after collecting parameters (no confirmation summary needed).

        ═══════════════════════════════════════════════════════════════════════════════════
        PARAMETER MAPPING (Prompt → Tool)
        ═══════════════════════════════════════════════════════════════════════════════════

        AUTO-POPULATED FROM SESSION (NOT collected from customer):
            contactId (str, required)
                Source: system-variables ({{$.contactId}})
                Used to look up verified customer information in session.

            countryCode, phoneNumber, customerName (NOT parameters)
                Source: Retrieved from session using contactId
                Used when calling API, not exposed as function parameters.

        Returns:
            JSON string with one of the following statuses:
            - "success": Successfully retrieved repair list, includes repairs array
            - "failed": Query failed, includes reason for failure. Message "customer_not_verified" means the identity gate was not satisfied — complete checkCustomer + setCustomerByPhoneAndName, then retry.

        POST-CALL: After a SUCCESSFUL tool call, append the questionFinish placeholder ({{$.Custom.questionFinish}}) at the end of your customer-facing message, separated by one blank line. Do NOT append it if the call failed or returned an error. Appending the questionFinish placeholder counts as your single question for that turn.
        """
        log.info(f"listRepairs: contactId='{contactId}'")

        session, err = _get_verified_session(session_client, contactId)
        if err:
            return err

        api_result = await call_iservice(
            "/api/iservice-ccm/bot/api/serviceReques/list",
            {
                "bu": session.get("bu"),
                "countryCode": session.get("countryCode"),
                "phoneNumber": session.get("phoneNumber"),
                "customerName": session.get("customerName"),
                "srCount": 10,
            },
        )

        error_msg = None
        if api_result.get("resultCode") == "ISC-000":
            api_data = api_result.get("data", {})
            service_requests = api_data.get("serviceRequestItems", [])

            if isinstance(service_requests, list):
                return json.dumps(
                    {
                        "status": "success",
                        "message": f"Found {len(service_requests)} repair request(s)",
                        "count": len(service_requests),
                        "repairs": service_requests,
                    },
                    ensure_ascii=False,
                )
            else:
                error_msg = "Invalid data format returned from API"
        else:
            error_msg = api_result.get("resultMsg", "Unknown error")

        log.info(f"listRepairs error: {error_msg}")
        return json.dumps({"status": "failed", "message": "listRepairs error"})

    @mcp.tool(structured_output=False)
    async def cancelRepair(contactId: str, srNumber: str, cancelReason: str) -> str:
        """Cancel a repair service request.

        WHEN TO CALL: ONLY call this tool when the customer explicitly requests to CANCEL a specific repair/service order.
        Trigger phrases include: "cancel my repair", "cancel service request", "I want to cancel order", etc.

        MANDATORY IDENTITY GATE: This tool requires a verified customer identity.
        Call checkCustomer FIRST (before asking the customer anything). If it
        returns "customer_not_found", complete setCustomerByPhoneAndName until it
        returns "success" before calling this tool. If identity is not verified,
        this tool returns status "failed" with message "customer_not_verified" and
        performs no action (enforced server-side). If a prior interruption leaves
        verification status uncertain, re-run checkCustomer rather than assuming.

        ═══════════════════════════════════════════════════════════════════════════════════
        FIELD COLLECTION FLOW
        ═══════════════════════════════════════════════════════════════════════════════════

        Collection priority (ask ONE field per message):
        1. Repair order ID (srNumber) — REQUIRED — which order to cancel?
        2. Reason for cancellation (cancelReason) — OPTIONAL — feedback only

        ═══════════════════════════════════════════════════════════════════════════════════
        CONFIRMATION PROTOCOL
        ═══════════════════════════════════════════════════════════════════════════════════

        This tool requires user confirmation. After collecting all fields, present a summary and ask the customer to confirm once before calling the tool.

        The confirmation summary is presented EXACTLY ONCE. There are two and only two outcomes:
        - Customer says "Yes" or equivalent approval: Proceed immediately with the tool call. Do NOT ask for confirmation again.
        - Customer says "No", indicates something is wrong, or wants to change anything: Tell the customer you cannot modify the details here in this conversation and transfer to a human agent to help with modifications.

        ═══════════════════════════════════════════════════════════════════════════════════
        PARAMETER MAPPING (Prompt → Tool)
        ═══════════════════════════════════════════════════════════════════════════════════

        USER-PROVIDED INPUTS (from customer via Prompt):
            srNumber (str, required)
                Mapping: Repair order ID (field #1)
                Customer-provided service request number to cancel.

            cancelReason (str, optional)
                Mapping: Reason for cancellation (field #2)
                Customer-provided feedback about why they are cancelling.

        AUTO-POPULATED FROM SESSION (NOT collected from customer):
            contactId (str, required)
                Source: system-variables ({{$.contactId}})
                Used to look up verified customer information in session.

            countryCode, phoneNumber, customerName (NOT parameters)
                Source: Retrieved from session using contactId
                Used when calling API, not exposed as function parameters.

        Returns:
            JSON string with one of the following statuses:
            - "success": Repair cancelled successfully
            - "failed": Cancellation failed, includes reason for failure. Message "customer_not_verified" means the identity gate was not satisfied — complete checkCustomer + setCustomerByPhoneAndName, then retry.

        POST-CALL: After a SUCCESSFUL tool call, append the questionFinish placeholder ({{$.Custom.questionFinish}}) at the end of your customer-facing message, separated by one blank line. Do NOT append it if the call failed or returned an error. Appending the questionFinish placeholder counts as your single question for that turn.
        """
        log.info(
            f"cancelRepair: contactId='{contactId}', srNumber='{srNumber}', cancelReason='{cancelReason}'"
        )

        session, err = _get_verified_session(session_client, contactId)
        if err:
            return err

        srNumber = srNumber.strip()
        cancelReason = cancelReason.strip()

        api_result = await call_iservice(
            "/api/iservice-ccm/bot/api/serviceReques/cancel",
            {
                "bu": session.get("bu"),
                "countryCode": session.get("countryCode"),
                "phoneNumber": session.get("phoneNumber"),
                "customerName": session.get("customerName"),
                "srNumber": srNumber,
                "cancelReason": cancelReason,
            },
        )

        error_msg = None
        if api_result.get("resultCode") == "ISC-000":
            api_data = api_result.get("data", {})
            if api_data.get("success"):
                return json.dumps(
                    {"status": "success", "message": "Repair cancelled successfully"}
                )
            else:
                error_msg = api_data.get("message", "Unknown error")
        else:
            error_msg = api_result.get("resultMsg", "Unknown error")

        log.info(f"cancelRepair error: {error_msg}")
        return json.dumps({"status": "failed", "message": "cancelRepair error"})

    @mcp.tool(structured_output=False)
    async def getRepairDetail(contactId: str, srNumber: str) -> str:
        """Query detailed information of a specific repair service request.

        WHEN TO CALL: ONLY call this tool when the customer explicitly requests to VIEW or CHECK the details of a specific repair/service order.
        Trigger phrases include: "show details of my repair", "check status of SR number", "what is the detail of my service request", etc.

        MANDATORY IDENTITY GATE: This tool requires a verified customer identity.
        Call checkCustomer FIRST (before asking the customer anything). If it
        returns "customer_not_found", complete setCustomerByPhoneAndName until it
        returns "success" before calling this tool. If identity is not verified,
        this tool returns status "failed" with message "customer_not_verified" and
        performs no action (enforced server-side). If a prior interruption leaves
        verification status uncertain, re-run checkCustomer rather than assuming.

        ═══════════════════════════════════════════════════════════════════════════════════
        FIELD COLLECTION FLOW
        ═══════════════════════════════════════════════════════════════════════════════════

        Collection priority:
        1. Repair order ID (srNumber) — REQUIRED — which order to retrieve details for?

        Confirmation: NO — Attempt the tool call immediately with best interpretation after collecting the REQUIRED parameter (no confirmation summary needed).

        ═══════════════════════════════════════════════════════════════════════════════════
        PARAMETER MAPPING (Prompt → Tool)
        ═══════════════════════════════════════════════════════════════════════════════════

        USER-PROVIDED INPUTS (from customer via Prompt):
            srNumber (str, required)
                Mapping: Repair order ID
                Customer-provided service request number to get details for.

        AUTO-POPULATED FROM SESSION (NOT collected from customer):
            contactId (str, required)
                Source: system-variables ({{$.contactId}})
                Used to look up verified customer information in session.

            countryCode, phoneNumber, customerName (NOT parameters)
                Source: Retrieved from session using contactId
                Used when calling API, not exposed as function parameters.

        Returns:
            JSON string with one of the following statuses:
            - "success": Successfully retrieved repair details, includes full repair information
            - "failed": Query failed, includes reason for failure. Message "customer_not_verified" means the identity gate was not satisfied — complete checkCustomer + setCustomerByPhoneAndName, then retry.

        POST-CALL: After a SUCCESSFUL tool call, append the questionFinish placeholder ({{$.Custom.questionFinish}}) at the end of your customer-facing message, separated by one blank line. Do NOT append it if the call failed or returned an error. Appending the questionFinish placeholder counts as your single question for that turn.
        """
        log.info(f"getRepairDetail: contactId='{contactId}', srNumber='{srNumber}'")

        session, err = _get_verified_session(session_client, contactId)
        if err:
            return err

        srNumber = srNumber.strip()

        api_result = await call_iservice(
            "/api/iservice-ccm/bot/api/serviceReques/detail",
            {
                "bu": session.get("bu"),
                "countryCode": session.get("countryCode"),
                "phoneNumber": session.get("phoneNumber"),
                "customerName": session.get("customerName"),
                "srNumber": srNumber,
            },
        )

        if api_result.get("resultCode") == "ISC-000":
            api_data = api_result.get("data", {})

            # 判断是否查到单据：srNumber 不为空表示查到了
            if api_data and api_data.get("srNumber"):
                return json.dumps(
                    {
                        "status": "success",
                        "message": "Repair details retrieved successfully",
                        "detail": api_data,
                    },
                    ensure_ascii=False,
                )
            else:
                return json.dumps(
                    {
                        "status": "failed",
                        "message": "No repair record found with the provided information",
                    }
                )

        log.info(
            f"getRepairDetail error: {api_result.get('resultMsg') or 'Unknown error'}"
        )
        return json.dumps({"status": "failed", "message": "getRepairDetail error"})
