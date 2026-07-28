#!/usr/local/lib/hermes-agent/venv/bin/python
"""Publish Pi's configured models through Hermes managed scope."""

import argparse
import json
import os
import re
import sys
from pathlib import Path

import yaml

PI_PROVIDER_PREFIX = "pi-"
API_MODES = {
    "anthropic-messages": "anthropic_messages",
    "openai-completions": "chat_completions",
    "openai-responses": "codex_responses",
}
ENV_REFERENCE = re.compile(
    r"\$(?:\{(?P<braced>[A-Za-z_][A-Za-z0-9_]*)\}|(?P<plain>[A-Za-z_][A-Za-z0-9_]*))"
)


def load_json(path: Path, *, missing_ok: bool = False) -> dict:
    try:
        value = json.loads(path.read_text())
    except FileNotFoundError:
        if missing_ok:
            return {}
        print(
            f"hermes: cannot import Pi model configuration: {path} does not exist",
            file=sys.stderr,
        )
        return {}
    except (OSError, json.JSONDecodeError) as error:
        print(f"hermes: cannot import Pi model configuration: {error}", file=sys.stderr)
        return {}
    return value if isinstance(value, dict) else {}


def model_metadata(model: dict) -> dict:
    metadata = {}
    context_length = model.get("contextWindow")
    if isinstance(context_length, int) and context_length > 0:
        metadata["context_length"] = context_length
    return metadata


def normalize_env_references(value: str) -> str:
    return ENV_REFERENCE.sub(
        lambda match: f"${{{match.group('braced') or match.group('plain')}}}",
        value,
    )


def credential_name(provider: str, index: int, suffix: str) -> str:
    provider = re.sub(r"[^A-Za-z0-9]+", "_", provider).strip("_").upper()
    suffix = re.sub(r"[^A-Za-z0-9]+", "_", suffix).strip("_").upper()
    return f"PIBOX_PI_{provider}_{index}_{suffix}"


def managed_credential(value: str, name: str, environment: dict) -> str:
    if ENV_REFERENCE.search(value):

        def replace(match):
            variable = match.group("braced") or match.group("plain")
            if variable not in os.environ:
                return match.group(0)
            environment[variable] = os.environ[variable]
            return os.environ[variable]

        return ENV_REFERENCE.sub(replace, value)
    environment[name] = value
    return value


def sensitive_header(name: str) -> bool:
    name = name.lower()
    return (
        name == "authorization" or "token" in name or "key" in name or "secret" in name
    )


def models_json_catalog(data: dict) -> dict:
    catalog = {}
    for provider, entry in data.get("providers", {}).items():
        if not isinstance(provider, str) or not isinstance(entry, dict):
            continue
        api = entry.get("api")
        base_url = entry.get("baseUrl")
        models = entry.get("models", [])
        if (
            not isinstance(api, str)
            or not isinstance(base_url, str)
            or not isinstance(models, list)
        ):
            continue
        catalog[provider] = {
            "models": [
                {
                    **model,
                    "api": model.get("api") or api,
                    "baseUrl": base_url,
                    "apiKey": entry.get("apiKey"),
                    "headers": entry.get("headers"),
                }
                for model in models
                if isinstance(model, dict)
            ]
        }
    return catalog


def import_providers(catalog: dict) -> tuple[dict, dict]:
    providers = {}
    environment = {}
    for provider, entry in catalog.items():
        if not isinstance(provider, str) or not isinstance(entry, dict):
            continue
        groups = {}
        for model in entry.get("models", []):
            if not isinstance(model, dict):
                continue
            api_type = model.get("api")
            api_mode = API_MODES.get(api_type)
            model_id = model.get("id")
            base_url = model.get("baseUrl")
            if (
                not api_mode
                or not isinstance(model_id, str)
                or not isinstance(base_url, str)
            ):
                continue
            headers = model.get("headers")
            key = (
                api_type,
                api_mode,
                base_url,
                json.dumps(headers, sort_keys=True)
                if isinstance(headers, dict)
                else "",
                model.get("apiKey") if isinstance(model.get("apiKey"), str) else "",
            )
            groups.setdefault(key, []).append(model)

        name_counts = {}
        for index, ((api_type, api_mode, base_url, _, api_key), models) in enumerate(
            groups.items(), start=1
        ):
            base_name = f"{PI_PROVIDER_PREFIX}{provider}-{api_type}"
            name_counts[base_name] = name_counts.get(base_name, 0) + 1
            collision = name_counts[base_name]
            name = base_name if collision == 1 else f"{base_name}-{collision}"
            imported = {
                "name": name,
                "base_url": base_url,
                "api_mode": api_mode,
                "models": {model["id"]: model_metadata(model) for model in models},
            }
            if api_key:
                imported["api_key"] = managed_credential(
                    api_key,
                    credential_name(provider, index, "api_key"),
                    environment,
                )
            headers = models[0].get("headers")
            if isinstance(headers, dict):
                extra_headers = {}
                for key, value in headers.items():
                    if not isinstance(key, str) or value is None:
                        continue
                    value = str(value)
                    if sensitive_header(key) or ENV_REFERENCE.search(value):
                        value = managed_credential(
                            value,
                            credential_name(provider, index, f"header_{key}"),
                            environment,
                        )
                    extra_headers[key] = value
                if extra_headers:
                    imported["extra_headers"] = extra_headers
            providers[name] = imported
    return providers, environment


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--home", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--env-output", type=Path, required=True)
    args = parser.parse_args()

    home = args.home.expanduser()
    agent_dir = home / ".pi" / "agent"
    models_json = load_json(agent_dir / "models.json", missing_ok=True)
    catalog = models_json_catalog(models_json)
    if not catalog:
        args.output.unlink(missing_ok=True)
        args.env_output.unlink(missing_ok=True)
        return 0

    imported, environment = import_providers(catalog)
    args.output.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    args.output.write_text(
        yaml.safe_dump(
            {"providers": imported}, default_flow_style=False, sort_keys=False
        )
    )
    args.output.chmod(0o644)
    args.env_output.write_text(
        "".join(
            f"{name}={json.dumps(value)}\n"
            for name, value in sorted(environment.items())
        )
    )
    args.env_output.chmod(0o644)
    print(
        f"hermes: published {len(imported)} Pi provider entries and {len(environment)} credentials to managed scope",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
