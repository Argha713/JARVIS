"""
JARVIS provider setup.

Run this before first use, or any time you want to change the LLM provider:
    python setup.py
"""
import json
import os

CONFIG_PATH = "config.json"


def _load_config() -> dict:
    if not os.path.exists(CONFIG_PATH):
        print(f"Error: {CONFIG_PATH} not found. Run this from the JARVIS project root.")
        raise SystemExit(1)
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)


def _save_config(config: dict) -> None:
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=4)


def _mask_key(key: str) -> str:
    if len(key) > 12:
        return key[:8] + "..." + key[-4:]
    return "***" if key else "(not set)"


def run_setup() -> None:
    config = _load_config()
    llm = config.setdefault("llm", {})
    current_provider = llm.get("provider", "ollama")

    print("\n╔══════════════════════════════════════╗")
    print("║      JARVIS — LLM Provider Setup     ║")
    print("╚══════════════════════════════════════╝")
    print(f"\nCurrent provider: {current_provider.upper()}\n")
    print("Choose your LLM provider:")
    print("  [1] Ollama  — local, free, no internet needed")
    print("  [2] OpenAI  — cloud, requires an API key")
    print("  [3] Cancel  — keep current settings")

    while True:
        choice = input("\nEnter 1, 2, or 3: ").strip()
        if choice in ("1", "2", "3"):
            break
        print("Please enter 1, 2, or 3.")

    if choice == "3":
        print("\nNo changes made.")
        return

    if choice == "1":
        _setup_ollama(llm)
    else:
        _setup_openai(llm)

    _save_config(config)
    print(f"\n✓ config.json saved.")
    print("Restart JARVIS (or say 'switch to Ollama/OpenAI') to apply.\n")


def _setup_ollama(llm: dict) -> None:
    current_host  = llm.get("ollama_host", "http://localhost:11434")
    current_fast  = llm.get("fast_model", "phi3")
    current_smart = llm.get("smart_model", "llama3.1:8b")

    print("\n── Ollama Setup ──")
    host = input(f"Ollama host [{current_host}]: ").strip() or current_host
    fast = input(f"Fast model  [{current_fast}]: ").strip()  or current_fast
    smart = input(f"Smart model [{current_smart}]: ").strip() or current_smart

    llm["provider"]    = "ollama"
    llm["ollama_host"] = host
    llm["fast_model"]  = fast
    llm["smart_model"] = smart

    print(f"\n✓ Provider : Ollama")
    print(f"  Host     : {host}")
    print(f"  Fast     : {fast}")
    print(f"  Smart    : {smart}")


def _setup_openai(llm: dict) -> None:
    current_key   = llm.get("openai_api_key", "")
    current_fast  = llm.get("openai_fast_model", "gpt-4o-mini")
    current_smart = llm.get("openai_smart_model", "gpt-4o")

    print("\n── OpenAI Setup ──")

    if current_key:
        prompt = f"API key [{_mask_key(current_key)}, press Enter to keep]: "
        key = input(prompt).strip() or current_key
    else:
        print("You need an OpenAI API key (starts with sk-).")
        print("Get one at: https://platform.openai.com/api-keys")
        key = input("API key: ").strip()
        if not key:
            print("API key is required. Aborting.")
            return

    fast  = input(f"Fast model  [{current_fast}]: ").strip()  or current_fast
    smart = input(f"Smart model [{current_smart}]: ").strip() or current_smart

    llm["provider"]           = "openai"
    llm["openai_api_key"]     = key
    llm["openai_fast_model"]  = fast
    llm["openai_smart_model"] = smart

    print(f"\n✓ Provider : OpenAI")
    print(f"  API key  : {_mask_key(key)}")
    print(f"  Fast     : {fast}")
    print(f"  Smart    : {smart}")


if __name__ == "__main__":
    run_setup()
