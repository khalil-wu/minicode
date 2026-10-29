"""Local stdio peer that exposes its actual Agent Plugins launch context."""
import json
import os
from pathlib import Path
import sys


def main():
    log = Path(os.environ["PLUGIN_DATA"]) / "peer-calls.jsonl"
    for line in sys.stdin:
        message = json.loads(line)
        with log.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(message, ensure_ascii=False) + "\n")
        if "id" not in message:
            continue
        method = message["method"]
        if method == "initialize":
            result = {"protocolVersion": message["params"]["protocolVersion"], "capabilities": {"tools": {}},
                "serverInfo": {"name": "agent-plugin-peer", "version": "1"}}
        elif method == "tools/list":
            result = {"tools": [{"name": "inspect_launch", "description": "Inspect this local fixture's launch context",
                "inputSchema": {"type": "object"}, "annotations": {"readOnlyHint": True}}]}
        elif method == "tools/call":
            context = {"cwd": os.getcwd(), "root": os.environ["PLUGIN_ROOT"], "data": os.environ["PLUGIN_DATA"],
                "argument": sys.argv[1], "literal": os.environ.get("LITERAL"), "overlay": os.environ.get("PLUGIN_TEST_TOKEN")}
            result = {"content": [{"type": "text", "text": json.dumps(context)}], "structuredContent": context}
        else:
            result = {}
        print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}), flush=True)


if __name__ == "__main__":
    main()
