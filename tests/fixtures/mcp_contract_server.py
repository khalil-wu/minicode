from __future__ import annotations

import json
import sys


def main() -> None:
    mode = sys.argv[1]
    for line in sys.stdin:
        message = json.loads(line)
        if "id" not in message:
            continue
        method = message.get("method")
        response = {"jsonrpc": "2.0", "id": message["id"]}
        if method == "initialize":
            capabilities = {"resources": {}, "prompts": {}}
            if mode == "tools":
                capabilities["tools"] = {}
            result = {
                "protocolVersion": message["params"]["protocolVersion"],
                "capabilities": capabilities,
                "serverInfo": {"name": "contract-fixture", "version": "1.0"},
            }
        elif method == "tools/list" and mode == "tools":
            result = {"tools": [{
                "name": "inspect",
                "inputSchema": {"type": "object"},
                "outputSchema": {
                    "type": "object",
                    "properties": {"count": {"type": "integer"}},
                    "required": ["count"],
                },
            }]}
        elif method == "tools/call" and mode == "tools":
            behavior = message["params"].get("arguments", {}).get("behavior")
            if behavior == "exit":
                return
            result = {
                "content": [{"type": "text", "text": "TOOL_MARKER"}],
                "structuredContent": {"count": "invalid" if behavior == "invalid" else 1},
            }
        elif method == "resources/list":
            result = {"resources": [{"uri": "fixture://guide", "name": "Guide"}]}
        elif method == "resources/templates/list":
            result = {"resourceTemplates": []}
        elif method == "prompts/list":
            result = {"prompts": [{"name": "review"}]}
        elif method == "resources/read":
            result = {"contents": [{"uri": "fixture://guide", "text": "RESOURCE_MARKER"}]}
        elif method == "prompts/get":
            result = {"messages": [{
                "role": "user",
                "content": {"type": "text", "text": "PROMPT_MARKER"},
            }]}
        elif method == "ping":
            result = {}
        else:
            response["error"] = {"code": -32601, "message": f"unsupported method: {method}"}
            print(json.dumps(response), flush=True)
            continue
        response["result"] = result
        print(json.dumps(response), flush=True)


if __name__ == "__main__":
    main()
