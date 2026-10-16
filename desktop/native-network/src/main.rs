use anyhow::{Context, Result};
use minicode_network::{Executions, Peer};
use serde_json::{Value, json};
use std::collections::HashMap;
use std::sync::Arc;
use tokio::io::{AsyncBufReadExt, BufReader};
use tokio::sync::Mutex;
use tokio::task::JoinSet;

#[tokio::main]
async fn main() -> Result<()> {
    tracing_subscriber::fmt().with_writer(std::io::stderr).with_env_filter(
        tracing_subscriber::EnvFilter::from_default_env()).init();
    let peer = Arc::new(Peer::new());
    let executions: Executions = Arc::new(Mutex::new(HashMap::new()));
    let mut lines = BufReader::new(tokio::io::stdin()).lines();
    let mut requests = JoinSet::new();
    loop {
        let line = tokio::select! {
            line = lines.next_line() => line?,
            result = requests.join_next(), if !requests.is_empty() => {
                result.context("native network request task missing")???;
                continue;
            }
        };
        let Some(line) = line else { break };
        let message: Value = serde_json::from_str(&line)?;
        if message.get("method").is_none() {
            peer.response(message)?;
            continue;
        }
        let method = message["method"].as_str().context("native network request method is not a string")?.to_string();
        let id = message.get("id").context("native network request has no id")?.clone();
        let params = message.get("params").cloned().unwrap_or(Value::Null);
        let peer = Arc::clone(&peer);
        let executions = Arc::clone(&executions);
        requests.spawn(async move {
            let response = match minicode_network::handle(&method, params, Arc::clone(&peer), executions).await {
                Ok(result) => json!({"id":id,"result":result}),
                Err(error) => json!({"id":id,"error":{"code":-32000,"message":format!("{error:#}")}}),
            };
            peer.send(response).await
        });
    }
    peer.close();
    while let Some(result) = requests.join_next().await {
        result??;
    }
    minicode_network::close(&executions).await
}
