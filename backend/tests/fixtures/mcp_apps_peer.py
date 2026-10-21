"""Owned stdio MCP peer used by both the runtime chain and real browser audit."""
import json
from pathlib import Path
import sys
import os
from uuid import uuid4

URI = "ui://owned/counter.html"
CSP = {"connectDomains": ["https://api.owned.example"], "resourceDomains": ["https://cdn.owned.example"],
       "frameDomains": ["https://frame.owned.example"], "baseUriDomains": ["https://base.owned.example"]}
HTML = r'''<!doctype html><html><head><meta charset="utf-8"><title>Owned MCP counter</title></head>
<body><h3>Owned MCP counter</h3><button id="increment">Increment</button><output id="count">0</output>
<button id="tool">Call app-only tool</button><output id="tool-result"></output><output id="received"></output>
<script>
window.instanceId=crypto.randomUUID();window.initializeCount=1;window.count=0;let sequence=0;const pending=new Map();
function rpc(method,params){const id=++sequence;return new Promise((resolve,reject)=>{pending.set(id,{resolve,reject});parent.postMessage({jsonrpc:'2.0',id,method,params},'*')})}
window.addEventListener('message',event=>{if(event.source!==parent)return;const m=event.data;if(m.id!=null&&pending.has(m.id)){const p=pending.get(m.id);pending.delete(m.id);m.error?p.reject(m.error):p.resolve(m.result);return}
if(m.method==='ui/notifications/tool-input')window.input=m.params.arguments;
if(m.method==='ui/notifications/tool-result'){window.result=m.params;document.querySelector('#received').textContent=m.params._meta.privateUi;}
if(m.method==='ui/notifications/host-context-changed')Object.assign(window.hostContext||={},m.params);
if(m.method==='ui/resource-teardown'){window.tornDown=true;parent.postMessage({jsonrpc:'2.0',id:m.id,result:{}},'*')}
if(m.method==='owned/test-port'){window.testPort=event.ports[0];testPort.onmessage=e=>testPort.postMessage({instance:instanceId,echo:e.data,count:window.count})}
});
document.querySelector('#increment').onclick=()=>{window.count++;document.querySelector('#count').textContent=window.count};
document.querySelector('#tool').onclick=async()=>{const r=await rpc('tools/call',{name:'app_only',arguments:{value:window.count}});document.querySelector('#tool-result').textContent=r.structuredContent.value;await rpc('ui/update-model-context',{content:[{type:'text',text:'counter='+window.count}]})};
rpc('ui/initialize',{appInfo:{name:'Owned counter',version:'1'},appCapabilities:{},protocolVersion:'2026-01-26'}).then(r=>{window.hostContext=r.hostContext;parent.postMessage({jsonrpc:'2.0',method:'ui/notifications/initialized',params:{}},'*');parent.postMessage({jsonrpc:'2.0',method:'ui/notifications/size-changed',params:{height:180}},'*')});
</script></body></html>'''

def main():
    log = Path(sys.argv[1])
    nonce, calls = uuid4().hex, 0
    for line in sys.stdin:
        message = json.loads(line)
        with log.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({**message, "_peerNonce": nonce, "_peerPid": os.getpid()}, ensure_ascii=False) + "\n")
        if "id" not in message:
            continue
        method = message["method"]
        if method == "initialize":
            result = {"protocolVersion": message["params"]["protocolVersion"], "capabilities": {"tools": {}, "resources": {}}, "serverInfo": {"name": "owned-app", "version": "1"}}
        elif method == "tools/list":
            result = {"tools": [
                {"name": "counter", "title": "Owned counter", "description": "Show the owned counter", "inputSchema": {"type": "object"}, "annotations": {"readOnlyHint": True}, "_meta": {"ui": {"resourceUri": URI}}},
                {"name": "app_only", "description": "Called only by the App", "inputSchema": {"type": "object"}, "_meta": {"ui": {"visibility": ["app"]}}},
                {"name": "model_only", "description": "Not called by Apps", "inputSchema": {"type": "object"}, "_meta": {"ui": {"visibility": ["model"]}}},
            ]}
        elif method == "tools/call":
            calls += 1
            elicitation = None
            if message["params"].get("arguments", {}).get("elicit"):
                print(json.dumps({"jsonrpc": "2.0", "id": "owned-elicitation", "method": "elicitation/create", "params": {"mode": "form", "message": "Choose the owned value", "requestedSchema": {"type": "object", "properties": {"selected": {"type": "string", "enum": ["yes"]}}, "required": ["selected"]}}}), flush=True)
                elicitation = json.loads(sys.stdin.readline())
            result = {"content": [{"type": "text", "text": "Owned counter result"}], "structuredContent": {"value": message["params"].get("arguments", {}).get("value", 0), "nonce": nonce, "pid": os.getpid(), "callCount": calls}, "_meta": {"privateUi": "UI_ONLY_SECRET"}}
            if elicitation is not None:
                result["structuredContent"]["elicitationResponse"] = elicitation
        elif method == "resources/read":
            result = {"contents": [{"uri": URI, "mimeType": "text/html;profile=mcp-app", "text": HTML, "_meta": {"ui": {"prefersBorder": True, "csp": CSP}}}]}
        elif method == "resources/list":
            result = {"resources": [{"uri": URI, "name": "Counter"}]}
        elif method == "resources/templates/list":
            result = {"resourceTemplates": []}
        else:
            result = {}
        print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}), flush=True)

if __name__ == "__main__":
    main()
