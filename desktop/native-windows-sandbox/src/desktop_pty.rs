//! Desktop transport for the same Source PTY owner used by the command runtime.
use anyhow::{Context, Result, bail};
use base64::Engine;
use base64::engine::general_purpose::STANDARD;
use codex_utils_pty::{ProcessHandle, TerminalSize};
use serde::Deserialize;
use serde_json::{Value, json};
use std::io::{BufRead, Write};
use std::path::PathBuf;
use std::sync::{Arc, Mutex};
use tokio::sync::mpsc;

#[derive(Deserialize)]
#[serde(tag = "method", content = "params", rename_all = "camelCase")]
enum Request {
    Start { program: String, args: Vec<String>, cwd: PathBuf, cols: u16, rows: u16 },
    Write { data: String },
    Resize { cols: u16, rows: u16 },
    Terminate,
}

type Output = Arc<Mutex<std::io::Stdout>>;

fn send(output: &Output, message: Value) -> Result<()> {
    let mut bytes = serde_json::to_vec(&message)?;
    bytes.push(b'\n');
    let mut output = output.lock().expect("desktop PTY output lock");
    output.write_all(&bytes)?;
    output.flush()?;
    Ok(())
}

pub fn run(args: &[String]) -> Result<()> {
    if args == ["--identity"] {
        println!("{}", identity());
        return Ok(());
    }
    if !args.is_empty() { bail!("usage: --desktop-pty [--identity]"); }
    let mut first = String::new();
    std::io::stdin().read_line(&mut first)?;
    let request: Request = serde_json::from_str(&first)?;
    let runtime = tokio::runtime::Builder::new_current_thread().enable_all().build()?;
    runtime.block_on(serve(request))
}

fn identity() -> Value {
    json!({"identity":"minicode-desktop-pty","protocol":1,"sourceVersion":"0.158.0-alpha.2.1"})
}

async fn pump(mut receiver: mpsc::Receiver<Vec<u8>>, output: Output) -> Result<()> {
    while let Some(bytes) = receiver.recv().await {
        send(&output, json!({"event":"data","data":STANDARD.encode(bytes)}))?;
    }
    Ok(())
}

async fn serve(request: Request) -> Result<()> {
    let Request::Start { program, args, cwd, cols, rows } = request else {
        bail!("desktop PTY must start with its original command");
    };
    let spawned = codex_utils_pty::spawn_pty_process(&program, &args, &cwd,
        &std::env::vars().collect(), &None, TerminalSize { rows, cols }, &[]).await?;
    let output: Output = Arc::new(Mutex::new(std::io::stdout()));
    let session = spawned.session;
    send(&output, json!({"event":"ready","identity":identity()}))?;
    let stdout = tokio::spawn(pump(spawned.stdout_rx, Arc::clone(&output)));
    let stderr = tokio::spawn(pump(spawned.stderr_rx, Arc::clone(&output)));
    // Terminal input may wait for the PTY's writer. Keep resize/termination
    // control independent so a pasted input queue cannot hold the owner open.
    let (input, mut input_rx) = mpsc::unbounded_channel::<Vec<u8>>();
    let writer = session.writer_sender();
    let input_output = Arc::clone(&output);
    let input_task = tokio::spawn(async move {
        while let Some(bytes) = input_rx.recv().await {
            if let Err(error) = writer.send(bytes).await {
                send(&input_output, json!({"event":"inputClosed","error":error.to_string()}))?;
                break;
            }
        }
        Ok::<_, anyhow::Error>(())
    });
    let (commands, mut requests) = mpsc::channel(32);
    // A detached std thread does not keep Tokio's blocking pool waiting on an
    // open caller stdin after the real terminal and its owner have finished.
    std::thread::spawn(move || {
        for line in std::io::stdin().lock().lines() {
            let request = line.map_err(anyhow::Error::from)
                .and_then(|line| serde_json::from_str::<Request>(&line).map_err(Into::into));
            if commands.blocking_send(request).is_err() { break; }
        }
    });
    let mut exit = spawned.exit_rx;
    let mut input_open = true;
    let exit_code = loop {
        tokio::select! {
            biased;
            code = &mut exit => break code.context("Source PTY exited without its status")?,
            request = requests.recv(), if input_open => match request {
                Some(request) => command(&session, request?, &input, &output)?,
                None => { input_open = false; session.request_terminate(); },
            },
        }
    };
    input_task.abort();
    // Release this terminal's retained Job Object even if the shell exited
    // while console descendants still held its output handles open.
    session.request_terminate();
    session.close_stdin();
    stdout.await??;
    stderr.await??;
    drop(session);
    send(&output, json!({"event":"exit","exitCode":exit_code}))?;
    Ok(())
}

fn command(session: &ProcessHandle, request: Request, input: &mpsc::UnboundedSender<Vec<u8>>, output: &Output) -> Result<()> {
    match request {
        Request::Write { data } => {
            if let Err(error) = input.send(data.into_bytes()) {
                send(output, json!({"event":"inputClosed","error":error.to_string()}))?;
            }
        }
        Request::Resize { cols, rows } => {
            session.resize(TerminalSize { rows, cols })?;
            send(output, json!({"event":"resized","cols":cols,"rows":rows}))?;
        }
        Request::Terminate => session.request_terminate(),
        Request::Start { .. } => bail!("desktop PTY command was already started"),
    }
    Ok(())
}
