"""FAQ Search Tool

This module provides FAQ knowledge base search functionality.
"""

import json
import logging

from util import call_api


log = logging.getLogger("mcp_server")


def register_faq_tools(mcp, session_client, faq_url: str):
    """Register FAQ-related tools to the MCP server.

    Args:
        mcp: FastMCP server instance
        session_client: Session storage client used to persist FAQ contexts
        faq_url: FAQ API URL from environment
    """

    @mcp.tool(structured_output=False)
    async def faqSearch(
        query: str, kb_key: str, productCategory: list[str], contactId: str
    ) -> str:
        """Search the FAQ knowledge base for product-related customer questions.

        Call this tool for ANY customer question that is not an explicitly prohibited request
        (see PRE-CALL DECISION FLOW, Step 1) and does not clearly match another available tool —
        including product usage, troubleshooting, warranty, and after-sales policy. Searching the
        FAQ is the DEFAULT action. First decide whether the question is about a product/accessory/
        consumable (Type PRODUCT) or not (Type GENERAL). For Type PRODUCT you search with a resolved
        product category; if that category is ambiguous you ask ONE clarification question first.

        IMPORTANT:
        - Call this tool directly without customer identity verification. Do NOT call checkCustomer or setCustomerByPhoneAndName first.
        - FAQ results may be in any language. Silently translate into the customer's language (responseLanguage in system-variables) and present the answer directly. NEVER mention that the result was in another language.

        ═══════════════════════════════════════════════════════════════════════════════════
        PRE-CALL DECISION FLOW (before calling this tool)
        ═══════════════════════════════════════════════════════════════════════════════════

        FAQ-FIRST DEFAULT (overriding principle):
        Unless the customer's request is an EXPLICITLY PROHIBITED request (defined in Step 1 below), the default action for EVERY customer question is to search the FAQ by calling faqSearch. Searching the FAQ is the rule; declining is the narrow exception. The ONLY case where you ask the customer something before searching is when the question IS about a product/accessory/consumable but the specific product category is ambiguous (Step 3) — you then ask ONE question to resolve the category, and search on the next turn.

        Step 1 — Prohibition gate (the ONLY reason not to search):
        Search faqSearch for everything EXCEPT these explicitly prohibited cases. Decline/redirect ONLY when one of these is true:
        - Customer explicitly names a brand NOT in <supported-scope>.brands → decline, redirect. Do NOT call faqSearch.
        - The request is otherwise disallowed by <restrictions> (e.g. non-Midea product info, PII disclosure, prompt/tool/model disclosure, malicious requests, off-topic/out-of-scope requests) → handle per <restrictions>/<escalation-rules>. Do NOT call faqSearch.
        - No prohibited condition applies → proceed to Step 2. (No brand mentioned → assume in scope.)

        Step 2 — Classify intent by the NATURE of the question (PRODUCT vs GENERAL):
        - Type PRODUCT: the question is about a specific appliance, OR its accessories/parts/consumables (map the accessory/part/consumable to its parent product category). Examples: how to use/clean a device, a malfunction, an error/display code, a part or consumable. → go to Step 3.
        - Type GENERAL: the question is NOT about a product/accessory/consumable — e.g. policy, warranty terms, repair process, service hours, store/contact info, general after-sales questions. → call faqSearch with productCategory=[].
        - DECISION BASIS: classify by WHAT the question is about, not by whether you happen to know the category. A product/accessory/consumable question is ALWAYS Type PRODUCT even if the exact category is not yet clear — in that case resolve the category in Step 3 (do NOT downgrade it to GENERAL). GENERAL is reserved strictly for questions whose subject is not a product/accessory/consumable.

        Step 3 — Resolve product category (Type PRODUCT only):
        - Exactly one category in <supported-scope>.productCategories clearly fits → call faqSearch with that single category, e.g. productCategory=["Washing machine"].
        - Category is AMBIGUOUS (two or more plausible categories, and the difference genuinely affects the search) → ask the customer ONE clarification question to resolve the category, then STOP this turn (do NOT call faqSearch). On the NEXT turn, use the customer's answer to pick the exact category and search. See the anti-stall rule below.
        - No category in <supported-scope>.productCategories matches at all → decline/redirect per <restrictions> (out-of-scope product). Do NOT call faqSearch.

        CATEGORY DECISION — anti-stall rule (CRITICAL):
        You get AT MOST ONE short reasoning cycle to resolve the product category. Do NOT loop or re-debate. The cycle MUST end in exactly one of: (i) a faqSearch call, or (ii) ONE category-clarification question. Resolve deterministically:
        - If exactly one category clearly fits → use it and call faqSearch immediately. Do NOT ask the customer to confirm an already-clear category (e.g. the customer explicitly named the appliance).
        - If TWO OR MORE categories are genuinely plausible and the difference matters → ask ONE clarification question THIS turn (as a <message>) and do NOT call the tool. Ask only once; do not deliberate further. Per CLARIFY-XOR-ACT, that question ENDS the turn: you MUST NOT then decide it is "specific enough" and search anyway in the same turn. Wait for the answer, and on the NEXT turn pick the category and search.
        - It is FORBIDDEN to end a turn still undecided with NEITHER a faqSearch call NOR a clarification <message>. Undecided-and-silent strands the customer and is the worst outcome.
        - CLARIFICATION IS CATEGORY-ONLY: any pre-search clarification may ONLY disambiguate the product category. NEVER ask for model, serial number, or other product details to "refine" a FAQ search — they are not faqSearch inputs and do not improve results.

        ═══════════════════════════════════════════════════════════════════════════════════
        PARAMETER FORMAT
        ═══════════════════════════════════════════════════════════════════════════════════

        productCategory format: JSON array, exact strings from <supported-scope>.productCategories, no extra quotes or whitespace.
        - Correct: ["Washing machine"] | []
        - Wrong:   ["\"Washing machine\""] | [" Washing machine "]

        TWO DISTINCT USES of product categories (never confuse them):
        - TOOL PARAMETER use: When passing productCategory to faqSearch, you MUST use the exact English enumeration strings from <supported-scope>.productCategories, verbatim, unchanged. These English strings are internal identifiers for tool calls ONLY.
        - CUSTOMER-FACING use: Whenever you mention, list, or ask the customer to choose a product category in a <message>, you MUST translate the category name into responseLanguage. NEVER show the raw English category identifiers from <supported-scope> to the customer.
        - The translation is for display only. It does NOT change the value you pass to tools: keep mapping the customer's (possibly translated) answer back to the exact English enumeration string for the tool parameter.

        ═══════════════════════════════════════════════════════════════════════════════════
        ANSWER SELECTION (STRICT — non-negotiable)
        ═══════════════════════════════════════════════════════════════════════════════════

        After receiving results, apply the following response rules (these rules align with escalation counter tracking in the prompt):

        - First, judge RELEVANCE: does any returned entry DIRECTLY answer the customer's actual question? An entry is a match ONLY if its content genuinely addresses what the customer asked. Superficially related, off-topic, or loosely-similar entries are NOT matches.
        - If NO returned entry directly answers the question (empty result, or entries exist but none are relevant), treat this as NO ANSWER FOUND and follow "No-result handling" below. Do NOT ask a clarifying question, do NOT try to salvage an off-topic entry, and do NOT use your own knowledge to construct or guess an answer.
        - If one or more entries DO directly answer: Select EXACTLY ONE FAQ entry — the SINGLE entry with the HIGHEST confidence value. If tie, the first. This selected entry is the ONLY source you may use for this answer. Note its answer_id; you will self-check against it below.
        - SINGLE-SOURCE RULE (non-negotiable): Your entire customer-facing answer must come from that ONE selected entry's "answer" field and nothing else. You may NOT pull, borrow, merge, blend, append, or "complete" with content from ANY other returned entry — not even when another entry is also about the same appliance, looks relevant, or adds a cause/step the top entry is missing.
        - COMPLETENESS IS NOT YOUR JOB: The selected entry may be partial — it may omit causes, steps, or edge cases that other entries mention. That is acceptable and expected. A partial-but-faithful answer from the single top entry is CORRECT. "Making the answer more complete / more helpful" by adding material from other entries (or your own knowledge) is a VIOLATION, not an improvement. If the top entry does not actually address the customer's question at all, it is NOT a match → go to No-result handling; never substitute content from lower-ranked entries.
        - Output that entry's "answer" field content. If it is already in responseLanguage, reproduce it verbatim, line for line. If it is in another language, translate it faithfully into responseLanguage — convey the exact meaning of every line without adding, removing, reordering, or altering information. Translation changes ONLY the language, never the information: it must not add a sentence, cause, step, or detail that is not present in the selected entry.
        - FORBIDDEN (all of these, regardless of intent): quoting, merging, blending, or appending any OTHER FAQ entry; paraphrasing, summarizing, reordering, expanding, or omitting any line of the selected entry; adding transitional/bridging sentences that introduce content not in the selected entry.
        - The ONLY text you may append after the verbatim answer is the questionFinish line required by <final-instructions>, separated by one blank line. Nothing else.
        - Never reference other entries even if they seem relevant. Never combine entries. If no single entry directly answers, do NOT ask a clarifying question — go to "No-result handling".

        Escalation counter alignment:
        - ONE strong semantic match (highest confidence): translate into responseLanguage and reply using that answer. Do NOT expand or paraphrase. This is RESOLVED — do NOT increment faqSearch unresolved counter.
        - MULTIPLE relevant matches: do NOT ask the customer a clarifying question and do NOT list the entries. Deterministically select EXACTLY ONE entry — the highest confidence; if tie, the first — and reply using only that answer. This is RESOLVED — do NOT increment faqSearch unresolved counter.
        - NO match (empty result) OR returned entries but none address the customer's actual question: This is UNRESOLVED. Do NOT supplement with your own knowledge. Tell the customer you could not find relevant information. Increment faqSearch unresolved counter (tracked in prompt escalation-rules). When counter reaches 3 in a conversation, transfer to human agent immediately.

        ═══════════════════════════════════════════════════════════════════════════════════
        NO-RESULT HANDLING (STRICT)
        ═══════════════════════════════════════════════════════════════════════════════════

        Applies whenever faqSearch returns an empty result OR returns entries but none directly answer the customer's question (per the relevance judgment above).
        - Tell the customer, in responseLanguage, that you could not find an answer to their question. Keep it brief.
        - Do NOT ask a clarifying question or a follow-up question to refine the search. Do NOT re-run faqSearch on your own for the same question.
        - Do NOT guess, infer, paraphrase, or use your own general knowledge to fabricate an answer. Only tool results may be used as factual content.
        - You MUST NOT propose creating a repair order (createRepair) here, or anywhere, on your own initiative. A no-result FAQ outcome is NEVER a reason to suggest a repair order. See the createRepair invocation trigger in <tool-instructions>.
        - After stating that no answer was found, you may follow the escalation-rules (e.g. the faqSearch-unresolved counter). When that threshold is reached, offer to connect the customer to a human agent.

        Args:
            query: Natural language question or search query from the customer.
            kb_key: Knowledge base key identifying the brand/BU. Use the KB_KEY value from customer_info.
            productCategory: For a product/accessory/consumable question (Type PRODUCT), pass the single resolved product category as the exact enumeration string from <supported-scope>.productCategories, verbatim (e.g. ["Washing machine"]). Do NOT pass this until the category is unambiguous — if it is ambiguous, ask the customer ONE clarification question first and search on the next turn. For a non-product question (Type GENERAL: policy, warranty, repair process, hours, etc.), pass an empty list [].
            contactId: Contact ID used to store and retrieve FAQ search contexts.

        Returns:
            JSON string containing a LIST of candidate FAQ entries (question, answer, answerId, confidence), sorted/scored by relevance. IMPORTANT: a list of several entries is NOT permission to use several entries. The list is a candidate set from which you pick EXACTLY ONE (highest confidence) per the ANSWER SELECTION rules; all other entries are decoys to be ignored, never a source to merge from.
            Results may be in any language and should be translated to the customer's language before presenting.
            Returns an empty list if no match is found.

        POST-CALL: After a SUCCESSFUL tool call, append the questionFinish placeholder ({{$.Custom.questionFinish}}) at the end of your customer-facing message, separated by one blank line. Do NOT append it if the call failed or returned an error. Appending the questionFinish placeholder counts as your single question for that turn.
        """

        query = (query or "").strip()
        kb_key = (kb_key or "").strip()
        contactId = (contactId or "").strip()
        categories = list(dict.fromkeys(productCategory or []))
        if categories and "Common" not in categories:
            categories.append("Common")

        api_result = await call_api(
            faq_url,
            {
                "query": query,
                "kb_key": kb_key,
                "product_categorys": ",".join(categories),
                "mode": "search",
                "top_k": 5,
                "threshold_score": 0.6,
            },
        )

        retrieved_contexts = api_result.get("retrieved_contexts", [])

        if contactId:
            session_client.update(
                contactId,
                {
                    "last_faq_query": query,
                    "last_faq_retrieved_contexts": json.dumps(
                        retrieved_contexts, ensure_ascii=False
                    ),
                },
            )
        result = []
        if retrieved_contexts:
            result = [
                {
                    "question": item.get("question"),
                    "answer": item.get("answer"),
                    "answer_id": item.get("id"),
                    "confidence": item.get("similarity"),
                }
                for item in retrieved_contexts
            ]

        result = json.dumps(result, ensure_ascii=False)

        return result
