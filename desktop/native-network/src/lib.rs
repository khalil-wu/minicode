//! Thin lifetime/RPC bridge over the pinned Source network policy and proxy.
use anyhow::{Context, Result, bail, ensure};
use codex_config::NetworkConstraints;
use codex_execpolicy::{Policy, PolicyParser};
use codex_network_proxy::{EnvironmentNetworkPolicy, LocalBindingPolicy, ManagedProxyRouting,
    NetworkDecision, NetworkPolicyRequest, NetworkProxy, NetworkProxyAuditMetadata,
    NetworkProxyConfig, NetworkProxyHandle, NetworkPolicyAuditObserver, NetworkPolicyDecider,
    NetworkPolicyRequestArgs, NetworkProtocol, RemoteCredentialMasking};
use codex_protocol::models::PermissionProfile;
use codex_protocol::approvals::{NetworkApprovalContext, NetworkApprovalProtocol, NetworkPolicyAmendment};
use codex_utils_path_uri::Platform;
use serde::Deserialize;
use serde_json::{Value, json};
use std::collections::{HashMap, HashSet};
use std::sync::{Arc, Mutex, RwLock};
use tokio::io::{AsyncWriteExt, Stdout};
use tokio::sync::{Mutex as AsyncMutex, oneshot};

pub mod media;

// prepare-network-runtime.py verifies this exact Source file before compilation.
#[path = "../../core/src/config/network_proxy_spec.rs"]
mod network_proxy_spec;
#[path = "../../core/src/network_policy_decision.rs"]
mod source_network_decision;
use network_proxy_spec::NetworkProxySpec;

pub const SOURCE_COMMIT: &str = "970111f6cb62cc02fe36a82ea7d71b37a2889c97";

#[derive(Clone, Deserialize)]
pub struct PolicySource {
    path: String,
    contents: String,
}

#[derive(Clone, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct PolicyInput {
    controller: Option<NetworkProxyConfig>,
    requirements: Option<NetworkConstraints>,
    environment_policy: Option<EnvironmentNetworkPolicy>,
    #[serde(deserialize_with = "deserialize_profile")]
    permission_profile: PermissionProfile,
    platform_os: Option<String>,
    windows_sandbox: String,
    #[serde(default)]
    exec_policy_sources: Vec<PolicySource>,
}

fn deserialize_profile<'de, D: serde::Deserializer<'de>>(deserializer: D) -> std::result::Result<PermissionProfile, D::Error> {
    codex_file_system::ExecPermissionProfile::deserialize(deserializer).map(PermissionProfile::from)
}

impl PolicyInput {
    fn local_binding(&self) -> LocalBindingPolicy {
        if self.windows_sandbox == "mxc" && self.platform_os.as_deref() == Some("windows") {
            LocalBindingPolicy::RequireTrue
        } else {
            LocalBindingPolicy::DefaultFalse
        }
    }

    fn specs(&self) -> Result<(Option<NetworkProxySpec>, Option<NetworkProxySpec>)> {
        let mut parser = PolicyParser::new();
        for source in &self.exec_policy_sources {
            parser.parse(&source.path, &source.contents)?;
        }
        let exec_policy: Policy = parser.build();
        let controller = if self.controller.is_some() || self.requirements.is_some() {
            Some(NetworkProxySpec::from_config_and_constraints(self.controller.clone().unwrap_or_default(),
                self.requirements.clone(), &self.permission_profile)?)
        } else {
            None
        };
        let effective = match &self.environment_policy {
            Some(policy) => Some(NetworkProxySpec::for_environment(controller.as_ref(), policy,
                &self.permission_profile, &exec_policy, self.local_binding())?),
            None => controller.map(|spec| spec.with_exec_policy_network_rules(&exec_policy))
                .transpose()?,
        };
        let controller = if self.environment_policy.is_some() && (self.controller.is_some() || self.requirements.is_some()) {
            Some(NetworkProxySpec::from_config_and_constraints(self.controller.clone().unwrap_or_default(),
                self.requirements.clone(), &self.permission_profile)?)
        } else { None };
        Ok((controller, effective))
    }

    async fn proxy(&self, spec: &NetworkProxySpec, managed: bool, audit: Option<NetworkPolicyAuditObserver>,
            decider: Option<Arc<dyn NetworkPolicyDecider>>, routing: ManagedProxyRouting) -> Result<NetworkProxy> {
        let mut state = spec.build_state_with_audit_metadata(NetworkProxyAuditMetadata::default(),
            Platform::from_platform_os(self.platform_os.as_deref()), self.local_binding())?;
        if let Some(observer) = audit { state.set_policy_audit_observer(observer); }
        let mut builder = NetworkProxy::builder().state(Arc::new(state)).managed_by_codex(managed)
            .managed_proxy_routing(routing);
        if let Some(decider) = decider { builder = builder.policy_decider_arc(decider); }
        builder.build().await
    }
}

pub struct Peer {
    stdout: AsyncMutex<Stdout>,
    decisions: Mutex<HashMap<String, oneshot::Sender<Value>>>,
    next_decision: std::sync::atomic::AtomicU64,
}

impl Peer {
    pub fn new() -> Self {
        Self { stdout: AsyncMutex::new(tokio::io::stdout()), decisions: Mutex::new(HashMap::new()),
            next_decision: std::sync::atomic::AtomicU64::new(1) }
    }

    pub async fn send(&self, message: Value) -> Result<()> {
        let mut bytes = serde_json::to_vec(&message)?;
        bytes.push(b'\n');
        let mut output = self.stdout.lock().await;
        output.write_all(&bytes).await?;
        output.flush().await?;
        Ok(())
    }

    pub fn response(&self, message: Value) -> Result<()> {
        let id = message["id"].as_str().context("network decision response has no string id")?;
        if let Some(sender) = self.decisions.lock().expect("network decision lock").remove(id) {
            let _ = sender.send(message); // A cancelled Source review no longer has a receiver.
        } else {
            tracing::warn!("late network policy response for {id}");
        }
        Ok(())
    }

    pub fn close(&self) {
        self.decisions.lock().expect("network decision lock").clear();
    }

    async fn decide(&self, request: NetworkPolicyRequest) -> NetworkDecision {
        let id = format!("network-{}", self.next_decision.fetch_add(1, std::sync::atomic::Ordering::Relaxed));
        let (sender, receiver) = oneshot::channel();
        self.decisions.lock().expect("network decision lock").insert(id.clone(), sender);
        let pending = PendingDecision { peer: self, id: id.clone() };
        let result = async {
            self.send(json!({"id":id,"method":"network/policyRequest","params":{
                "executionId":request.execution_id,"environmentId":request.environment_id,
                "protocol":request.protocol.as_policy_protocol(),"host":request.host,"port":request.port,
                "method":request.method,"command":request.command,"execPolicyHint":request.exec_policy_hint}})).await?;
            let response = receiver.await.context("network decision channel closed")?;
            if let Some(error) = response.get("error") { bail!("{error}") }
            let decision = &response["result"];
            match decision["type"].as_str() {
                Some("allow") => Ok(NetworkDecision::Allow),
                Some("deny") => Ok(NetworkDecision::deny(decision["reason"].as_str().context("deny has no reason")?)),
                Some("ask") => Ok(NetworkDecision::ask(decision["reason"].as_str().context("ask has no reason")?)),
                _ => bail!("unknown controller network policy decision"),
            }
        }.await;
        drop(pending);
        match result {
            Ok(decision) => decision,
            Err(error) => {
                tracing::error!("network policy decision failed: {error:#}");
                NetworkDecision::deny(format!("controller network policy decision failed: {error:#}"))
            }
        }
    }
}

struct PendingDecision<'a> { peer: &'a Peer, id: String }
impl Drop for PendingDecision<'_> {
    fn drop(&mut self) { self.peer.decisions.lock().expect("network decision lock").remove(&self.id); }
}

pub struct Execution {
    _proxy: NetworkProxy,
    handle: Option<NetworkProxyHandle>,
    remote_decider: Option<Arc<dyn NetworkPolicyDecider>>,
    audit_tasks: Arc<Mutex<Vec<tokio::task::JoinHandle<Result<()>>>>>,
    browser_targets: Option<BrowserTargets>,
}

type BrowserTargets = Arc<RwLock<BrowserTargetState>>;

#[derive(Default)]
struct BrowserTargetState {
    scoped_hosts: HashSet<String>,
    approved: HashSet<(String, u16, String)>,
}

#[derive(Deserialize)]
struct BrowserTarget { host: String, port: u16, protocol: String }

fn browser_targets(params: &Value) -> Result<HashSet<(String, u16, String)>> {
    let targets: Vec<BrowserTarget> = serde_json::from_value(params["targets"].clone())?;
    targets.into_iter().map(|target| {
        ensure!(matches!(target.protocol.as_str(), "http" | "https_connect"), "unsupported browser target protocol");
        Ok((codex_network_proxy::normalize_host(&target.host), target.port, target.protocol))
    }).collect()
}

async fn flush_audit(tasks: &Mutex<Vec<tokio::task::JoinHandle<Result<()>>>>) -> Result<()> {
    let pending = std::mem::take(&mut *tasks.lock().expect("network audit task lock"));
    for task in pending { task.await??; }
    Ok(())
}

impl Execution {
    async fn close(self) -> Result<()> {
        if let Some(handle) = self.handle { handle.shutdown().await?; }
        flush_audit(&self.audit_tasks).await
    }
}

#[derive(Deserialize)]
#[serde(rename_all = "camelCase")]
struct Prepare {
    policy: PolicyInput,
    environment_id: String,
    execution_id: String,
    attribution_token: String,
    #[serde(default)]
    env: HashMap<String, String>,
    #[serde(default)]
    enable_approval_flow: bool,
}

pub type Executions = Arc<AsyncMutex<HashMap<String, Execution>>>;

pub async fn handle(method: &str, params: Value, peer: Arc<Peer>, executions: Executions) -> Result<Value> {
    match method {
        "identity" => Ok(json!({"identity":"minicode-network","protocol":1,"sourceCommit":SOURCE_COMMIT})),
        "media/audioTokens" => {
            let request: media::AudioTokensRequest = serde_json::from_value(params)?;
            Ok(json!({"tokens":media::audio_tokens(&request.audio_url)}))
        }
        "media/originalImageBytes" => {
            let request: media::OriginalImageBytesRequest = serde_json::from_value(params)?;
            Ok(json!({"bytes":media::original_image_bytes(&request.image_url)}))
        }
        "network/normalizeHost" => Ok(json!({"host":codex_network_proxy::normalize_host(
            params["host"].as_str().context("network host is not a string")?)})),
        "network/deniedMessage" => {
            let params = &params["event"];
            let blocked = codex_network_proxy::BlockedRequest::new(codex_network_proxy::BlockedRequestArgs {
                host:params["host"].as_str().context("network audit has no host")?.to_string(),
                reason:params["reason"].as_str().context("network audit has no reason")?.to_string(),
                protocol:params["protocol"].as_str().context("network audit has no protocol")?.to_string(),
                decision:params["decision"].as_str().map(str::to_string),
                source:params["source"].as_str().map(str::to_string),
                client:params["client"].as_str().map(str::to_string),
                method:params["method"].as_str().map(str::to_string), mode:None,
                port:params["port"].as_u64().map(u16::try_from).transpose()?,
            });
            Ok(json!({"message":source_network_decision::denied_network_policy_message(&blocked)}))
        }
        "network/appendAmendment" => {
            let path = std::path::PathBuf::from(params["path"].as_str().context("network amendment has no path")?);
            let amendment: NetworkPolicyAmendment = serde_json::from_value(json!({
                "host":params["host"],"action":params["action"]}))?;
            let host = amendment.host.clone();
            let protocol = codex_execpolicy::NetworkRuleProtocol::parse(
                params["protocol"].as_str().context("network amendment has no protocol")?)?;
            let context = NetworkApprovalContext { host:host.clone(), protocol:match protocol {
                codex_execpolicy::NetworkRuleProtocol::Http => NetworkApprovalProtocol::Http,
                codex_execpolicy::NetworkRuleProtocol::Https => NetworkApprovalProtocol::Https,
                codex_execpolicy::NetworkRuleProtocol::Socks5Tcp => NetworkApprovalProtocol::Socks5Tcp,
                codex_execpolicy::NetworkRuleProtocol::Socks5Udp => NetworkApprovalProtocol::Socks5Udp,
            }};
            let native = source_network_decision::execpolicy_network_rule_amendment(&amendment, &context, &host);
            tokio::task::spawn_blocking(move || codex_execpolicy::blocking_append_network_rule(&path,
                &host, native.protocol, native.decision, Some(&native.justification))).await??;
            Ok(json!({"written":true}))
        }
        "network/inspect" => {
            let policy: PolicyInput = serde_json::from_value(params)?;
            let (_, spec) = policy.specs()?;
            let Some(spec) = spec else { return Ok(json!({"enabled":false})) };
            if !spec.enabled() { return Ok(json!({"enabled":false})) }
            let proxy = policy.proxy(&spec, false, None, None, ManagedProxyRouting::SharedIngress).await?;
            let launch = proxy.remote_launch_config(policy.local_binding(), RemoteCredentialMasking::Disabled).await?;
            Ok(json!({"enabled":true,"effectivePolicy":spec.environment_policy(),"remoteLaunch":launch}))
        }
        "network/startBrowser" => {
            // Browser public pages share their normal Chromium storage partition,
            // but their transport must still use Source's resolved-address check.
            // Source reserves the dedicated listeners before publishing their
            // real endpoints. This does not use the sandbox/SID ingress route.
            let id = params["executionId"].as_str().context("browser proxy has no executionId")?.to_string();
            let policy: PolicyInput = serde_json::from_value(json!({
                "controller": {"enabled":true,"proxy_url":"http://127.0.0.1:0",
                    "socks_url":"socks5://127.0.0.1:0","domains":{"*":"allow"},
                    "allow_local_binding":false,"allow_upstream_proxy":false},
                "requirements":{"review_domains":["*"]},
                "permissionProfile":{"type":"managed","file_system":{"type":"restricted","entries":[]},
                    "network":"restricted"},
                "platformOs":std::env::consts::OS,"windowsSandbox":"none"
            }))?;
            let mut executions = executions.lock().await;
            ensure!(!executions.contains_key(&id), "browser proxy was already started");
            let (_, spec) = policy.specs()?;
            let targets: BrowserTargets = Arc::new(RwLock::new(BrowserTargetState::default()));
            let allowed = Arc::clone(&targets);
            let decider: Arc<dyn NetworkPolicyDecider> = Arc::new(move |request: NetworkPolicyRequest| {
                let allowed = Arc::clone(&allowed);
                async move {
                    let host = codex_network_proxy::normalize_host(&request.host);
                    let targets = allowed.read().expect("browser target lock");
                    if targets.scoped_hosts.contains(&host) && !targets.approved.contains(
                        &(host, request.port, request.protocol.as_policy_protocol().to_string())) {
                        return NetworkDecision::deny("browser target has no origin grant");
                    }
                    NetworkDecision::Allow
                }
            });
            let proxy = policy.proxy(&spec.context("browser proxy policy is missing")?, true, None,
                Some(decider), ManagedProxyRouting::DedicatedListeners).await?;
            let handle = proxy.run().await?;
            let url = format!("http://{}", proxy.http_addr());
            executions.insert(id, Execution { _proxy:proxy, handle:Some(handle), remote_decider:None,
                audit_tasks:Arc::new(Mutex::new(Vec::new())), browser_targets:Some(targets) });
            Ok(json!({"proxyUrl":url,"sourceCommit":SOURCE_COMMIT,"allowLocalBinding":false}))
        }
        "network/setBrowserTargets" => {
            let id = params["executionId"].as_str().context("browser targets have no executionId")?;
            let targets = browser_targets(&params)?;
            let executions = executions.lock().await;
            let execution = executions.get(id).context("browser proxy was not started")?;
            let active = execution.browser_targets.as_ref().context("network execution is not a browser proxy")?;
            active.write().expect("browser target lock").scoped_hosts.extend(targets.iter().map(|(host, _, _)| host.clone()));
            for (host, _, _) in &targets { execution._proxy.add_allowed_domain(host).await?; }
            active.write().expect("browser target lock").approved = targets;
            Ok(json!({"applied":true}))
        }
        "network/prepareLocal" | "network/prepareRemote" => {
            let prepare: Prepare = serde_json::from_value(params)?;
            let mut executions = executions.lock().await;
            ensure!(!executions.contains_key(&prepare.execution_id), "network execution was already prepared");
            let local = method == "network/prepareLocal";
            if local {
                ensure!(prepare.policy.platform_os.as_deref() == Some(std::env::consts::OS),
                    "local network policy platform does not match its native host");
            }
            let (controller, spec) = prepare.policy.specs()?;
            let spec = spec.context("network proxy is not configured")?;
            ensure!(spec.enabled(), "network proxy is disabled");
            let audit_tasks = Arc::new(Mutex::new(Vec::new()));
            let audit: NetworkPolicyAuditObserver = {
                let peer = Arc::clone(&peer);
                let audit_tasks = Arc::clone(&audit_tasks);
                let execution_id = prepare.execution_id.clone();
                let environment_id = prepare.environment_id.clone();
                Arc::new(move |event| {
                    let peer = Arc::clone(&peer);
                    let notification = json!({"method":"network/policyDecision","params":{
                        "executionId":execution_id,"environmentId":environment_id,
                        "timestamp":event.timestamp,"scope":event.scope,"decision":event.decision,
                        "source":event.source,"reason":event.reason,"protocol":event.protocol.as_policy_protocol(),
                        "host":event.host,"port":event.port,"method":event.method,"client":event.client,
                        "policyOverride":event.policy_override}});
                    let task = tokio::spawn(async move { peer.send(notification).await });
                    audit_tasks.lock().expect("network audit task lock").push(task);
                })
            };
            let carrier = controller.as_ref().unwrap_or(&spec);
            let mut proxy = prepare.policy.proxy(carrier, local, Some(audit), None, ManagedProxyRouting::SharedIngress).await?;
            let root_handle = if local { Some(proxy.run().await?) } else { None };
            let decider: Option<Arc<dyn codex_network_proxy::NetworkPolicyDecider>> = if prepare.enable_approval_flow
                && !spec.environment_policy().managed_allowed_domains_only {
                let peer = Arc::clone(&peer);
                Some(Arc::new(move |request| { let peer = Arc::clone(&peer); async move { peer.decide(request).await } }))
            } else { None };
            proxy = proxy.for_execution(&prepare.environment_id, &prepare.execution_id,
                prepare.attribution_token, prepare.policy.environment_policy.as_ref().map(|_| spec.environment_policy()), decider)?;
            let (result, remote_decider) = if local {
                let prepared = proxy.prepare_for_optional_environment(prepare.env, Some(&prepare.environment_id))?;
                #[cfg(target_os = "windows")]
                let restricting_sid = proxy.network_proxy_restricting_sid(Some(&prepare.environment_id));
                #[cfg(not(target_os = "windows"))]
                let restricting_sid: Option<String> = None;
                let provisioning = json!({"proxy_ports":prepared.sandbox_context.loopback_ports,
                    "allow_local_binding":prepared.sandbox_context.allow_local_binding});
                (json!({"env":prepared.env,"sandboxContext":prepared.sandbox_context,"provisioningSettings":provisioning,
                    "restrictingSid":restricting_sid}), None)
            } else {
                let launch = proxy.remote_launch_config(prepare.policy.local_binding(), RemoteCredentialMasking::Disabled).await?;
                let decider = proxy.remote_policy_decider(launch.proxy.allow_local_binding);
                (json!({"remoteLaunch":launch}), decider)
            };
            executions.insert(prepare.execution_id, Execution { _proxy:proxy, handle:root_handle, remote_decider, audit_tasks, browser_targets:None });
            Ok(result)
        }
        "network/decideRemote" => {
            let id = params["executionId"].as_str().context("network decision has no executionId")?;
            let decider = executions.lock().await.get(id).context("network execution is not prepared")?.remote_decider.clone();
            let Some(decider) = decider else { return Ok(json!({"type":"deny","reason":"not_allowed"})) };
            let request = &params["request"];
            let protocol = match request["protocol"].as_str() {
                Some("http") => NetworkProtocol::Http,
                Some("https_connect") => NetworkProtocol::HttpsConnect,
                Some("socks5_tcp") => NetworkProtocol::Socks5Tcp,
                Some("socks5_udp") => NetworkProtocol::Socks5Udp,
                _ => bail!("unknown network request protocol"),
            };
            let decision = decider.decide(NetworkPolicyRequest::new(NetworkPolicyRequestArgs {
                protocol, host:request["host"].as_str().context("network request has no host")?.to_string(),
                port:u16::try_from(request["port"].as_u64().context("network request has no port")?)?,
                environment_id:None, client_addr:None, method:request["method"].as_str().map(str::to_string),
                command:request["command"].as_str().map(str::to_string),
                exec_policy_hint:request["execPolicyHint"].as_str().map(str::to_string),
            })).await;
            Ok(match decision {
                NetworkDecision::Allow => json!({"type":"allow"}),
                NetworkDecision::Deny { reason, decision, .. } => json!({"type":decision.as_str(),"reason":reason}),
            })
        }
        "network/applyAmendment" => {
            let id = params["executionId"].as_str().context("network amendment has no executionId")?;
            let proxy = executions.lock().await.get(id).context("network execution is not prepared")?._proxy.clone();
            let host = params["host"].as_str().context("network amendment has no host")?;
            match params["action"].as_str() {
                Some("allow") => proxy.add_allowed_domain(host).await?,
                Some("deny") => proxy.add_denied_domain(host).await?,
                _ => bail!("network amendment action must be allow or deny"),
            }
            Ok(json!({"applied":true}))
        }
        "network/flushAudit" => {
            let id = params["executionId"].as_str().context("network audit flush has no executionId")?;
            let tasks = Arc::clone(&executions.lock().await.get(id).context("network execution is not prepared")?.audit_tasks);
            flush_audit(&tasks).await?;
            Ok(json!({"flushed":true}))
        }
        "network/release" => {
            let id = params["executionId"].as_str().context("network release has no executionId")?;
            let execution = executions.lock().await.remove(id).context("network execution is not prepared")?;
            execution.close().await?;
            Ok(json!({"released":true}))
        }
        _ => bail!("unknown native network method `{method}`"),
    }
}

pub async fn close(executions: &Executions) -> Result<()> {
    let active = std::mem::take(&mut *executions.lock().await);
    for (_, execution) in active {
        execution.close().await?;
    }
    Ok(())
}
