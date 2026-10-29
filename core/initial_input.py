"""Online LLM entry for initial planning facts and constraints."""
from __future__ import annotations
import os
import sys
from utils import REQUEST_AGENT
from input_completion import complete_request
from llm_interpreter import interpret as interpret_with_llm
from llm_interpreter import interpret_field_reply


def complete_initial_information(request):
    """Interpret the request with the LLM and complete its supported fields."""
    if "--local" in sys.argv:
        raise ValueError("Explicit --local runs must use local_input.complete_local_information")
    if REQUEST_AGENT.get() is None and not os.environ.get("OPENROUTER_API_KEY", "").strip():
        raise RuntimeError("OPENROUTER_API_KEY is required for the LLM entry; use --local for historical offline parsing")
    return complete_request(request, interpret_with_llm(request["text"]),
                            reply_interpreter=interpret_field_reply)
