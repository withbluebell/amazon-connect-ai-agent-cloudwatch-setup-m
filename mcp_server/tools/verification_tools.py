"""Customer Verification Tools

This module provides customer identity verification functionality.
"""

import json
import logging
from util import call_iservice
from . import repair_intake as intake
from .repair_intake import REQUIRED_IDENTITY_FIELDS as _REQUIRED_IDENTITY_FIELDS

log = logging.getLogger("mcp_server")


def register_verification_tools(mcp, session_client):
    """Register customer verification tools to the MCP server.
    
    Args:
        mcp: FastMCP server instance
        session_client: session store client
    """

    @mcp.tool(structured_output=False)
    async def checkCustomer(contactId: str) -> str:
        """Check if customer information exists in the current session.

        INTERNAL TOOL - Do NOT call this tool directly from chatbot.

        Args:
            contactId: The current contact ID from system-variables.

        Returns:
            JSON string with one of the following statuses:
            - "customer_found": Customer identity is verified and stored in the session. You may proceed with operations that require customer identity.
            - "customer_not_found": Customer identity is NOT verified.

        MANDATORY GATE (applies to createRepair, listRepairs, cancelRepair,
        getRepairDetail):
        - These tools REQUIRE a verified customer identity. Identity is considered
          verified ONLY after setCustomerByPhoneAndName has completed successfully
          in this session.
        - For listRepairs, cancelRepair and getRepairDetail: if this tool returns
          "customer_not_found", you MUST complete the setCustomerByPhoneAndName
          verification flow BEFORE calling them.
        - For creating a repair: do NOT verify up front. startRepairRequest /
          submitRepairField collect the fields in a server-defined order, and the
          verification happens when they return next.step = "identity".
        - Calling a repair tool without a verified identity will be rejected by the
          tool with status "failed" and message "customer_not_verified". This is a
          hard server-side check, not a suggestion — it cannot be bypassed by
          supplying field values directly.
        """
        session = session_client.get(contactId)
        # Identity is verified ONLY when the session exists AND all required
        # identity fields are present. A session record may exist without a
        # verified identity (e.g. it was created by flow/FAQ context writes that
        # do not populate identity fields), so checking existence alone is not
        # enough — it must match the same gate enforced by the repair tools
        # (_get_verified_session / _REQUIRED_IDENTITY_FIELDS). Otherwise
        # checkCustomer would report "customer_found" while createRepair rejects
        # the request with "customer_not_verified".
        if not session or any(not session.get(f) for f in _REQUIRED_IDENTITY_FIELDS):
            return json.dumps({"status": "customer_not_found"})

        return json.dumps({"status": "customer_found"})

    @mcp.tool(structured_output=False)
    async def setCustomerByPhoneAndName(contactId: str, bu: str, countryCode: str, phoneNumber: str, customerName: str) -> str:
        """set customer information by phone number and customer name.

        INTERNAL TOOL - Do NOT call this tool directly from chatbot.

        Follow this verification flow strictly before calling:

        CASE A — customer_info.userNumber is available:
        1. Parse countryCode and phoneNumber from customer_info.userNumber.
           Format: the digits after "+" up to the first space are the countryCode;
           the remaining digits are the phoneNumber.
        2. Present the parsed number to the customer as "+{countryCode} {phoneNumber}"
           and ask them to confirm whether it is correct.
        3. Wait for the customer's confirmation:
           - If correct: ask for full name (customerName), then present
             "+{countryCode} {phoneNumber}, name: {customerName}" and ask to confirm.
             Only call this tool after receiving explicit confirmation.
           - If incorrect: follow CASE B below.

        CASE B — customer_info.userNumber is empty or customer says number is wrong:
        1. Ask for international dialing code ONLY. Wait for response.
        2. Ask for phone number ONLY. Wait for response.
        3. Ask for full name ONLY. Wait for response.
        4. Present "+{countryCode} {phoneNumber}, name: {customerName}" and ask to confirm.
           Only call this tool after receiving explicit confirmation.

        Do NOT provide example phone numbers or country codes to the customer.
        Do NOT invent or suggest any specific number format to the customer.

        Args:
            contactId: The current contact ID from system-variables.
            bu: Use the BU value from customer_info.
            countryCode: International dialing code digits only, no "+" prefix.
            phoneNumber: Phone number digits only, no country code.
            customerName: The customer's full name.

        Returns:
            JSON string with one of the following statuses:
            - "success": Customer information set successfully.
              If a repair request is being collected (startRepairRequest was
              called), the result also contains "next": continue the repair
              collection with that step (ask ONLY that field next).
        """
        countryCode = countryCode.strip()
        phoneNumber = phoneNumber.strip()
        customerName = customerName.strip()
        
        # Set customer information in session. update() (not put()) so other
        # session attributes, e.g. an in-progress repair draft collected before
        # the identity step, are kept.
        session_client.update(contactId, {
            "bu": bu,
            "countryCode": countryCode,
            "phoneNumber": phoneNumber,
            "customerName": customerName
        })

        result = {"status": "success"}
        session = session_client.get(contactId) or {}
        draft = intake.load_draft(session)
        if draft is not None:
            step = intake.next_step(draft, session)
            result["next"] = intake.describe_step(step, draft, session)
        return json.dumps(result, ensure_ascii=False)
