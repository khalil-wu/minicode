"""Executor-owned Skill catalogs and reads in the selected URI domain."""
from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

import yaml

from backend.environments import ExecutorError, ExecutorProtocolError
from backend.environments.client import _retryable
from backend.skills.frontmatter import parse_skill_frontmatter
from backend.skills.errors import RequiredSkillUnavailableError
from backend.workspace.executor_paths import ExecutorPath

MAX_RESOURCE_BYTES = 1024 * 1024
MAX_RESPONSE_BYTES = 512 * 1024
MAX_ROOTS_PER_REQUEST = 128
WALK_OPTIONS = {"maxDepth": 6, "maxDirectories": 2000, "maxEntries": 20000,
    "followDirectorySymlinks": True, "pruneHiddenDirectories": False}
PLUGIN_MANIFESTS = (".codex-plugin/plugin.json", ".claude-plugin/plugin.json", ".cursor-plugin/plugin.json")


@dataclass(frozen=True)
class ExecutorCapabilityRoot:
    id: str
    environment_id: str
    path: str
    environment: object
    sandbox: dict | None
    platform_os: str | None
    high_level: bool
    requires_sandbox: bool

    def uri(self, value: str) -> ExecutorPath:
        return ExecutorPath.resolve(value, cwd_uri=self.path, platform_os=self.platform_os)


@dataclass(frozen=True)
class ExecutorSkillEntry:
    authority_id: str
    environment_id: str
    name: str
    description: str
    package: str
    main_resource: str
    main_path: str
    alias_root: str
    enabled: bool
    allow_implicit_invocation: bool
    short_description: str | None = None
    instructions: str | None = None
    dependencies: tuple[dict, ...] = ()


@dataclass(frozen=True)
class ExecutorRootDiscovery:
    root: ExecutorCapabilityRoot
    bundle: dict
    retryable: bool = False


async def _read_text(root: ExecutorCapabilityRoot, path: str) -> str:
    contents = bytearray()
    handle = await root.environment.filesystem.open(path, sandbox=root.sandbox, mode="read")
    async with handle:
        async for chunk in handle.chunks():
            if len(contents) + len(chunk) > MAX_RESOURCE_BYTES:
                raise ValueError(f"executor skill resource {path} exceeds {MAX_RESOURCE_BYTES} bytes")
            contents.extend(chunk)
    return contents.decode("utf-8")


async def _optional_text(root, path, warnings):
    try:
        return {"path": path, "contents": await _read_text(root, path)}
    except (ExecutorError, OSError, ValueError) as error:
        warnings.append(f"failed to read capability file {path}: {error}")
        return None


async def _walk_bundle(root: ExecutorCapabilityRoot) -> dict:
    filesystem = root.environment.filesystem
    walk = await filesystem.walk(root.path, WALK_OPTIONS, sandbox=root.sandbox)
    warnings = [f"failed to scan skill path {entry['path']}: {entry['message']}" for entry in walk["errors"]]
    if walk["truncated"]:
        warnings.append(f"skills scan reached its traversal limit (root: {root.path})")
    files = {entry["path"] for entry in walk["entries"] if entry["kind"] == "file"}
    complete = not walk["truncated"] and not walk["errors"]
    skills, namespaces = [], []
    reads = asyncio.Semaphore(64)

    async def load(path):
        async with reads:
            try:
                instructions = {"path": path, "contents": await _read_text(root, path)}
            except (ExecutorError, OSError, ValueError) as error:
                warnings.append(f"Failed to load environment skill at {path}: {error}")
                return None
            metadata_path = root.uri(path).parent.joinpath("agents/openai.yaml").uri
            metadata = (await _optional_text(root, metadata_path, warnings)
                if metadata_path in files or not complete else None)
            return {"instructions": instructions, "metadata": metadata}

    paths = sorted(path for path in files if root.uri(path).name == "SKILL.md")
    skills = [result for result in await asyncio.gather(*(load(path) for path in paths)) if result is not None]
    manifests = sorted(path for path in files if any(path.endswith("/" + suffix) for suffix in PLUGIN_MANIFESTS))
    for path in manifests:
        manifest = await _optional_text(root, path, warnings)
        if manifest is not None:
            namespaces.append(manifest)
    # The Source namespace resolver also probes ancestors of a selected skills directory.
    ancestor = root.uri(root.path)
    seen = {manifest["path"] for manifest in namespaces}
    while True:
        found = False
        for suffix in PLUGIN_MANIFESTS:
            path = ancestor.joinpath(suffix).uri
            if path in seen:
                found = True
                break
            try:
                metadata = await filesystem.get_metadata(path, sandbox=root.sandbox)
            except (ExecutorError, OSError):
                continue
            if metadata["isFile"]:
                manifest = await _optional_text(root, path, warnings)
                if manifest is not None:
                    namespaces.insert(0, manifest)
                    found = True
                    break
        if found or ancestor.parent.uri == ancestor.uri:
            break
        ancestor = ancestor.parent
    plugin = None
    manifest = next((file for file in namespaces
        if root.uri(file["path"]).parent.parent.uri == root.path), None)
    if manifest is not None:
        try:
            metadata = json.loads(manifest["contents"])
            declaration = metadata.get("mcpServers")
            mcp_path = None if isinstance(declaration, dict) else root.uri(root.path).joinpath(declaration or ".mcp.json")
            if mcp_path is not None and not mcp_path.is_relative_to(root.uri(root.path)):
                raise ValueError("plugin MCP path must remain within plugin root")
            mcp_config = None
            if mcp_path is not None:
                try:
                    status = await filesystem.get_metadata(mcp_path.uri, sandbox=root.sandbox)
                except (ExecutorError, OSError):
                    status = None
                if status is not None and status["isFile"]:
                    mcp_config = await _optional_text(root, mcp_path.uri, warnings)
            plugin = {"manifest": manifest, "mcpConfig": mcp_config}
        except ValueError as error:
            warnings.append(f"failed to parse plugin manifest {manifest['path']}: {error}")
    return {"id": root.id, "path": root.path, "skills": skills,
        "namespaceManifests": namespaces, "plugin": plugin, "warnings": warnings, "error": None}


async def discover_executor_roots(roots: tuple[ExecutorCapabilityRoot, ...]) -> tuple[ExecutorRootDiscovery, ...]:
    grouped = {}
    for root in roots:
        grouped.setdefault((root.environment_id, root.high_level), []).append(root)
    results = {}
    for (_, high_level), selected in grouped.items():
        if high_level and selected[0].environment_id != "local":
            environment = selected[0].environment
            for start in range(0, len(selected), MAX_ROOTS_PER_REQUEST):
                batch = selected[start:start + MAX_ROOTS_PER_REQUEST]
                try:
                    info = await environment.info()
                    if any(root.requires_sandbox for root in batch) and not info["capabilities"].get("capabilityDiscoverySandbox", False):
                        raise ExecutorProtocolError("exec-server does not support sandboxed capability discovery")
                    client = await environment.get_client()
                    response = await client.request("capabilityRoots/discoverV1", {"roots": [
                        {"id": root.id, "path": root.path, "sandbox": root.sandbox} for root in batch]})
                    if len(response["roots"]) != len(batch):
                        raise ExecutorProtocolError("exec-server returned a different number of capability roots")
                except (ExecutorError, OSError, TimeoutError) as error:
                    for root in batch:
                        results[root.id] = _failed_discovery(root, error, retryable=_retryable(error))
                    continue
                for root, bundle in zip(batch, response["roots"], strict=True):
                    if bundle["id"] != root.id or bundle["path"] != root.path:
                        results[root.id] = _failed_discovery(root, ExecutorProtocolError("exec-server returned a different capability root identity or path"))
                        continue
                    results[root.id] = ExecutorRootDiscovery(root, bundle)
        else:
            for root in selected:
                try:
                    bundle = await _walk_bundle(root)
                except (ExecutorError, OSError, ValueError) as error:
                    bundle = {"id": root.id, "path": root.path, "skills": [], "warnings": [],
                        "error": f"Selected capability root `{root.id}` discovery failed: {error}"}
                results[root.id] = ExecutorRootDiscovery(root, bundle)
    return tuple(results[root.id] for root in roots)


def _failed_discovery(root, error, *, retryable=False):
    return ExecutorRootDiscovery(root, {"id": root.id, "path": root.path, "skills": [], "warnings": [],
        "error": str(error)}, retryable)


def _namespace(root, path, manifests):
    candidates = []
    for manifest in manifests:
        parent = root.uri(manifest["path"]).parent.parent
        if not path.is_relative_to(parent):
            continue
        try:
            metadata = json.loads(manifest["contents"])
        except ValueError:
            continue
        if not isinstance(metadata, dict) or not isinstance(metadata.get("name", ""), str):
            continue
        name = metadata.get("name", "").strip() or parent.name
        candidates.append((len(parent.uri), name))
    return max(candidates)[1] if candidates else ""


class ExecutorSkillCatalog:
    def __init__(self, discoveries: tuple[ExecutorRootDiscovery, ...], *,
        disabled_paths: Mapping[str, set[str]] | None = None, restriction_product: str | None = None):
        self.discoveries = discoveries
        self.roots = {discovery.root.id: discovery.root for discovery in discoveries}
        self.entries = []
        self.warnings = []
        self.read_snapshot = None
        disabled_paths = disabled_paths or {}
        for discovery in discoveries:
            root, bundle = discovery.root, discovery.bundle
            self.warnings.extend(bundle.get("warnings", []))
            if bundle.get("error") is not None:
                self.warnings.append(f"Selected capability root `{root.id}` discovery failed: {bundle['error']}")
                continue
            for skill in bundle.get("skills", []):
                document = skill["instructions"]
                path = root.uri(document["path"])
                try:
                    metadata = parse_skill_frontmatter(document["contents"], path.parent.name)
                    if len(metadata["name"]) > 64 or len(metadata["description"]) > 1024:
                        raise ValueError("Skill frontmatter exceeds the Source name/description limit")
                    optional = skill.get("metadata")
                    try:
                        details = yaml.safe_load(optional["contents"]) if optional is not None else {}
                        details = details or {}
                        if not isinstance(details, dict) or any(details.get(key) is not None and not isinstance(details[key], dict)
                            for key in ("policy", "interface", "dependencies")):
                            raise ValueError("Skill metadata must contain typed policy/interface/dependencies objects")
                        policy = details.get("policy") or {}
                        if policy.get("allow_implicit_invocation") is not None and not isinstance(policy["allow_implicit_invocation"], bool):
                            raise ValueError("Skill allow_implicit_invocation must be a boolean")
                        products = policy.get("products", [])
                        if not isinstance(products, list) or any(not isinstance(product, str) or product not in {"chatgpt", "codex", "atlas", "CHATGPT", "CODEX", "ATLAS"} for product in products):
                            raise ValueError("Skill products must contain supported Product values")
                        policy["products"] = [product.lower() for product in products]
                        dependencies = details.get("dependencies") or {}
                        tools = dependencies.get("tools", [])
                        if not isinstance(tools, list) or any(not isinstance(tool, dict) for tool in tools):
                            raise ValueError("Skill dependencies.tools must be an array of dependency objects")
                        for tool in tools:
                            for key in ("type", "value", "description", "transport", "command", "url"):
                                if tool.get(key) is not None and not isinstance(tool[key], str):
                                    raise ValueError(f"Skill dependencies.tools.{key} must be a string")
                            oauth = tool.get("oauth")
                            if oauth is not None and (not isinstance(oauth, dict) or oauth.get("callbackPort", oauth.get("callback_port")) is not None
                                and (type(oauth.get("callbackPort", oauth.get("callback_port"))) is not int or not 0 <= oauth.get("callbackPort", oauth.get("callback_port")) <= 65535)):
                                raise ValueError("Skill dependency oauth callbackPort must be a u16")
                    except (yaml.YAMLError, ValueError) as error:
                        self.warnings.append(f"ignoring {optional['path']}: {error}")
                        details = {}
                    policy = details.get("policy") or {}
                    products = policy.get("products", [])
                    if products and restriction_product is not None and restriction_product not in products:
                        continue
                    namespace = _namespace(root, path, bundle.get("namespaceManifests", []))
                    name = f"{namespace}:{metadata['name']}" if namespace else metadata["name"]
                    if len(name) > 129:
                        raise ValueError("Skill qualified name exceeds 129 characters")
                    prefix = f"skill://{root.id}/"
                    resource = prefix + path.native.replace("\\", "/").lstrip("/")
                    package = prefix + path.parent.native.replace("\\", "/").lstrip("/")
                    self.entries.append(ExecutorSkillEntry(root.id, root.environment_id, name,
                        metadata["description"], package, resource, path.uri,
                        prefix + root.uri(root.path).native.replace("\\", "/").lstrip("/"),
                        path.uri not in disabled_paths.get(root.environment_id, set()),
                        policy.get("allow_implicit_invocation") is not False, _short_description(metadata),
                        document["contents"] if root.high_level else None,
                        _dependencies(details)))
                except ValueError as error:
                    self.warnings.append(f"Failed to load environment skill at {path}: {error}")
        self.entries.sort(key=lambda entry: (entry.name, entry.main_path, entry.authority_id))
        alias_roots = list(dict.fromkeys(entry.alias_root for entry in self.entries if entry.enabled and entry.allow_implicit_invocation))
        self.aliases = {f"e{index}": root for index, root in enumerate(alias_roots)}

    def validate_required_skills(self, requirements: Iterable) -> None:
        for requirement in requirements:
            for name in requirement.skill_names:
                if not any(entry.enabled and entry.name == name and entry.environment_id == requirement.environment_id
                    for entry in self.entries):
                    raise RequiredSkillUnavailableError(f"Required skill {json.dumps(name, ensure_ascii=False)} from environment {json.dumps(requirement.environment_id, ensure_ascii=False)} is unavailable")

    async def read(self, package: str, resource: str | None = None) -> tuple[ExecutorSkillEntry, str, str]:
        alias, separator, rest = package.partition("/")
        if separator and alias in self.aliases:
            package = self.aliases[alias].rstrip("/") + "/" + rest
        entry = next((entry for entry in self.entries if entry.enabled and entry.package == package), None)
        if entry is None:
            raise ValueError("Skill package is unavailable from the current executor catalog")
        requested = resource or entry.main_resource
        prefix = package.rstrip("/") + "/"
        relative = requested.removeprefix(prefix)
        if not requested.startswith(prefix) or any(part in {"", ".", ".."} for part in relative.split("/")):
            raise ValueError("executor skill resource does not match its package")
        if requested == entry.main_resource and entry.instructions is not None:
            return entry, requested, entry.instructions
        root = self.roots[entry.authority_id]
        path = root.uri(entry.main_path).parent.joinpath(relative)
        contents = await _read_text(root, path.uri)
        return entry, requested, contents

    def list_page(self, *, cursor: str | None = None, response_byte_budget: int = MAX_RESPONSE_BYTES) -> dict:
        rows, oversized = [], False
        for entry in self.entries:
            if not entry.enabled or not entry.allow_implicit_invocation:
                continue
            if not all(_bounded_handle(value) for value in (entry.authority_id, entry.package, entry.main_resource)):
                oversized = True
                continue
            rows.append({"authority": {"kind": "executor", "id": entry.authority_id}, "package": entry.package,
                "name": _utf8_prefix(entry.name, 256), "description": entry.description if len(entry.description) <= 1024 else entry.description[:1021] + "...",
                "main_resource": entry.main_resource})
        fingerprint = _fingerprint(_json(rows))
        previous_budget = None
        canonical_cursor = cursor
        if cursor is not None and cursor.count(":") == 2:
            canonical_cursor, raw_budget = cursor.rsplit(":", 1)
            try:
                previous_budget = int(raw_budget)
            except ValueError as error:
                raise ValueError("skills.list cursor is invalid") from error
        start = _cursor_offset(canonical_cursor, fingerprint, "skills.list")
        if not 0 <= start <= len(rows):
            raise ValueError("skills.list cursor is invalid")

        def next_at(offset):
            return f"{fingerprint}:{offset}:{response_byte_budget}"

        retained = []
        for index in range(len(rows) - 1, start - 1, -1):
            response = {"skills": [rows[index]], "warnings": [], "next_cursor": next_at(index + 1) if retained else None}
            if len(_json(response).encode("utf-8")) <= response_byte_budget:
                retained.append((index, rows[index]))
            else:
                oversized = True
        retained.reverse()
        warnings = [_utf8_prefix(warning, 256) for warning in self.warnings[:4]] if cursor is None else []
        omission = ("Some skills were omitted because their metadata is too large."
            if oversized and previous_budget != response_byte_budget else None)
        end = min(20, len(retained))
        while True:
            response = {"skills": [row for _, row in retained[:end]],
                "warnings": [*warnings, *([omission] if omission is not None else [])],
                "next_cursor": next_at(retained[end - 1][0] + 1) if end < len(retained) and end else None}
            if len(_json(response).encode("utf-8")) <= response_byte_budget:
                return response
            if end > 1:
                end -= 1
            elif len(warnings) > 1:
                warnings.pop()
            elif response["warnings"]:
                response["skills"] = []
                response["next_cursor"] = next_at(start)
                if len(_json(response).encode("utf-8")) <= response_byte_budget:
                    return response
                if omission is not None and warnings:
                    response["warnings"] = warnings
                    response["next_cursor"] = f"{fingerprint}:{start}"
                    if len(_json(response).encode("utf-8")) <= response_byte_budget:
                        return response
                raise ValueError("skills.list response budget leaves no room for discovery warnings")
            else:
                raise ValueError("skill metadata is too large to list")

    async def read_page(self, package: str, resource: str | None = None, *, cursor: str | None = None,
        response_byte_budget: int = MAX_RESPONSE_BYTES) -> dict:
        alias, separator, rest = package.partition("/")
        if separator and alias in self.aliases:
            package = self.aliases[alias].rstrip("/") + "/" + rest
        entry = next((entry for entry in self.entries if entry.enabled and entry.package == package), None)
        if entry is None:
            raise ValueError("skill package is not available")
        cache_key = package, resource or entry.main_resource
        if cursor is not None and self.read_snapshot is not None and self.read_snapshot[0] == cache_key:
            _, entry, requested, contents = self.read_snapshot
        else:
            entry, requested, contents = await self.read(package, resource)
        encoded = contents.encode("utf-8")
        fingerprint = _fingerprint(contents)
        offset = 0
        if cursor is not None:
            offset = _cursor_offset(cursor, fingerprint, "skills.read")
        if not 0 <= offset <= len(encoded):
            raise ValueError("skills.read cursor is invalid")
        try:
            start = len(encoded[:offset].decode("utf-8"))
        except UnicodeDecodeError as error:
            raise ValueError("skills.read cursor is invalid") from error
        skill_root = self.roots[entry.authority_id].uri(entry.main_path).parent.native

        def page(end):
            next_cursor = f"{fingerprint}:{len(contents[:end].encode('utf-8'))}" if end < len(contents) else None
            return {"resource": requested, "contents": contents[start:end], "skill_root": skill_root, "next_cursor": next_cursor}

        response = page(len(contents))
        if len(_json(response).encode("utf-8")) <= response_byte_budget:
            self.read_snapshot = cache_key, entry, requested, contents
            return response
        lower, upper, best = start + 1, len(contents), None
        while lower <= upper:
            middle = (lower + upper) // 2
            candidate = page(middle)
            if len(_json(candidate).encode("utf-8")) <= response_byte_budget:
                best, lower = candidate, middle + 1
            else:
                upper = middle - 1
        if best is None:
            raise ValueError("skills.read response budget leaves no room for contents")
        self.read_snapshot = cache_key, entry, requested, contents
        return best


def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _short_description(metadata):
    details = metadata.get("metadata", {})
    text = details.get("short-description") if isinstance(details, dict) else None
    return " ".join(text.split()) or None if isinstance(text, str) else None


def _dependencies(details):
    tools = []
    for tool in (details.get("dependencies") or {}).get("tools", []):
        parsed = {name: " ".join(value.split()) for name, value in tool.items() if isinstance(value, str)}
        if not parsed.get("type") or not parsed.get("value") or len(parsed["type"]) > 64 or len(parsed["value"]) > 1024:
            continue
        for name in ("description", "transport", "command", "url"):
            if len(parsed.get(name, "")) > (64 if name == "transport" else 1024):
                parsed.pop(name)
        oauth = tool.get("oauth")
        if oauth is not None:
            parsed["oauth_callback_port"] = oauth.get("callbackPort", oauth.get("callback_port"))
        tools.append(parsed)
    return tuple(tools)


def _fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def _bounded_handle(value: str) -> bool:
    return bool(value) and len(value.encode("utf-8")) <= 2048 and not any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in value)


def _utf8_prefix(value: str, limit: int) -> str:
    return value.encode("utf-8")[:limit].decode("utf-8", errors="ignore")


def _cursor_offset(cursor, fingerprint, tool):
    if cursor is None:
        return 0
    try:
        stamp, raw_offset = cursor.split(":")
        offset = int(raw_offset)
        if offset < 0:
            raise ValueError
    except ValueError as error:
        raise ValueError(f"{tool} cursor is invalid") from error
    if stamp != fingerprint:
        raise ValueError(f"{tool} cursor is stale; restart from the first page")
    return offset
