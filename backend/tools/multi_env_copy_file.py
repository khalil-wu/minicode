"""Source file-copy tool: captured endpoints, bounded blocks, no model payload bytes."""
from contextlib import AsyncExitStack
import errno
import json

from backend.environments.errors import ExecutorError
from backend.tools.base import BaseTool, PermissionLevel, ToolResult, ToolSchema
from backend.tools.contracts import ToolSpec
from backend.workspace.executor_paths import ExecutorPath


class MultiEnvCopyFileTool(BaseTool):
    name = "multi_env_copy_file"
    description = "Copy a file between two different environments without loading it into context. Returns bytes copied after completion. Interrupted copies may leave files partially written."
    requires_execution_environment = True
    execution_environment_fields = ("source_environment_id", "target_environment_id")
    workspace_path_fields = ("target_path",)
    mutates_workspace = True
    permission = PermissionLevel.CONFIRM
    display_label = "Copy file"

    def get_schema(self):
        fields = {
            "source_environment_id": "Source environment ID from <environment_context>.",
            "source_path": "Absolute file path in the source environment. Parent directories must exist.",
            "target_environment_id": "Target environment ID from <environment_context>.",
            "target_path": "Absolute file path in the target environment. Parent directories must exist.",
        }
        return ToolSchema(self.name, self.description, {"type": "object", "properties": {
            name: {"type": "string", "description": description} for name, description in fields.items()},
            "required": list(fields), "additionalProperties": False})

    def get_spec(self):
        return ToolSpec(self.name, capability="file.write", toolset="core",
            required_args=("source_environment_id", "source_path", "target_environment_id", "target_path"))

    def validate_input(self, args=None):
        if args["source_environment_id"] == args["target_environment_id"]:
            return "source and target environment IDs must differ. To copy within one environment, use a shell command."
        return ""

    async def execute(self, args, context=None):
        from backend.agent.tool_environments import permission_scope_for_attachment

        endpoints = [context.named_turn_environments[name] for name in self.execution_environment_fields]
        scopes = [permission_scope_for_attachment(context, endpoint) for endpoint in endpoints]
        paths = []
        for scope, field, write in zip(scopes, ("source_path", "target_path"), (False, True)):
            convention = ExecutorPath.parse(scope.cwd_uri, platform_os=scope.platform_os).convention
            try:
                path = ExecutorPath.from_native(args[field], convention=convention, platform_os=scope.platform_os)
            except ValueError:
                return ToolResult(f"provide an absolute {convention} file path for environment {scope.environment_id}", is_error=True)
            access = scope.resolve_access(path.uri)
            if not (access.can_write if write else access.can_read):
                return ToolResult(f"The selected environment does not grant {'write' if write else 'read'} access to {path.uri}", is_error=True)
            paths.append(path)
        source, target = endpoints
        copied = 0
        stage = "source"
        try:
            async with AsyncExitStack() as handles:
                reader = await handles.enter_async_context(await source.environment.filesystem.open(
                    paths[0].uri, sandbox=scopes[0].sandbox_context()))
                stage = "target"
                streaming = (await target.environment.info())["capabilities"].get("fileWriteStreaming", False)
                if streaming:
                    writer = await handles.enter_async_context(await target.environment.filesystem.open(
                        paths[1].uri, sandbox=scopes[1].sandbox_context(), mode="replace"))
                    async for chunk in reader.chunks():
                        await writer.write_block(copied, chunk)
                        copied += len(chunk)
                else:
                    # Source retains compatibility with executors predating streaming writes.
                    contents = bytearray()
                    async for chunk in reader.chunks():
                        contents.extend(chunk)
                    await target.environment.filesystem.write_file(paths[1].uri, bytes(contents),
                        sandbox=scopes[1].sandbox_context())
                    copied = len(contents)
        except (OSError, ExecutorError) as error:
            kind = errno.errorcode.get(error.errno, type(error).__name__) if isinstance(error, OSError) else type(error).__name__
            message = f"could not open copy source: {kind}" if stage == "source" else f"copy failed; target may be truncated or partially written: {kind}"
            return ToolResult(message, is_error=True, native_tool_output=message)
        result = {"bytes_copied": copied}
        body = json.dumps(result)
        return ToolResult(body, native_tool_output=body, runtime_metadata={"code_mode_result": result})
