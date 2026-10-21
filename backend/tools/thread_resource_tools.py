"""Current thread resource membership, using its captured repository owner."""
from __future__ import annotations

import json

from backend.async_cleanup import to_thread_cancel_safe
from backend.conversations.thread_resources import pull_request_identity, pull_request_resource
from backend.services.thread_metadata_service import publish_thread_metadata
from backend.tools.base import BaseTool, PermissionLevel, ToolResult, ToolSchema


class ThreadResourceTool(BaseTool):
    permission = PermissionLevel.AUTO

    def __init__(self, name):
        self.name = name
        self.read_only = name == "list_artifacts"
        self.description = {
            "list_artifacts": "List this chat's current durable pull-request and worktree references, including archived worktree snapshots. These are separate from uploaded files and chat history.",
            "attach_artifact": "Attach a pull request to the current chat after creating it or when asked to review or continue it. The original host, owner, repository and pull request number define its stable identity.",
            "remove_artifact": "Unlink a pull request from the current chat. This removes only its saved reference; it does not close or modify the pull request.",
        }[name]

    def get_spec(self):
        from backend.tools.contracts import ToolSpec
        return ToolSpec(name=self.name, capability="artifact.reference", toolset="artifact", exposure="core")

    def get_schema(self):
        properties = {} if self.read_only else {"artifact_type": {"type": "string", "enum": ["pull_request"]}, "url": {"type": "string"}}
        return ToolSchema(name=self.name, description=self.description,
            parameters={"type": "object", "properties": properties, "required": [] if self.read_only else ["artifact_type", "url"]})

    async def execute(self, args, context=None):
        if context is None or context.run_context is None or context.run_context.conversation_repository is None or not context.conversation_id:
            return ToolResult(content="The original chat repository and conversation owner are required", is_error=True)
        repository = context.run_context.conversation_repository
        try:
            if self.read_only:
                result = await to_thread_cancel_safe(repository.thread_resources.list, context.conversation_id)
            else:
                if self.name == "attach_artifact":
                    identity, payload = pull_request_resource(args["url"])
                    result = await to_thread_cancel_safe(repository.thread_resources.add, context.conversation_id,
                        "pull_request", identity, payload)
                else:
                    identity = pull_request_identity(args["url"])
                    removed = await to_thread_cancel_safe(repository.thread_resources.remove, context.conversation_id, "pull_request", identity)
                    result = {"removed": removed is not None}
                await publish_thread_metadata(repository)
            return ToolResult(content=json.dumps(result, ensure_ascii=False))
        except ValueError as error:
            return ToolResult(content=str(error), is_error=True)
