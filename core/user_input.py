"""Step 1: get the API key, solver choice, and planning request."""
import getpass
import os
import sys
from urllib.request import Request, urlopen


def verify_api_key(key):
    request = Request("https://openrouter.ai/api/v1/key",
                      headers={"Authorization": f"Bearer {key}"})
    with urlopen(request, timeout=10) as response:
        return response.status == 200


def read_user_input():
    if "--local" not in sys.argv and not os.environ.get("OPENROUTER_API_KEY", "").strip():
        try:
            key = getpass.getpass("OPENROUTER_API_KEY (use --local for offline parsing): ").strip()
        except EOFError:
            key = ""
        if key:
            os.environ["OPENROUTER_API_KEY"] = key
    if "--local" not in sys.argv and os.environ.get("OPENROUTER_API_KEY", "").strip():
        try:
            if not verify_api_key(os.environ["OPENROUTER_API_KEY"]):
                raise RuntimeError("OpenRouter API key verification returned an unsuccessful status")
            print("OpenRouter API key verified.")
        except Exception:
            raise RuntimeError("OpenRouter API key could not be verified; use --local explicitly for offline parsing")
    if "--local" not in sys.argv and not os.environ.get("OPENROUTER_API_KEY", "").strip():
        raise RuntimeError("OPENROUTER_API_KEY is required; use --local explicitly for offline parsing")
    backend = "copt" if "--copt" in sys.argv else "scipy"
    print("Describe the known site conditions and planning priorities in one text entry.")
    return {"text": input("> ").strip(), "backend": backend}
