use anyhow::Result;
use anyhow::bail;
use codex_windows_sandbox::WindowsSandboxProvisioningSettings;
use codex_windows_sandbox::run_elevated_provisioning_setup;
use codex_windows_sandbox::run_windows_sandbox_wrapper_main;
use codex_windows_sandbox::current_setup_user;
use codex_windows_sandbox::windows_sandbox_owner_identity;
use codex_windows_sandbox::windows_sandbox_owner_resources;
use codex_windows_sandbox::bind_windows_sandbox_owner;
use codex_windows_sandbox::clean_up_packaged_windows_sandbox;
use std::path::Path;

#[cfg(test)]
use codex_windows_sandbox::{OwnerIdentity, sandbox_dir, sandbox_bin_dir, sandbox_secrets_dir};
#[cfg(test)]
use sha2::{Digest, Sha256};
#[cfg(test)]
use std::path::PathBuf;
#[cfg(test)]
use windows_sys::core::GUID;
#[cfg(test)]
#[path = "../owner_identity_tests.rs"]
mod tests;

fn main() -> Result<()> {
    let args: Vec<String> = std::env::args().skip(1).collect();
    if args.first().is_some_and(|arg| arg == "--run-as-pty") {
        return run_pty(&args[1..]);
    }
    if args.first().is_some_and(|arg| arg == "--run-as-windows-sandbox") {
        run_windows_sandbox_wrapper_main();
    }
    match args.as_slice() {
        [flag] if flag == "--version" => {
            println!("minicode-windows-sandbox 0.158.0-alpha.2.1 owner-v3");
            Ok(())
        }
        [sandbox, identity, home_flag, home]
            if sandbox == "sandbox" && identity == "identity" && home_flag == "--codex-home" =>
        {
            let owner = windows_sandbox_owner_identity(Path::new(home))?;
            println!("{}", serde_json::to_string(&windows_sandbox_owner_resources(&owner))?);
            Ok(())
        }
        [sandbox, setup, elevated, current_user, home_flag, home]
            if sandbox == "sandbox"
                && setup == "setup"
                && elevated == "--elevated"
                && current_user == "--current-user"
                && home_flag == "--codex-home" =>
        {
            let user = current_setup_user()?;
            codex_windows_sandbox::validate_local_directory_path(Path::new(home))?;
            std::fs::create_dir_all(home)?;
            bind_windows_sandbox_owner(Path::new(home), &user)?;
            run_elevated_provisioning_setup(
                Path::new(home),
                &user,
                WindowsSandboxProvisioningSettings::default(),
            )
        }
        [sandbox, setup, elevated, user_flag, user, home_flag, home]
            if sandbox == "sandbox"
                && setup == "setup"
                && elevated == "--elevated"
                && user_flag == "--user"
                && home_flag == "--codex-home" =>
        {
            codex_windows_sandbox::validate_local_directory_path(Path::new(home))?;
            std::fs::create_dir_all(home)?;
            bind_windows_sandbox_owner(Path::new(home), user)?;
            run_elevated_provisioning_setup(
                Path::new(home),
                user,
                WindowsSandboxProvisioningSettings::default(),
            )
        }
        [sandbox, cleanup, elevated, current_user, home_flag, home]
            if sandbox == "sandbox" && cleanup == "cleanup" && elevated == "--elevated"
                && current_user == "--current-user" && home_flag == "--codex-home" =>
        {
            bind_windows_sandbox_owner(Path::new(home), &current_setup_user()?)?;
            clean_up_packaged_windows_sandbox(Some(Path::new(home)), |line| println!("{line}"), || Ok(()))?;
            codex_windows_sandbox::remove_installation()
        }
        _ => bail!(
            "usage: codex.exe --run-as-windows-sandbox ... | \
             sandbox setup --elevated (--current-user | --user USER) --codex-home PATH | \
             sandbox identity --codex-home PATH | \
             sandbox cleanup --elevated --current-user --codex-home PATH"
        ),
    }
}

fn run_pty(args: &[String]) -> Result<()> {
    let [cwd_flag, cwd, separator, program, arguments @ ..] = args else {
        bail!("usage: --run-as-pty --command-cwd PATH -- PROGRAM [ARGS...]");
    };
    if cwd_flag != "--command-cwd" || separator != "--" {
        bail!("usage: --run-as-pty --command-cwd PATH -- PROGRAM [ARGS...]");
    }
    let runtime = tokio::runtime::Builder::new_current_thread().enable_all().build()?;
    let exit_code = runtime.block_on(async {
        let spawned = codex_utils_pty::spawn_pty_process(
            program, arguments, Path::new(cwd), &std::env::vars().collect(),
            &None, codex_utils_pty::TerminalSize::default(), &[],
        ).await?;
        Ok::<_, anyhow::Error>(codex_windows_sandbox::forward_sandbox_session_stdio(spawned).await)
    })?;
    std::process::exit(exit_code);
}
