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
use std::fs::File;
use std::io::{Read, Write};
use std::os::windows::io::{FromRawHandle, RawHandle};
use std::sync::Arc;

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
        [sandbox, ready, home_flag, home, settings_flag, settings]
            if sandbox == "sandbox" && ready == "network-ready" && home_flag == "--codex-home"
                && settings_flag == "--settings-json" =>
        {
            let settings: WindowsSandboxProvisioningSettings = serde_json::from_str(settings)?;
            bind_windows_sandbox_owner(Path::new(home), &current_setup_user()?)?;
            let ready = codex_windows_sandbox::sandbox_setup_is_complete_with_settings(Path::new(home), &settings);
            println!("{}", serde_json::json!({"ready":ready,"settings":settings}));
            Ok(())
        }
        [sandbox, setup, elevated, user_flag, user, home_flag, home, settings_flag, settings]
            if sandbox == "sandbox" && setup == "setup" && elevated == "--elevated"
                && user_flag == "--user" && home_flag == "--codex-home" && settings_flag == "--settings-json" =>
        {
            let settings: WindowsSandboxProvisioningSettings = serde_json::from_str(settings)?;
            codex_windows_sandbox::validate_local_directory_path(Path::new(home))?;
            std::fs::create_dir_all(home)?;
            bind_windows_sandbox_owner(Path::new(home), user)?;
            run_elevated_provisioning_setup(Path::new(home), user, settings)
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
    let (control, args) = match args {
        [flag, input, output, rest @ ..] if flag == "--control-handles" => {
            let input = input.parse::<usize>()? as RawHandle;
            let output = output.parse::<usize>()? as RawHandle;
            // Only the wrapper inherits these handles. Source's ConPTY
            // CreateProcessW uses bInheritHandles=FALSE for the shell.
            (Some(unsafe { (File::from_raw_handle(input), File::from_raw_handle(output)) }), rest)
        }
        _ => (None, args),
    };
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
        Ok::<_, anyhow::Error>(codex_windows_sandbox::forward_sandbox_session_stdio_with_session(spawned, move |session| {
            if let Some((input, output)) = control {
                std::thread::spawn(move || {
                    if let Err(error) = resize_pty(session, input, output) {
                        eprintln!("Windows PTY resize channel failed: {error:#}");
                    }
                });
            }
        }).await)
    })?;
    std::process::exit(exit_code);
}

fn resize_pty(session: Arc<codex_utils_pty::ProcessHandle>, mut input: File, mut output: File) -> Result<()> {
    loop {
        let mut size = [0u8; 4];
        if input.read(&mut size[..1])? == 0 {
            return Ok(());
        }
        input.read_exact(&mut size[1..])?;
        let size = codex_utils_pty::TerminalSize {
            cols: u16::from_be_bytes([size[0], size[1]]),
            rows: u16::from_be_bytes([size[2], size[3]]),
        };
        let response = match session.resize(size) {
            Ok(()) => serde_json::json!({"resized": true}),
            Err(error) => serde_json::json!({"error": format!("{error:#}")}),
        };
        serde_json::to_writer(&mut output, &response)?;
        output.write_all(b"\n")?;
        output.flush()?;
    }
}
