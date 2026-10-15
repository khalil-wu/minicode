"""Self-contained MCP standard form schema (MCP 2025-11-25).

Codex's McpElicitationSchema uses the same closed object/primitive contract.
Schema references and arbitrary JSON Schema composition are not form fields.
"""

from typing import Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field


class _FormModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class _Field(_FormModel):
    title: str | None = None
    description: str | None = None


class _String(_Field):
    type: Literal["string"]
    minLength: int | None = Field(default=None, ge=0)
    maxLength: int | None = Field(default=None, ge=0)
    format: Literal["email", "uri", "date", "date-time"] | None = None
    default: str | None = None


class _StringEnum(_Field):
    type: Literal["string"]
    enum: list[str]
    enumNames: list[str] | None = None
    default: str | None = None


class _Option(_FormModel):
    const: str
    title: str


class _TitledStringEnum(_Field):
    type: Literal["string"]
    oneOf: list[_Option]
    default: str | None = None


class _Number(_Field):
    type: Literal["number", "integer"]
    minimum: float | None = None
    maximum: float | None = None
    default: int | float | None = None


class _Boolean(_Field):
    type: Literal["boolean"]
    default: bool | None = None


class _EnumItems(_FormModel):
    type: Literal["string"]
    enum: list[str]


class _TitledEnumItems(_FormModel):
    anyOf: list[_Option] = Field(validation_alias=AliasChoices("anyOf", "oneOf"))


class _MultiSelect(_Field):
    type: Literal["array"]
    items: _EnumItems | _TitledEnumItems
    minItems: int | None = Field(default=None, ge=0)
    maxItems: int | None = Field(default=None, ge=0)
    default: list[str] | None = None


class ElicitationFormSchema(_FormModel):
    schema_uri: str | None = Field(default=None, alias="$schema")
    type: Literal["object"]
    properties: dict[str, _String | _StringEnum | _TitledStringEnum | _Number | _Boolean | _MultiSelect]
    required: list[str] | None = None
