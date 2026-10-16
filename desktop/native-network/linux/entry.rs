//! Linux-only entry point; the existing Windows proxy service is unchanged.
mod proxy_service {
    include!("main.rs");

    pub(super) fn run() -> anyhow::Result<()> {
        main()
    }
}

fn main() -> anyhow::Result<()> {
    // Source may prepend --verify-fd-mount during inner-stage bwrap translation.
    // Dispatch before Tokio, since the helper forks its host proxy bridges.
    if std::env::args().skip(1).take_while(|arg| arg != "--")
        .any(|arg| arg == "--sandbox-policy-cwd") {
        codex_linux_sandbox::run_main();
    }
    proxy_service::run()
}
