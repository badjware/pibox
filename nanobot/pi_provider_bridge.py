#!/usr/bin/env python3
"""Build a nanobot config from explicitly configured pi providers."""

import json
import os
import re
import subprocess
import sys
from pathlib import Path


PI_PROVIDER_MAP = {
    "amazon-bedrock": "bedrock",
    "anthropic": "anthropic",
    "azure-openai-responses": "azure_openai",
    "deepseek": "deepseek",
    "google": "gemini",
    "groq": "groq",
    "huggingface": "huggingface",
    "minimax": "minimax",
    "mistral": "mistral",
    "moonshotai": "moonshot",
    "nvidia": "nvidia",
    "ollama": "ollama",
    "openai": "openai",
    "openrouter": "openrouter",
    "vllm": "vllm",
}


def error(message: str) -> None:
    print(f"pibox pi provider bridge: {message}", file=sys.stderr)
    raise SystemExit(1)


def command(*args: str) -> str:
    result = subprocess.run(args, capture_output=True, text=True, check=False)
    if result.returncode:
        detail = result.stderr.strip() or result.stdout.strip() or "command failed"
        raise RuntimeError(detail)
    return result.stdout


def load_json(path: Path, default: object) -> object:
    if not path.exists():
        return default
    try:
        with path.open(encoding="utf-8") as file:
            return json.load(file)
    except (OSError, json.JSONDecodeError) as exc:
        error(f"cannot read {path}: {exc}")


def parse_models() -> dict[str, list[str]]:
    output = command("pi", "--offline", "--list-models")
    lines = output.splitlines()
    if not lines or not lines[0].lstrip().startswith("provider"):
        raise RuntimeError("unexpected pi --list-models output")

    models: dict[str, list[str]] = {}
    for line in lines[1:]:
        fields = re.split(r" {2,}", line.strip())
        if len(fields) != 6:
            raise RuntimeError("unexpected pi --list-models row")
        provider, model = fields[:2]
        models.setdefault(provider, []).append(model)
    return models


def resolve_value(value: str) -> str:
    if value.startswith("!"):
        result = subprocess.run(value[1:], shell=True, capture_output=True, text=True, check=False)
        if result.returncode:
            raise RuntimeError("command-based value resolution failed")
        return result.stdout.strip()

    def replace(match: re.Match[str]) -> str:
        name = match.group(1) or match.group(2)
        if name not in os.environ:
            raise RuntimeError(f"environment variable {name} is not set")
        return os.environ[name]

    return re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)", replace, value)


def provider_config(
    name: str,
    config: dict[str, object],
    api_key: str,
    api: object,
) -> tuple[str, dict[str, object]]:
    if api == "anthropic-messages":
        mapped = "anthropic"
    elif api == "google-generative-ai":
        mapped = "gemini"
    elif api == "openai-responses":
        mapped = "openai"
    else:
        mapped = PI_PROVIDER_MAP.get(name)
        if mapped is None:
            if api != "openai-completions":
                raise RuntimeError("nanobot has no compatible provider adapter")
            mapped = f"pi_{name.replace('-', '_')}"

    result: dict[str, object] = {"apiKey": api_key}
    if mapped == "openai" and api != "openai-completions":
        result["apiType"] = "responses"
    if isinstance(config.get("baseUrl"), str):
        result["apiBase"] = resolve_value(config["baseUrl"])
    if isinstance(config.get("headers"), dict):
        headers: dict[str, str] = {}
        for key, value in config["headers"].items():
            if not isinstance(key, str) or not isinstance(value, str):
                raise RuntimeError("provider headers must be strings")
            headers[key] = resolve_value(value)
        result["extraHeaders"] = headers
    return mapped, result


def get_api_key(provider: str) -> str:
    status = json.loads(command("pi", "auth", "check", "--provider", provider, "--json"))
    if status.get("status") != "ready":
        raise RuntimeError("provider authentication is not ready")
    if status.get("authType") != "api_key":
        raise RuntimeError("OAuth providers are not supported")
    return command("pi", "auth", "print-api-key", "--provider", provider).strip()


def main() -> None:
    home = Path.home()
    pi_dir = home / ".pi" / "agent"
    nanobot_dir = home / ".nanobot"
    configured = load_json(pi_dir / "models.json", {})
    if not isinstance(configured, dict) or not isinstance(configured.get("providers", {}), dict):
        error("models.json must contain a providers object")
    provider_definitions = configured["providers"]
    available = parse_models()

    template = load_json(nanobot_dir / "config.json", {})
    if not isinstance(template, dict):
        error("nanobot config.json must contain an object")
    generated = dict(template)
    generated.pop("providers", None)
    generated.pop("modelPresets", None)

    providers: dict[str, object] = {}
    presets: dict[str, object] = {}
    skipped: list[str] = []
    preset_by_pi_model: dict[tuple[str, str], str] = {}

    provider_names = list(provider_definitions)
    provider_names.extend(sorted(set(available) - set(provider_definitions)))
    for name in provider_names:
        definition = provider_definitions.get(name, {})
        if not isinstance(name, str) or not isinstance(definition, dict):
            skipped.append(f"{name}: invalid provider definition")
            continue
        if name not in PI_PROVIDER_MAP and name not in provider_definitions:
            skipped.append(f"{name}: nanobot has no compatible provider adapter")
            continue
        models = available.get(name, [])
        if not models:
            skipped.append(f"{name}: no available pi models")
            continue
        try:
            api_key = get_api_key(name)
        except (RuntimeError, json.JSONDecodeError) as exc:
            skipped.append(f"{name}: {exc}")
            continue
        model_apis = {
            model["id"]: model.get("api", definition.get("api"))
            for model in definition.get("models", [])
            if isinstance(model, dict) and isinstance(model.get("id"), str)
        }
        for model in models:
            try:
                nanobot_provider, config = provider_config(
                    name, definition, api_key, model_apis.get(model, definition.get("api"))
                )
            except RuntimeError as exc:
                skipped.append(f"{name}/{model}: {exc}")
                continue
            existing = providers.get(nanobot_provider)
            if existing is not None and existing != config:
                skipped.append(f"{name}/{model}: maps to duplicate nanobot provider {nanobot_provider}")
                continue
            providers[nanobot_provider] = config
            preset = f"{name}/{model}"
            presets[preset] = {"provider": nanobot_provider, "model": model}
            preset_by_pi_model[(name, model)] = preset

    if not presets:
        details = "; ".join(skipped) or "no explicitly configured pi providers"
        error(f"no usable models found: {details}")

    settings = load_json(pi_dir / "settings.json", {})
    if not isinstance(settings, dict):
        error("pi settings.json must contain an object")
    selected = (settings.get("defaultProvider"), settings.get("defaultModel"))
    preset = preset_by_pi_model.get(selected)
    if preset is None:
        error("pi default model is unavailable or unsupported")

    agents = generated.get("agents", {})
    if not isinstance(agents, dict):
        error("nanobot agents setting must contain an object")
    defaults = agents.get("defaults", {})
    if not isinstance(defaults, dict):
        error("nanobot agents.defaults setting must contain an object")
    defaults = dict(defaults)
    for key in (
        "modelPreset",
        "model",
        "provider",
        "maxTokens",
        "contextWindowTokens",
        "temperature",
        "reasoningEffort",
        "fallbackModels",
    ):
        defaults.pop(key, None)
    defaults["modelPreset"] = preset
    defaults["fallbackModels"] = [name for name in presets if name != preset]
    agents = dict(agents)
    agents["defaults"] = defaults
    generated["agents"] = agents
    generated["providers"] = providers
    generated["modelPresets"] = presets

    nanobot_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = nanobot_dir / "pibox-config.json"
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(generated, indent=2) + "\n", encoding="utf-8")
    temporary.chmod(0o600)
    temporary.replace(path)
    for message in skipped:
        print(f"pibox pi provider bridge: skipped {message}", file=sys.stderr)


if __name__ == "__main__":
    main()
