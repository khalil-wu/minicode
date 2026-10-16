//! Linux-only entry point; the existing Windows proxy service is unchanged.
mod proxy_service {
    include!("main.rs");

    pub(super) fn run() -> anyhow::Result<()> {
        main()
    }
}

fn main() -> anyhow::Result<()> {
    // Source's outer and inner stages both start with this argument. Dispatch
    // before Tokio, since the original helper forks its host proxy bridges.
    if std::env::args().nth(1).as_deref() == Some("--sandbox-policy-cwd") {
        codex_linux_sandbox::run_main();
    }
    proxy_service::run()
}
