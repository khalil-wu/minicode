"""Registered MCP App declarations and resource data, independent of tool output."""
from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit


MCP_APP_MIME_TYPES = ("text/html;profile=mcp-app", "text/html+skybridge")
MCP_APP_EXTENSION = "io.modelcontextprotocol/ui"


class MCPAppError(ValueError):
    def __init__(self, message: str, *, code: str = "app_unavailable", status: int = 409):
        super().__init__(message)
        self.code, self.status = code, status


@dataclass(frozen=True, slots=True)
class MCPAppDeclaration:
    resource_uri: str
    preferred_model_display_mode: str | None = None

    def public_fields(self) -> dict[str, Any]:
        fields: dict[str, Any] = {"mcp_app_resource_uri": self.resource_uri}
        if self.preferred_model_display_mode is not None:
            fields["mcp_app_ui"] = {"resourceUri": self.resource_uri,
                "preferredModelDisplayMode": self.preferred_model_display_mode}
        return fields


def registered_app_declaration(meta: dict[str, Any]) -> MCPAppDeclaration | None:
    """mcp_tool_call.rs::get_mcp_app_resource_uri, from registered Tool.meta only."""
    ui = meta.get("ui")
    candidates = (ui.get("resourceUri") if isinstance(ui, dict) else None,
        meta.get("ui/resourceUri"), meta.get("openai/outputTemplate"))
    uri = next((value for value in candidates if isinstance(value, str)), None)
    if uri is None:
        return None
    preference = meta.get("openai/ui")
    mode = preference.get("preferredModelDisplayMode") if isinstance(preference, dict) else None
    return MCPAppDeclaration(uri, mode if mode in {"inline", "fullscreen"} else None)


def app_can_call_tool(meta: dict[str, Any]) -> bool:
    ui = meta.get("ui")
    visibility = ui.get("visibility") if isinstance(ui, dict) else None
    return not isinstance(visibility, list) or "app" in visibility


def is_app_sandbox_hostname(hostname: str | None) -> bool:
    return bool(hostname and hostname.startswith("mcp-app-") and hostname.endswith(".localhost"))


def is_app_sandbox_origin(origin: str | None) -> bool:
    return is_app_sandbox_hostname(urlsplit(origin).hostname) if origin else False


@dataclass(frozen=True, slots=True)
class MCPAppResource:
    uri: str
    html: str
    mime_type: str
    csp: dict[str, list[str]]
    permissions: dict[str, Any]
    prefers_border: bool


def parse_app_resource(result: dict[str, Any], uri: str) -> MCPAppResource:
    """Parse the resource boundary once; arbitrary tool-result HTML is never read here."""
    for content in result.get("contents", []):
        mime = str(content.get("mimeType") or "").replace(" ", "").lower()
        if content.get("uri") != uri or mime not in MCP_APP_MIME_TYPES:
            continue
        html = content.get("text")
        if html is None and isinstance(content.get("blob"), str):
            html = base64.b64decode(content["blob"], validate=True).decode("utf-8")
        if not isinstance(html, str):
            raise MCPAppError("MCP App resource has no HTML body", code="invalid_resource")
        metadata = content.get("_meta") or {}
        ui = metadata.get("ui") or {}
        if not isinstance(ui, dict):
            raise MCPAppError("Invalid MCP App resource metadata", code="invalid_resource")
        csp = ui.get("csp") or {}
        legacy = metadata.get("openai/widgetCSP") or {}
        if not all(isinstance(value, dict) for value in (csp, legacy)):
            raise MCPAppError("Invalid MCP App resource metadata", code="invalid_resource")
        rules: dict[str, list[str]] = {}
        for current, previous in (("connectDomains", "connect_domains"), ("resourceDomains", "resource_domains"),
                ("frameDomains", "frame_domains"), ("baseUriDomains", "base_uri_domains")):
            domains = csp.get(current, legacy.get(previous, []))
            if not isinstance(domains, list) or any(not isinstance(domain, str) for domain in domains):
                raise MCPAppError(f"Invalid MCP App CSP {current}", code="invalid_resource")
            for domain in domains:
                parsed = urlsplit(domain.replace("*.", "wildcard."))
                if parsed.scheme not in {"http", "https", "ws", "wss"} or not parsed.hostname or any(
                        character.isspace() or character in ";'\"" for character in domain):
                    raise MCPAppError(f"Invalid MCP App CSP origin: {domain}", code="invalid_resource")
            rules[current] = domains
        return MCPAppResource(uri, html, mime, rules, ui.get("permissions") or {},
            bool(ui.get("prefersBorder", metadata.get("openai/widgetPrefersBorder", False))))
    raise MCPAppError("MCP App resource is unavailable or has an unsupported MIME type", code="unsupported_resource")


def resource_csp(resource: MCPAppResource) -> str:
    resources = " ".join(resource.csp["resourceDomains"])
    connections = " ".join(resource.csp["connectDomains"]) or "'none'"
    frames = " ".join(resource.csp["frameDomains"]) or "'none'"
    base = " ".join(resource.csp["baseUriDomains"]) or "'none'"
    return "; ".join(("default-src 'none'", "object-src 'none'", f"base-uri {base}",
        f"script-src 'self' 'unsafe-inline' 'unsafe-eval' blob: {resources}",
        f"style-src 'self' 'unsafe-inline' {resources}", f"img-src data: blob: {resources}",
        f"font-src data: {resources}", f"media-src data: blob: {resources}",
        f"connect-src {connections}", f"frame-src {frames}", "form-action 'none'"))
