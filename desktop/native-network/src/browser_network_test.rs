use minicode_network::{Executions, Peer, SOURCE_COMMIT, close, handle};
use serde_json::json;
use std::collections::HashMap;
use std::sync::Arc;
use tokio::sync::Mutex as AsyncMutex;
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::{TcpListener, TcpStream};
use tokio::time::{Duration, timeout};

#[tokio::test]
async fn browser_proxy_owns_its_listener_and_blocks_local_target_before_connect() {
    let peer = Arc::new(Peer::new());
    let executions: Executions = Arc::new(AsyncMutex::new(HashMap::new()));
    let target = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let target_addr = target.local_addr().unwrap();
    let started = handle("network/startBrowser", json!({"executionId":"public-browser"}),
        Arc::clone(&peer), Arc::clone(&executions)).await.unwrap();
    assert_eq!(started["sourceCommit"], SOURCE_COMMIT);
    assert_eq!(started["allowLocalBinding"], false);
    let proxy = started["proxyUrl"].as_str().unwrap().strip_prefix("http://").unwrap();
    assert!(!proxy.ends_with(":0"));
    let mut stream = TcpStream::connect(proxy).await.unwrap();
    stream.write_all(format!("GET http://{target_addr}/private HTTP/1.1\r\nHost: {target_addr}\r\nConnection: close\r\n\r\n").as_bytes()).await.unwrap();
    let mut response = Vec::new();
    timeout(Duration::from_secs(5), stream.read_to_end(&mut response)).await.unwrap().unwrap();
    assert!(String::from_utf8_lossy(&response).starts_with("HTTP/1.1 403"));
    assert!(timeout(Duration::from_millis(100), target.accept()).await.is_err());
    assert!(handle("network/startBrowser", json!({"executionId":"public-browser"}),
        Arc::clone(&peer), Arc::clone(&executions)).await.is_err());
    handle("network/release", json!({"executionId":"public-browser"}), peer,
        Arc::clone(&executions)).await.unwrap();
    assert!(executions.lock().await.is_empty());
    assert!(TcpStream::connect(proxy).await.is_err());
}

#[tokio::test]
async fn browser_origin_grant_is_limited_to_its_host_port_and_protocol() {
    let peer = Arc::new(Peer::new());
    let executions: Executions = Arc::new(AsyncMutex::new(HashMap::new()));
    let preview = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let other = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let target = preview.local_addr().unwrap();
    let other_target = other.local_addr().unwrap();
    let started = handle("network/startBrowser", json!({"executionId":"workspace-browser"}),
        Arc::clone(&peer), Arc::clone(&executions)).await.unwrap();
    let proxy = started["proxyUrl"].as_str().unwrap().strip_prefix("http://").unwrap();
    handle("network/setBrowserTargets", json!({"executionId":"workspace-browser","targets":[
        {"host":"127.0.0.1","port":target.port(),"protocol":"http"}
    ]}), Arc::clone(&peer), Arc::clone(&executions)).await.unwrap();
    let serving = tokio::spawn(async move {
        let (mut socket, _) = preview.accept().await.unwrap();
        let mut request = [0; 2048];
        socket.read(&mut request).await.unwrap();
        socket.write_all(b"HTTP/1.1 200 OK\r\nContent-Length: 7\r\nConnection: close\r\n\r\npreview").await.unwrap();
        preview
    });
    let mut stream = TcpStream::connect(proxy).await.unwrap();
    stream.write_all(format!("GET http://{target}/ HTTP/1.1\r\nHost: {target}\r\nConnection: close\r\n\r\n").as_bytes()).await.unwrap();
    let mut response = Vec::new();
    timeout(Duration::from_secs(5), stream.read_to_end(&mut response)).await.unwrap().unwrap();
    assert!(String::from_utf8_lossy(&response).starts_with("HTTP/1.1 200"));
    assert!(response.ends_with(b"preview"));
    let preview = serving.await.unwrap();
    for request in [
        format!("GET http://{other_target}/ HTTP/1.1\r\nHost: {other_target}\r\nConnection: close\r\n\r\n"),
        format!("CONNECT {target} HTTP/1.1\r\nHost: {target}\r\nConnection: close\r\n\r\n"),
    ] {
        let mut stream = TcpStream::connect(proxy).await.unwrap();
        stream.write_all(request.as_bytes()).await.unwrap();
        let mut response = Vec::new();
        timeout(Duration::from_secs(5), stream.read_to_end(&mut response)).await.unwrap().unwrap();
        assert!(String::from_utf8_lossy(&response).starts_with("HTTP/1.1 403"));
    }
    assert!(timeout(Duration::from_millis(100), other.accept()).await.is_err());
    assert!(timeout(Duration::from_millis(100), preview.accept()).await.is_err());
    handle("network/setBrowserTargets", json!({"executionId":"workspace-browser","targets":[]}),
        Arc::clone(&peer), Arc::clone(&executions)).await.unwrap();
    let mut stream = TcpStream::connect(proxy).await.unwrap();
    stream.write_all(format!("GET http://{target}/ HTTP/1.1\r\nHost: {target}\r\nConnection: close\r\n\r\n").as_bytes()).await.unwrap();
    let mut response = Vec::new();
    timeout(Duration::from_secs(5), stream.read_to_end(&mut response)).await.unwrap().unwrap();
    assert!(String::from_utf8_lossy(&response).starts_with("HTTP/1.1 403"));
    assert!(timeout(Duration::from_millis(100), preview.accept()).await.is_err());
    close(&executions).await.unwrap();
}

#[tokio::test]
async fn browser_release_closes_a_tunnel_already_open_when_its_target_is_revoked() {
    let peer = Arc::new(Peer::new());
    let executions: Executions = Arc::new(AsyncMutex::new(HashMap::new()));
    let target = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let address = target.local_addr().unwrap();
    let started = handle("network/startBrowser", json!({"executionId":"tunnel-browser"}),
        Arc::clone(&peer), Arc::clone(&executions)).await.unwrap();
    let proxy = started["proxyUrl"].as_str().unwrap().strip_prefix("http://").unwrap();
    handle("network/setBrowserTargets", json!({"executionId":"tunnel-browser","targets":[
        {"host":"127.0.0.1","port":address.port(),"protocol":"https_connect"}
    ]}), Arc::clone(&peer), Arc::clone(&executions)).await.unwrap();
    let mut client = TcpStream::connect(proxy).await.unwrap();
    client.write_all(format!("CONNECT {address} HTTP/1.1\r\nHost: {address}\r\n\r\n").as_bytes()).await.unwrap();
    let (mut upstream, _) = timeout(Duration::from_secs(5), target.accept()).await.unwrap().unwrap();
    let mut headers = [0; 1024];
    let length = timeout(Duration::from_secs(5), client.read(&mut headers)).await.unwrap().unwrap();
    assert!(String::from_utf8_lossy(&headers[..length]).starts_with("HTTP/1.1 200"));
    handle("network/setBrowserTargets", json!({"executionId":"tunnel-browser","targets":[]}),
        Arc::clone(&peer), Arc::clone(&executions)).await.unwrap();
    client.write_all(b"existing tunnel").await.unwrap();
    let mut bytes = [0; 15];
    timeout(Duration::from_secs(5), upstream.read_exact(&mut bytes)).await.unwrap().unwrap();
    assert_eq!(&bytes, b"existing tunnel");
    handle("network/release", json!({"executionId":"tunnel-browser"}), peer,
        Arc::clone(&executions)).await.unwrap();
    assert_eq!(timeout(Duration::from_secs(5), client.read(&mut bytes)).await.unwrap().unwrap(), 0);
    assert_eq!(timeout(Duration::from_secs(5), upstream.read(&mut bytes)).await.unwrap().unwrap(), 0);
}
