//! One immutable owner per helper/launcher/runner process. No environment routing authority.
use anyhow::{Context, Result, ensure};
use serde::Serialize;
use sha2::{Digest, Sha256};
use std::path::{Path, PathBuf};
use std::sync::OnceLock;
use windows_sys::core::GUID;

#[derive(Debug, Clone, Eq, PartialEq, Serialize)]
pub struct OwnerIdentity {
    pub schema: u32,
    pub owner_id: String,
    pub user_sid: String,
    pub home: PathBuf,
    pub offline_username: String,
    pub online_username: String,
    pub group: String,
    pub installation_key: String,
    pub core_installation_key: String,
    pub setup_mutex: String,
    pub read_acl_mutex: String,
    pub service: String,
    pub pipe: String,
}

impl OwnerIdentity {
    // The caller supplies a canonical local home and an OS-resolved SID.
    #[doc(hidden)]
    pub fn from_canonical(home: PathBuf, user_sid: String) -> Self {
        let path_key = home.to_string_lossy().replace('\\', "/").to_ascii_lowercase();
        let digest = Sha256::digest(
            format!("minicode.windows-sandbox.owner.v3\0{user_sid}\0{path_key}").as_bytes(),
        );
        let owner_id = digest.iter().map(|b| format!("{b:02x}")).collect::<String>();
        // 80-bit account label: 2 brand characters + 16 base32 characters + role = 19.
        let alphabet = b"abcdefghijklmnopqrstuvwxyz234567";
        let mut label = String::new();
        for index in 0..16 {
            let bit = index * 5;
            let word = (u16::from(digest[bit / 8]) << 8)
                | u16::from(digest[bit / 8 + 1]);
            label.push(alphabet[((word >> (11 - bit % 8)) & 31) as usize] as char);
        }
        let installation_key = format!(r"SOFTWARE\MiniCode\WindowsSandbox\Owners\{owner_id}");
        Self {
            schema: 3,
            offline_username: format!("MC{label}O"),
            online_username: format!("MC{label}N"),
            group: format!("MiniCodeSandboxUsers-{owner_id}"),
            core_installation_key: format!(r"{installation_key}\RegisteredCore"),
            installation_key,
            setup_mutex: format!(r"Global\MiniCodeSandboxSetup-{owner_id}"),
            read_acl_mutex: format!(r"Local\MiniCodeSandboxReadAcl-{owner_id}"),
            service: format!("MiniCodeSandboxService.{owner_id}"),
            pipe: format!(r"\\.\pipe\MiniCode.Sandbox.{owner_id}"),
            owner_id,
            user_sid,
            home,
        }
    }

    #[doc(hidden)]
    pub fn guid(&self, seed: GUID) -> GUID {
        let mut hash = Sha256::new();
        hash.update(b"minicode.windows-sandbox.wfp.v3\0");
        hash.update(self.owner_id.as_bytes());
        hash.update(seed.data1.to_be_bytes());
        hash.update(seed.data2.to_be_bytes());
        hash.update(seed.data3.to_be_bytes());
        hash.update(seed.data4);
        let digest = hash.finalize();
        let mut bytes: [u8; 16] = digest[..16].try_into().unwrap();
        bytes[6] = (bytes[6] & 15) | 0x80;
        bytes[8] = (bytes[8] & 63) | 0x80;
        GUID::from_u128(u128::from_be_bytes(bytes))
    }

    pub(crate) fn firewall_name(&self, name: &str) -> String {
        format!("{name}.{}", self.owner_id)
    }
}

static OWNER: OnceLock<OwnerIdentity> = OnceLock::new();

pub(crate) fn owner() -> &'static OwnerIdentity {
    OWNER.get().expect("owner must be bound at the process request boundary")
}

pub fn windows_sandbox_owner_identity(home: &Path) -> Result<OwnerIdentity> {
    let user = crate::runtime_ownership::current_setup_user()?;
    identity_for_user(home, &user)
}

fn identity_for_user(home: &Path, user: &str) -> Result<OwnerIdentity> {
    crate::validate_local_directory_path(home)?;
    let home = dunce::canonicalize(home).context("canonicalize sandbox owner home")?;
    let sid = crate::resolve_sid(user)?;
    let sid = crate::string_from_sid_bytes(&sid).map_err(anyhow::Error::msg)?;
    Ok(OwnerIdentity::from_canonical(home, sid))
}

fn bind(identity: OwnerIdentity) -> Result<()> {
    if let Some(previous) = OWNER.get() {
        ensure!(previous.owner_id == identity.owner_id, "process is already bound to another sandbox owner");
    } else {
        OWNER.set(identity).expect("owner is bound once before worker threads start");
    }
    Ok(())
}

// Reject legacy/copied credentials before setup can write a marker or reset any account.
pub fn bind_for_user(home: &Path, user: &str) -> Result<()> {
    let identity = identity_for_user(home, user)?;
    let path = crate::setup::sandbox_users_path(home);
    match std::fs::read(&path) {
        Ok(bytes) => {
            let users: crate::setup::SandboxUsersFile = serde_json::from_slice(&bytes)
                .context("read sandbox owner credentials (legacy homes require their v2 runtime)")?;
            ensure!(
                users.owner_id == identity.owner_id
                    && users.offline.username == identity.offline_username
                    && users.online.username == identity.online_username,
                "sandbox home belongs to a legacy or different owner; use its original runtime, not automatic migration"
            );
        }
        Err(err) if err.kind() == std::io::ErrorKind::NotFound => {}
        Err(err) => return Err(err).context("read sandbox owner credentials"),
    }
    match std::fs::read(crate::setup::setup_marker_path(home)) {
        Ok(bytes) if !bytes.is_empty() => {
            let marker: crate::setup::SetupMarker = serde_json::from_slice(&bytes)
                .context("read sandbox owner marker")?;
            ensure!(marker.owner_id == identity.owner_id
                && marker.offline_username == identity.offline_username
                && marker.online_username == identity.online_username,
                "sandbox marker belongs to a legacy or different owner");
        }
        // Setup intentionally leaves an empty protected marker until commit.
        Ok(_) => {}
        Err(err) if err.kind() == std::io::ErrorKind::NotFound => {}
        Err(err) => return Err(err).context("read sandbox owner marker"),
    }
    bind(identity)
}

pub(crate) fn bind_current(home: &Path) -> Result<()> {
    bind_for_user(home, &crate::runtime_ownership::current_setup_user()?)
}

// Runner cannot read the owner-only credential directory. Its framed parent supplies
// the canonical home/SID; its own OS token must identify one derived sandbox account.
pub fn bind_windows_sandbox_runner(home: &Path, user_sid: &str) -> Result<()> {
    crate::validate_local_directory_path(home)?;
    let owner_sid = crate::winutil::sid_bytes_from_string(user_sid)?;
    let owner_sid = crate::string_from_sid_bytes(&owner_sid).map_err(anyhow::Error::msg)?;
    let identity = OwnerIdentity::from_canonical(home.to_path_buf(), owner_sid);
    let current = crate::resolve_sid(&crate::runtime_ownership::current_setup_user()?)?;
    let offline = crate::resolve_sid(&identity.offline_username)?;
    let online = crate::resolve_sid(&identity.online_username)?;
    ensure!(current == offline || current == online, "runner token does not belong to sandbox owner");
    bind(identity)
}

pub fn windows_sandbox_read_acl_mutex() -> &'static str {
    &owner().read_acl_mutex
}

#[cfg(test)]
#[path = "owner_identity_tests.rs"]
mod tests;
