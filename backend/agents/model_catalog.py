"""Source's bounded spawn choices, captured once for each receiving step."""

from __future__ import annotations

from dataclasses import dataclass
from itertools import islice
from xml.sax.saxutils import escape

from backend.llm.model_catalog import responses_model_catalog_entry
from backend.llm.model_request_policy import source_model_request_capabilities


MODEL_CATALOG_KIND = "multi_agent.model_catalog"
MAX_SPAWN_MODEL_CHOICES = 5
_OPEN, _CLOSE = "<model_catalog>", "</model_catalog>"
_OMITTED = "Additional model choices omitted.\n"
_EMPTY = "No picker-visible model overrides are currently loaded."
INVALIDATED_CATALOG = _OPEN + "\nThe previous spawn_agent model catalog no longer applies.\n" + _CLOSE


@dataclass(frozen=True)
class SpawnModelChoice:
    model: str
    description: str
    reasoning_efforts: tuple[str, ...]
    default_reasoning_effort: str
    service_tiers: tuple[str, ...]

    def render(self, *, in_context: bool) -> str:
        description = self.description
        if in_context and len(description) > 250:
            description = description[:250] + "…"
        line = f"- `{self.model}`: {description}"
        if self.reasoning_efforts:
            efforts = [
                (effort if in_context else effort[:64])
                + (" (default)" if effort == self.default_reasoning_effort else "")
                for effort in self.reasoning_efforts
            ]
            line += " Reasoning efforts: " + ", ".join(efforts) + "."
        if self.service_tiers:
            line += " Service tiers: " + ", ".join(self.service_tiers) + "."
        return line


def eligible_spawn_models(models, version: str):
    """Preserve picker order while applying the actual child-backend constraint."""
    for model in models:
        metadata = source_model_request_capabilities(model.id, model)
        if not metadata.get("show_in_picker", metadata.get("visibility", "list") == "list"):
            continue
        if version == "v2" and metadata.get("multi_agent_version") == "disabled":
            continue
        yield model


@dataclass(frozen=True)
class SpawnModelCatalog:
    choices: tuple[SpawnModelChoice, ...]
    version: str
    namespace: str
    in_context: bool
    hide_metadata: bool
    expose_overrides: bool = True

    @property
    def tool_name(self) -> str:
        return "spawn_agent" if self.version == "v2" else "task"

    def schema_guidance(self) -> str:
        if not self.expose_overrides:
            return ""
        if self.in_context:
            inherited = (
                "Spawned agents inherit your current model by default. Do not set the `model` field unless the user explicitly asks for a different model."
                if self.version == "v2" else
                "Spawned agents inherit your current model by default. Omit `model` to use that preferred default; set `model` only when an explicit override is needed."
            )
            return inherited + "\nPick model overrides from the latest <model_catalog> listing."
        listing = ("Available model overrides (optional; inherited parent model is preferred):\n"
            + "\n".join(choice.render(in_context=False) for choice in self.choices)) if self.choices else _EMPTY
        if self.version == "v1" or not self.hide_metadata:
            listing += "\nSpawned agents inherit your current model by default. Omit `model` to use that preferred default; set `model` only when an explicit override is needed."
        return listing

    def fragment(self) -> str:
        body = "" if self.choices else _EMPTY + "\n"
        budget = 1000 - len(_OPEN) - len(_CLOSE) - 2
        for choice in sorted(self.choices, key=lambda choice: choice.model):
            line = escape(choice.render(in_context=True), {'"': "&quot;", "'": "&apos;"})
            if len(body.encode("utf-8")) + len(line.encode("utf-8")) + 1 + len(_OMITTED) > budget:
                body += _OMITTED
                break
            body += line + "\n"
        return _OPEN + "\n" + body + _CLOSE


def capture_spawn_model_catalog(execution, options) -> SpawnModelCatalog | None:
    if execution is None or not options["enabled"]:
        return None
    version = options["version"]
    if execution.model_runtime is not None:
        models = execution.model_runtime.get_models(execution.provider)
    else:
        from backend.llm.provider_contracts import ModelDefinition

        models = tuple(execution.model_info if execution.model_info is not None and model == execution.model_info.id
            else ModelDefinition(provider=execution.provider, id=model, name=model, api="", base_url="")
            for model in execution.available_models)
    choices = []
    for model in islice(eligible_spawn_models(models, version), MAX_SPAWN_MODEL_CHOICES):
        metadata = source_model_request_capabilities(model.id, model)
        source = responses_model_catalog_entry(model.id) if model.source_model_metadata is not None else None
        choices.append(SpawnModelChoice(model.id, metadata.get("description") or "",
            tuple(model.reasoning_effort_levels or (source.reasoning_effort_levels if source is not None else ())),
            model.default_reasoning_effort or (source.default_reasoning_effort if source is not None else ""),
            tuple(tier["id"] for tier in metadata["service_tiers"])))
    return SpawnModelCatalog(tuple(choices), version,
        options["tool_namespace"] if version == "v2" else "functions",
        execution.config.feature_flags.enabled("model_catalog_in_context"), options["hide_spawn_agent_metadata"],
        version == "v1" or options["expose_spawn_agent_model_overrides"])
