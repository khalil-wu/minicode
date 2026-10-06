"""Apply MiniCode's owner-scoped Windows sandbox identity to pinned Codex source."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import uuid


UPSTREAM_VERSION = "0.158.0-alpha.2.1"
PATCH_VERSION = 3
NAMESPACE = uuid.uuid5(uuid.NAMESPACE_DNS, "com.minicode.desktop")
IDENTITY_REPLACEMENTS = (
    ("CodexSandboxOffline", "MiniCodeSbxOffline"),
    ("CodexSandboxOnline", "MiniCodeSbxOnline"),
    ("OpenAI.CodexSandbox", "MiniCode.Sandbox"),
    ("CodexSandbox", "MiniCodeSandbox"),
    ("codex_sandbox_offline_", "minicode_sandbox_offline_"),
    ("codex_wfp_", "minicode_wfp_"),
    ("Codex Windows Sandbox WFP", "MiniCode Windows Sandbox WFP"),
    ("Codex Sandbox Offline", "MiniCode Sandbox Offline"),
)
GUID_RE = re.compile(r"GUID::from_u128\(0x([0-9a-fA-F_]+)\)")


def _owner_namespace_edits(source_dir: Path, texts: dict[Path, str]) -> None:
    """Patch the reviewed v2 ownership chain; all anchors must match before writing."""
    def replace(file: str, old: str, new: str, count: int = 1) -> None:
        path = source_dir / file
        text = texts[path]
        if text.count(new) == count:
            return
        if text.count(old) != count:
            raise RuntimeError(f"Owner namespace anchor changed: {file}: {old[:80]!r}")
        texts[path] = text.replace(old, new)

    replace("lib.rs", 'mod runtime_ownership;', 'mod runtime_ownership;\n#[cfg(target_os = "windows")]\nmod owner_identity;\n#[cfg(target_os = "windows")]\npub use owner_identity::{OwnerIdentity, windows_sandbox_owner_identity, bind_windows_sandbox_runner, windows_sandbox_read_acl_mutex};\n#[cfg(target_os = "windows")]\npub use owner_identity::bind_for_user as bind_windows_sandbox_owner;\n#[cfg(target_os = "windows")]\npub use runtime_ownership::current_setup_user;\n#[cfg(target_os = "windows")]\npub use wfp::windows_sandbox_owner_resources;')
    replace("runtime_ownership.rs", 'pub(crate) fn current_setup_user()', 'pub fn current_setup_user()')
    replace("setup.rs", 'pub const SETUP_VERSION: u32 = 5;', 'pub const SETUP_VERSION: u32 = 6;')
    replace("setup.rs", 'pub const OFFLINE_USERNAME: &str = "MiniCodeSbxOffline";', 'pub fn offline_username() -> &\'static str { &crate::owner_identity::owner().offline_username }')
    replace("setup.rs", 'pub const ONLINE_USERNAME: &str = "MiniCodeSbxOnline";', 'pub fn online_username() -> &\'static str { &crate::owner_identity::owner().online_username }')
    replace("winutil.rs", 'pub const SANDBOX_USERS_GROUP: &str = "MiniCodeSandboxUsers";', 'pub fn sandbox_users_group() -> &\'static str { &crate::owner_identity::owner().group }')
    replace("installation_record.rs", 'pub const INSTALLATION_KEY: &str = r"SOFTWARE\\OpenAI\\Codex\\WindowsSandboxService";', 'pub fn installation_key() -> &\'static str { &crate::owner_identity::owner().installation_key }')
    replace("runtime_ownership.rs", 'pub const CORE_INSTALLATION_KEY: &str =\n    r"SOFTWARE\\OpenAI\\Codex\\WindowsSandboxService\\RegisteredCore";', 'pub fn core_installation_key() -> &\'static str { &crate::owner_identity::owner().core_installation_key }')

    # Convert the old imported constants to owner-bound getters, including callers/tests.
    # Rust format captures need a local binding rather than a function expression.
    replace("winutil.rs", '    const ERROR_ALIAS_EXISTS:', '    let group = sandbox_users_group();\n    const ERROR_ALIAS_EXISTS:')
    replace("setup_provisioning/sandbox_users.rs", '    if let Err(err) = ensure_sandbox_users_group()', '    let group = sandbox_users_group();\n    if let Err(err) = ensure_sandbox_users_group()')
    for path, text in list(texts.items()):
        text = text.replace('{SANDBOX_USERS_GROUP}', '{group}')
        for old, new in [("OFFLINE_USERNAME", "offline_username"), ("ONLINE_USERNAME", "online_username"), ("SANDBOX_USERS_GROUP", "sandbox_users_group"), ("CORE_INSTALLATION_KEY", "core_installation_key"), ("INSTALLATION_KEY", "installation_key")]:
            text = re.sub(rf"\b{old}\b", new + "()", text)
            text = re.sub(rf"((?:pub )?use [^;\n]*::{new})\(\);", r"\1;", text)
        texts[path] = text

    # Namespace both mutex participants, including the runner under a different OS token.
    replace("setup_mutex.rs", 'to_wide(r"Global\\MiniCodeSandboxSetup")', 'to_wide(&crate::owner_identity::owner().setup_mutex)')
    for file in ["setup_provisioning/read_acl_mutex.rs", "bin/command_runner/win.rs"]:
        replace(file, 'const READ_ACL_MUTEX_NAME: &str = "Local\\\\MiniCodeSandboxReadAcl";', 'fn read_acl_mutex_name() -> &\'static str { codex_windows_sandbox::windows_sandbox_read_acl_mutex() }' if file.startswith("bin/") else 'fn read_acl_mutex_name() -> &\'static str { &crate::owner_identity::owner().read_acl_mutex }')
        path = source_dir / file
        texts[path] = texts[path].replace('OsStr::new(READ_ACL_MUTEX_NAME)', 'OsStr::new(read_acl_mutex_name())')

    # A READ carveout leaves a DENY ACE attached to its capability SID. Reusing
    # that SID after an explicit WRITE authorization would still deny the write.
    # Preserve old SIDs for running commands and select a cache per effective
    # policy; actual filesystem paths remain the innermost cache keys.
    replace("cap.rs", '    pub writable_root_by_path: HashMap<String, String>,', '''    pub writable_root_by_path: HashMap<String, String>,
    #[serde(default)]
    pub workspace_by_policy: HashMap<String, HashMap<String, String>>,
    #[serde(default)]
    pub writable_root_by_policy: HashMap<String, HashMap<String, String>>,''')
    replace("cap.rs", 'writable_root_by_path: HashMap::new(),', '''writable_root_by_path: HashMap::new(),
        workspace_by_policy: HashMap::new(),
        writable_root_by_policy: HashMap::new(),''', 2)
    for legacy_map, policy_map, argument in [
        ("workspace_by_cwd", "workspace_by_policy", "cwd"),
        ("writable_root_by_path", "writable_root_by_policy", "root"),
    ]:
        selected_map = policy_map + "_paths"
        replace("cap.rs", f'''    let key = canonical_path_key({argument});
    if let Some(sid) = caps.{legacy_map}.get(&key) {{''', f'''    let key = canonical_path_key({argument});
    let {selected_map} = match std::env::var_os("MINICODE_WINDOWS_SANDBOX_POLICY_SCOPE") {{
        Some(scope) => caps.{policy_map}.entry(scope.to_string_lossy().into_owned()).or_default(),
        None => &mut caps.{legacy_map},
    }};
    if let Some(sid) = {selected_map}.get(&key) {{''')
        replace("cap.rs", f'    caps.{legacy_map}.insert(key, sid.clone());', f'    {selected_map}.insert(key, sid.clone());')

    # The upstream full-disk READ shortcut returns no explicit roots, losing
    # declared metadata such as a linked worktree's common .git directory.
    # Resolve those entries separately while retaining the original policy's
    # effective deny decisions.
    replace("resolved_permissions.rs", '''        self.file_system
            .get_readable_roots_with_cwd(cwd)
            .into_iter()
            .map(AbsolutePathBuf::into_path_buf)
            .collect()''', '''        let mut explicit = self.file_system.clone();
        explicit.entries.retain(|entry| {
            !matches!(&entry.path, FileSystemPath::Special { value: Root })
                || !entry.access.can_read()
        });
        explicit.get_readable_roots_with_cwd(cwd)
            .into_iter()
            .filter(|path| self.file_system.can_read_local_path_with_cwd(path.as_path(), cwd))
            .map(AbsolutePathBuf::into_path_buf)
            .collect()''')
    replace("resolved_permissions.rs", '    pub(crate) fn uses_write_capabilities_for_cwd(', '''    pub(crate) fn readable_launch_ancestors_for_cwd(&self, cwd: &Path) -> Vec<PathBuf> {
        self.readable_roots_for_cwd(cwd).into_iter().chain(std::iter::once(cwd.to_path_buf()))
            .flat_map(|root| root.ancestors().skip(1).map(Path::to_path_buf).collect::<Vec<_>>())
            .filter(|path| path.parent().is_some() && self.file_system.can_read_local_path_with_cwd(path, cwd))
            .collect()
    }

    pub(crate) fn uses_write_capabilities_for_cwd(''')
    for file in ["setup.rs", "setup_provisioning.rs"]:
        replace(file, '    read_roots: Vec<PathBuf>,', '    read_roots: Vec<PathBuf>,\n    #[serde(default)]\n    launch_read_roots: Vec<PathBuf>,')
        replace(file, '    launch_read_roots: Vec<PathBuf>,', '    launch_read_roots: Vec<PathBuf>,\n    #[serde(default)]\n    launch_read_ancestors: Vec<PathBuf>,')
    replace("setup.rs", '    let (read_roots, write_roots) = build_payload_roots(&request, &overrides, runtime);', '''    let (read_roots, write_roots) = build_payload_roots(&request, &overrides, runtime);
    let launch_read_roots = request.permissions.readable_roots_for_cwd(request.command_cwd)
        .into_iter().filter(|root| root.parent().is_some()).collect();''')
    replace("setup.rs", '    let deny_read_paths = build_payload_deny_read_paths(overrides.deny_read_paths);', '    let launch_read_ancestors = request.permissions.readable_launch_ancestors_for_cwd(request.command_cwd);\n    let deny_read_paths = build_payload_deny_read_paths(overrides.deny_read_paths);')
    if '        launch_read_ancestors,\n        write_roots,' not in texts[source_dir / "setup.rs"]:
        replace("setup.rs", '        read_roots,\n        write_roots,', '        read_roots,\n        launch_read_roots,\n        write_roots,')
    replace("setup.rs", '        launch_read_roots,', '        launch_read_roots,\n        launch_read_ancestors,')
    replace("setup.rs", '        read_roots: Vec::new(),', '        read_roots: Vec::new(),\n        launch_read_roots: Vec::new(),', 2)
    replace("setup_provisioning/service.rs", '        read_roots: Vec::new(),', '        read_roots: Vec::new(),\n        launch_read_roots: Vec::new(),')
    for file, count in [("setup.rs", 2), ("setup_provisioning/service.rs", 1)]:
        replace(file, '        launch_read_roots: Vec::new(),', '        launch_read_roots: Vec::new(),\n        launch_read_ancestors: Vec::new(),', count)
    replace("setup_provisioning/acl_tests.rs", '            read_roots: Vec::new(),', '            read_roots: Vec::new(),\n            launch_read_roots: Vec::new(),\n            launch_read_ancestors: Vec::new(),')

    # Runner logon uses command_cwd before the background read helper can run.
    # Finish only its already-authorized RX root here; other read roots retain
    # their existing background path. Both paths share the same ACL operation.
    read_acl_body = '''    if !payload.read_roots.is_empty() {
        let users_sid = resolve_sid("Users")?;
        let users_psid = sid_bytes_to_psid(&users_sid)?;
        let auth_sid = resolve_sid("Authenticated Users")?;
        let auth_psid = sid_bytes_to_psid(&auth_sid)?;
        let everyone_sid = resolve_sid("Everyone")?;
        let everyone_psid = sid_bytes_to_psid(&everyone_sid)?;
        let rx_psids = vec![users_psid, auth_psid, everyone_psid];
        let subjects = ReadAclSubjects {
            sandbox_group_psid,
            rx_psids: &rx_psids,
        };
        apply_read_acls(
            &payload.read_roots,
            &subjects,
            log,
            &mut refresh_errors,
            FILE_GENERIC_READ | FILE_GENERIC_EXECUTE,
            "read",
            OBJECT_INHERIT_ACE | CONTAINER_INHERIT_ACE,
        )?;
        unsafe {
            if !users_psid.is_null() {
                LocalFree(users_psid as HLOCAL);
            }
            if !auth_psid.is_null() {
                LocalFree(auth_psid as HLOCAL);
            }
            if !everyone_psid.is_null() {
                LocalFree(everyone_psid as HLOCAL);
            }
        }
    }
'''
    has_read_acl_helper = 'fn apply_read_acls_for_sandbox_group(' in texts[source_dir / "setup_provisioning.rs"]
    if not has_read_acl_helper:
        replace("setup_provisioning.rs", read_acl_body, '''    apply_read_acls_for_sandbox_group(
        &payload.read_roots, sandbox_group_psid, log, &mut refresh_errors,
    )?;
''')
    shared_read_acl_body = read_acl_body.replace('&payload.read_roots', 'read_roots').replace('payload.read_roots', 'read_roots').replace('&mut refresh_errors,', 'refresh_errors,')
    if not has_read_acl_helper:
        replace("setup_provisioning.rs", 'fn run_read_acl_only(', '''fn apply_read_acls_for_sandbox_group(
    read_roots: &[PathBuf],
    sandbox_group_psid: *mut c_void,
    log: &mut dyn Write,
    refresh_errors: &mut Vec<String>,
) -> Result<()> {
''' + shared_read_acl_body + '''    Ok(())
}

fn run_read_acl_only(''')
    old_launch_read_acl = '''    // This root has already passed the read-policy and deny-path filters.
    // Keep RX inheritance identical to the background grant so a root-only ACE
    // cannot make its existing children look prepared when they are not.
    let cwd_key = crate::path_normalization::canonical_path_key(&payload.command_cwd);
    let launch_read_roots: Vec<PathBuf> = payload.read_roots.iter()
        .filter(|root| crate::path_normalization::canonical_path_key(root) == cwd_key)
        .cloned()
        .collect();
    apply_read_acls_for_sandbox_group(
        &launch_read_roots, sandbox_group_psid, log, &mut refresh_errors,
    )?;

    if payload.read_roots.is_empty() {'''
    launch_read_acl = '''    // Synchronize only cwd and explicitly declared concrete roots which survived
    // the existing read/deny filters. Platform and profile-wide preparation keeps
    // its background path, so this does not scan the drive before each command.
    let cwd_key = crate::path_normalization::canonical_path_key(&payload.command_cwd);
    let launch_keys: HashSet<String> = payload.launch_read_roots.iter()
        .map(|root| crate::path_normalization::canonical_path_key(root)).collect();
    let launch_read_roots: Vec<PathBuf> = payload.read_roots.iter()
        .filter(|root| {
            let key = crate::path_normalization::canonical_path_key(root);
            key == cwd_key || launch_keys.contains(&key)
        })
        .cloned()
        .collect();
    apply_read_acls_for_sandbox_group(
        &launch_read_roots, sandbox_group_psid, log, &mut refresh_errors,
    )?;

    if payload.read_roots.is_empty() {'''
    if '&payload.launch_read_ancestors, sandbox_group_psid' not in texts[source_dir / "setup_provisioning.rs"]:
        read_acl_anchor = old_launch_read_acl if old_launch_read_acl in texts[source_dir / "setup_provisioning.rs"] else '    if payload.read_roots.is_empty() {'
        replace("setup_provisioning.rs", read_acl_anchor, launch_read_acl)
    if '    inheritance: u32,\n    access_mask: u32,\n) -> Result<()> {' not in texts[source_dir / "setup_provisioning.rs"]:
        replace("setup_provisioning.rs", '''fn apply_read_acls_for_sandbox_group(
    read_roots: &[PathBuf],
    sandbox_group_psid: *mut c_void,
    log: &mut dyn Write,
    refresh_errors: &mut Vec<String>,
)''', '''fn apply_read_acls_for_sandbox_group(
    read_roots: &[PathBuf],
    sandbox_group_psid: *mut c_void,
    log: &mut dyn Write,
    refresh_errors: &mut Vec<String>,
    inheritance: u32,
)''')
    replace("setup_provisioning.rs", '    inheritance: u32,\n) -> Result<()> {\n    if !read_roots.is_empty() {', '    inheritance: u32,\n    access_mask: u32,\n) -> Result<()> {\n    if !read_roots.is_empty() {')
    replace("setup_provisioning.rs", '            "read",\n            OBJECT_INHERIT_ACE | CONTAINER_INHERIT_ACE,', '            "read",\n            inheritance,')
    replace("setup_provisioning.rs", '            refresh_errors,\n            FILE_GENERIC_READ | FILE_GENERIC_EXECUTE,\n            "read",\n            inheritance,', '            refresh_errors,\n            access_mask,\n            "read",\n            inheritance,')
    replace("setup_provisioning.rs", '        &payload.read_roots, sandbox_group_psid, log, &mut refresh_errors,', '        &payload.read_roots, sandbox_group_psid, log, &mut refresh_errors,\n        OBJECT_INHERIT_ACE | CONTAINER_INHERIT_ACE,')
    replace("setup_provisioning.rs", '        &launch_read_roots, sandbox_group_psid, log, &mut refresh_errors,', '        &launch_read_roots, sandbox_group_psid, log, &mut refresh_errors,\n        OBJECT_INHERIT_ACE | CONTAINER_INHERIT_ACE,')
    for roots in ["payload.read_roots", "launch_read_roots"]:
        replace("setup_provisioning.rs", f'        &{roots}, sandbox_group_psid, log, &mut refresh_errors,\n        OBJECT_INHERIT_ACE | CONTAINER_INHERIT_ACE,', f'        &{roots}, sandbox_group_psid, log, &mut refresh_errors,\n        OBJECT_INHERIT_ACE | CONTAINER_INHERIT_ACE, FILE_GENERIC_READ | FILE_GENERIC_EXECUTE,')
    if '&payload.launch_read_ancestors, sandbox_group_psid' not in texts[source_dir / "setup_provisioning.rs"]:
        replace("setup_provisioning.rs", '    let cwd_key = crate::path_normalization::canonical_path_key(&payload.command_cwd);', '''    apply_read_acls_for_sandbox_group(
        &payload.launch_read_ancestors, sandbox_group_psid, log, &mut refresh_errors,
        0,
    )?;
    let cwd_key = crate::path_normalization::canonical_path_key(&payload.command_cwd);''')
    replace("setup_provisioning.rs", '        &payload.launch_read_ancestors, sandbox_group_psid, log, &mut refresh_errors,\n        0,', '        &payload.launch_read_ancestors, sandbox_group_psid, log, &mut refresh_errors,\n        0, windows_sys::Win32::Storage::FileSystem::FILE_READ_ATTRIBUTES | windows_sys::Win32::Storage::FileSystem::FILE_TRAVERSE,')

    # A directory-only grant must not cause SetNamedSecurityInfo to propagate
    # unrelated existing inheritable ACEs through the host's profile tree.
    # SetFileSecurity writes this object's DACL only, preserving the ACL entries,
    # owner, and inheritance-protection state already present on that object.
    if 'GetSecurityDescriptorControl(p_sd, &mut old_control' not in texts[source_dir / "acl.rs"]:
        replace("acl.rs", '''            let code3 = SetNamedSecurityInfoW(
                to_wide(path).as_ptr() as *mut u16,
                1,
                DACL_SECURITY_INFORMATION,
                std::ptr::null_mut(),
                std::ptr::null_mut(),
                p_new_dacl,
                std::ptr::null_mut(),
            );''', '''            let code3 = if inheritance == 0 {
                use windows_sys::Win32::Foundation::GetLastError;
                use windows_sys::Win32::Security::{InitializeSecurityDescriptor, SECURITY_DESCRIPTOR, SetFileSecurityW, SetSecurityDescriptorDacl};
                let mut descriptor: SECURITY_DESCRIPTOR = std::mem::zeroed();
                let descriptor_ptr = std::ptr::addr_of_mut!(descriptor).cast();
                if InitializeSecurityDescriptor(descriptor_ptr, 1) == 0
                    || SetSecurityDescriptorDacl(descriptor_ptr, 1, p_new_dacl, 0) == 0
                    || SetFileSecurityW(to_wide(path).as_ptr(), DACL_SECURITY_INFORMATION, descriptor_ptr) == 0 {
                    GetLastError()
                } else { ERROR_SUCCESS }
            } else {
                SetNamedSecurityInfoW(
                    to_wide(path).as_ptr() as *mut u16,
                    1,
                    DACL_SECURITY_INFORMATION,
                    std::ptr::null_mut(),
                    std::ptr::null_mut(),
                    p_new_dacl,
                    std::ptr::null_mut(),
                )
            };''')
    replace("acl.rs", '                use windows_sys::Win32::Security::{InitializeSecurityDescriptor, SECURITY_DESCRIPTOR, SetFileSecurityW, SetSecurityDescriptorDacl};', '                use windows_sys::Win32::Security::{GetSecurityDescriptorControl, InitializeSecurityDescriptor, SECURITY_DESCRIPTOR, SE_DACL_AUTO_INHERITED, SE_DACL_AUTO_INHERIT_REQ, SE_DACL_PROTECTED, SetFileSecurityW, SetSecurityDescriptorControl, SetSecurityDescriptorDacl};')
    replace("acl.rs", '''                if InitializeSecurityDescriptor(descriptor_ptr, 1) == 0
                    || SetSecurityDescriptorDacl''', '''                let mut old_control = 0;
                let mut revision = 0;
                let mode = SE_DACL_PROTECTED | SE_DACL_AUTO_INHERITED | SE_DACL_AUTO_INHERIT_REQ;
                if GetSecurityDescriptorControl(p_sd, &mut old_control, &mut revision) == 0
                    || InitializeSecurityDescriptor(descriptor_ptr, 1) == 0
                    || SetSecurityDescriptorControl(descriptor_ptr, mode, old_control & mode) == 0
                    || SetSecurityDescriptorDacl''')
    replace("acl_tests.rs", '#[test]\nfn revoking_absent_sid_preserves_child_null_dacl()', '''#[test]
fn noninheriting_grant_preserves_existing_child_security() {
    let parent = tempfile::tempdir().expect("parent directory");
    unsafe {
        let (_, descriptor) = super::fetch_dacl_handle(parent.path()).expect("parent descriptor");
        let protected = windows_sys::Win32::Security::SE_DACL_PROTECTED;
        let control = windows_sys::Win32::Security::SetSecurityDescriptorControl(descriptor, protected, protected);
        assert_ne!(control, 0, "protect fixture descriptor");
        let set = windows_sys::Win32::Security::SetFileSecurityW(
            crate::winutil::to_wide(parent.path()).as_ptr(),
            windows_sys::Win32::Security::DACL_SECURITY_INFORMATION,
            descriptor,
        );
        LocalFree(descriptor as HLOCAL);
        assert_ne!(set, 0, "protect fixture parent");
    }
    let child = parent.path().join("child.txt");
    std::fs::write(&child, "host content").expect("create child");
    let sid = LocalSid::from_string("S-1-5-21-10-20-30-42").expect("test SID");
    let snapshot = || unsafe {
        let (_, descriptor) = super::fetch_dacl_handle(&child).expect("child descriptor");
        let length = windows_sys::Win32::Security::GetSecurityDescriptorLength(descriptor);
        let bytes = std::slice::from_raw_parts(descriptor.cast::<u8>(), length as usize).to_vec();
        LocalFree(descriptor as HLOCAL);
        bytes
    };
    let before = snapshot();
    let mask = windows_sys::Win32::Storage::FileSystem::FILE_READ_ATTRIBUTES
        | windows_sys::Win32::Storage::FileSystem::FILE_TRAVERSE;
    assert!(unsafe {
        super::ensure_allow_mask_aces_with_inheritance(parent.path(), &[sid.as_ptr()], mask, 0)
    }.expect("grant directory attributes"));
    assert!(super::path_mask_allows(parent.path(), &[sid.as_ptr()], mask, true).expect("parent access"));
    assert_eq!(snapshot(), before, "directory-only grant must leave child security unchanged");
    unsafe {
        let (_, descriptor) = super::fetch_dacl_handle(parent.path()).expect("updated parent descriptor");
        let mut control = 0;
        let mut revision = 0;
        let valid = windows_sys::Win32::Security::GetSecurityDescriptorControl(descriptor, &mut control, &mut revision);
        LocalFree(descriptor as HLOCAL);
        assert_ne!(valid, 0);
        assert_ne!(control & windows_sys::Win32::Security::SE_DACL_PROTECTED, 0, "preserve parent inheritance mode");
    }
}

#[test]
fn revoking_absent_sid_preserves_child_null_dacl()''')
    unit_path = source_dir / "acl_tests.rs"
    unit_marker = '#[test]\nfn noninheriting_grant_preserves_existing_child_security()'
    if texts[unit_path].count(unit_marker) == 2:
        first = texts[unit_path].index(unit_marker)
        second = texts[unit_path].index(unit_marker, first + len(unit_marker))
        texts[unit_path] = texts[unit_path][:first] + texts[unit_path][second:]

    # Guest-created files belong to the guest account. OWNER RIGHTS alone would
    # then leave the host unable to read its own workspace (including Git refs).
    # Give only the bound host SID inherited access on existing WRITE roots;
    # this SID is absent from guest tokens and uses no sandbox deny replacement.
    replace("setup_provisioning.rs", '    let mut seen_write_roots: HashSet<PathBuf> = HashSet::new();', '''    let mut seen_write_roots: HashSet<PathBuf> = HashSet::new();
    let host_user_psid = unsafe {
        convert_string_sid_to_sid(&crate::owner_identity::owner().user_sid)
            .ok_or_else(|| anyhow::anyhow!("convert host owner SID failed"))?
    };''')
    replace("setup_provisioning.rs", '        let root_cap_sid_str =\n            workspace_write_cap_sid_for_root(', '''        unsafe {
            ensure_allow_mask_aces_with_inheritance(
                root, &[host_user_psid],
                FILE_GENERIC_READ | FILE_GENERIC_WRITE | FILE_GENERIC_EXECUTE | DELETE,
                CONTAINER_INHERIT_ACE | OBJECT_INHERIT_ACE,
            ).context("preserve host access to sandbox-written files")?;
        }
        let root_cap_sid_str =
            workspace_write_cap_sid_for_root(''')
    replace("setup_provisioning.rs", '    if refresh_only && !refresh_errors.is_empty() {', '''    unsafe { LocalFree(host_user_psid as HLOCAL); }
    if refresh_only && !refresh_errors.is_empty() {''')

    # No new owner may reach a pre-v3 global service, including a package-family hint.
    replace("service_identity.rs", 'Ok("MiniCodeSandboxService".into())', 'Ok(crate::owner_identity::owner().service.clone())')
    replace("service_identity.rs", 'format!("MiniCodeSandboxService.{name}")', 'format!("{}.{name}", crate::owner_identity::owner().service)')
    replace("service_identity.rs", 'format!("{}.{}", crate::SANDBOX_PROVISIONING_PIPE_NAME, family)', 'format!("{}.{family}", crate::owner_identity::owner().pipe)')
    replace("service_identity.rs", 'None => crate::SANDBOX_PROVISIONING_PIPE_NAME.into()', 'None => crate::owner_identity::owner().pipe.clone()')

    replace("wrapper.rs", '    let request = parse_windows_sandbox_wrapper_args(args)?;', '    let request = parse_windows_sandbox_wrapper_args(args)?;\n    crate::owner_identity::bind_current(&request.codex_home)?;')
    replace("setup_provisioning.rs", '    *setup_mode = Some(payload.mode);', '    crate::owner_identity::bind_for_user(&payload.codex_home, &payload.real_user)?;\n    anyhow::ensure!(payload.offline_username == crate::setup::offline_username() && payload.online_username == crate::setup::online_username(), "setup payload accounts do not match its owner");\n    *setup_mode = Some(payload.mode);')
    replace("setup_provisioning.rs", '        anyhow::ensure!(\n            crate::runtime_ownership::load_installation()?\n                .is_none_or(|record| record.runtime.is_none()),\n            "registered Core owns these sandbox accounts; helper provisioning is not permitted"\n        );', '''        let owner = crate::owner_identity::owner();
        if let Some(record) = crate::runtime_ownership::load_installation()? {
            anyhow::ensure!(record.user_sid == owner.user_sid && crate::path_normalization::canonical_path_key(&record.codex_home) == crate::path_normalization::canonical_path_key(&owner.home), "sandbox installation belongs to a different owner");
            anyhow::ensure!(record.runtime.is_none(), "registered Core requires service-owned provisioning");
        } else {
            crate::runtime_ownership::save_installation(&crate::InstallationRecord {
                user_sid: owner.user_sid.clone(), codex_home: owner.home.clone(),
                session_id: 0, desktop_installation: None, runtime: None,
            })?;
        }''')
    replace("setup_provisioning/service.rs", '    let payload = Payload {', '    crate::owner_identity::bind_for_user(codex_home, real_user)?;\n    let payload = Payload {')

    # Identity belongs to both marker and ciphertext record, not just their version.
    for file in ["setup.rs", "setup_provisioning/sandbox_users.rs"]:
        for type_name in ["SetupMarker", "SandboxUsersFile"]:
            prefix = "pub " if file == "setup.rs" else ""
            replace(file, f'{prefix}struct {type_name} {{\n', f'{prefix}struct {type_name} {{\n    {prefix}owner_id: String,\n')
    for path, text in list(texts.items()):
        # Every reviewed constructor gets the bound identity. Test-only literals are
        # typed fixtures; do not invent an owner from a missing process binding.
        if path.name in {"setup.rs", "identity.rs", "identity_integration_tests.rs"}:
            text = re.sub(r'(SetupMarker \{\n)(\s+)(version:)', r'\1\2owner_id: crate::owner_identity::owner().owner_id.clone(),\n\2\3', text)
        if path.name == "identity_integration_tests.rs":
            text = re.sub(r'(SandboxUsersFile \{\n)(\s+)(version:)', r'\1\2owner_id: crate::owner_identity::owner().owner_id.clone(),\n\2\3', text)
        texts[path] = text
    replace("setup_provisioning/sandbox_users.rs", '    let users = SandboxUsersFile {', '    let users = SandboxUsersFile {\n        owner_id: crate::owner_identity::owner().owner_id.clone(),')
    replace("setup_provisioning/sandbox_users.rs", '    let marker = SetupMarker {', '    let marker = SetupMarker {\n        owner_id: crate::owner_identity::owner().owner_id.clone(),')
    replace("setup.rs", 'impl SetupMarker {\n    pub fn version_matches(&self) -> bool {\n        self.version == SETUP_VERSION', 'impl SetupMarker {\n    pub fn version_matches(&self) -> bool {\n        self.offline_username == offline_username() && self.online_username == online_username() && self.version == SETUP_VERSION && self.owner_id == crate::owner_identity::owner().owner_id')
    replace("setup.rs", 'impl SandboxUsersFile {\n    pub fn version_matches(&self) -> bool {\n        self.version == SETUP_VERSION', 'impl SandboxUsersFile {\n    pub fn version_matches(&self) -> bool {\n        self.offline.username == offline_username() && self.online.username == online_username() && self.version == SETUP_VERSION && self.owner_id == crate::owner_identity::owner().owner_id')
    replace("identity.rs", '    let password = decode_password(&chosen)?;', '    let expected = if network_identity.uses_offline_identity() { crate::setup::offline_username() } else { crate::setup::online_username() };\n    ensure!(chosen.username == expected, "credential account does not match sandbox owner");\n    let password = decode_password(&chosen)?;')

    # Protect machine-DPAPI ciphertext before it is written, not after the reset.
    replace("setup_provisioning/sandbox_users.rs", '    mode: SetupMode,\n) -> Result<()> {\n    let group', '    mode: SetupMode,\n    payload: &super::Payload,\n) -> Result<()> {\n    let group')
    replace("setup_provisioning/sandbox_users.rs", '    let offline_password = random_password();', '    super::lock_persistent_sandbox_dirs(payload, &resolve_sandbox_users_group_sid()?)?;\n    let offline_password = random_password();')
    replace("setup_provisioning.rs", '        payload.mode,\n    );', '        payload.mode,\n        payload,\n    );')
    replace("setup_provisioning.rs", '        FILE_GENERIC_READ | FILE_GENERIC_WRITE | FILE_GENERIC_EXECUTE,\n        DaclInheritance::Inherited,\n        payload.mode,\n    )\n    .map_err(|err| {\n        anyhow::Error::new(SetupFailure::new(\n            SetupErrorCode::HelperSandboxLockFailed,\n            format!(\n                "lock sandbox secrets dir', '        FILE_GENERIC_READ | FILE_GENERIC_WRITE | FILE_GENERIC_EXECUTE,\n        DaclInheritance::Protected,\n        payload.mode,\n    )\n    .map_err(|err| {\n        anyhow::Error::new(SetupFailure::new(\n            SetupErrorCode::HelperSandboxLockFailed,\n            format!(\n                "lock sandbox secrets dir')
    # Native API failures are not permission to try a password reset. A same-name
    # account must have the full owner receipt before its password is changed.
    replace("setup_provisioning/sandbox_users.rs", '    let pwd_w = to_wide(OsStr::new(password));', '    let pwd_w = to_wide(OsStr::new(password));\n    let comment = to_wide(format!("MiniCode sandbox owner {}", crate::owner_identity::owner().owner_id));')
    replace("setup_provisioning/sandbox_users.rs", '            usri1_comment: std::ptr::null_mut(),', '            usri1_comment: comment.as_ptr() as *mut u16,')
    replace("setup_provisioning/sandbox_users.rs", '        if status != NERR_Success {\n            // Reset only', '''        if status != NERR_Success {
            use windows_sys::Win32::NetworkManagement::NetManagement::{NERR_UserExists, NetUserGetInfo, NetApiBufferFree};
            anyhow::ensure!(status == NERR_UserExists, "NetUserAdd failed for {name}: {status}");
            let mut buffer = std::ptr::null_mut();
            let query = NetUserGetInfo(std::ptr::null(), name_w.as_ptr(), 1, &mut buffer);
            anyhow::ensure!(query == NERR_Success, "read sandbox account owner failed: {query}");
            let actual = (*buffer.cast::<USER_INFO_1>()).usri1_comment;
            let mut length = 0;
            if !actual.is_null() { while *actual.add(length) != 0 { length += 1; } }
            let matches = !actual.is_null() && std::slice::from_raw_parts(actual, length) == &comment[..comment.len()-1];
            NetApiBufferFree(buffer.cast());
            anyhow::ensure!(matches, "existing local account does not belong to sandbox owner: {name}");
            // Reset only''')
    replace("setup_provisioning.rs", '    if repairing_disabled_accounts {\n        // Ordinary setup keeps its best-effort WFP behavior. Recovery must not reopen logons\n        // after cleanup removed protections unless restoring those protections succeeded.\n        wfp_result?;', '    wfp_result?;\n    if repairing_disabled_accounts {')

    # Parent sends owner authority in framed IPC, never in the workload environment.
    replace("elevated/ipc_framed.rs", 'pub const IPC_PROTOCOL_VERSION: u8 = 6;', 'pub const IPC_PROTOCOL_VERSION: u8 = 7;')
    replace("elevated/ipc_framed.rs", '    pub real_codex_home: PathBuf,', '    pub real_codex_home: PathBuf,\n    pub owner_sid: String,')
    for file in ["elevated_impl.rs", "unified_exec/backends/elevated.rs"]:
        replace(file, 'real_codex_home: codex_home.to_path_buf(),', 'real_codex_home: crate::owner_identity::owner().home.clone(),\n                owner_sid: crate::owner_identity::owner().user_sid.clone(),')
    for file in ["elevated/ipc_framed.rs", "unified_exec/backends/elevated_tests.rs"]:
        path = source_dir / file
        texts[path] = re.sub(r'(real_codex_home: PathBuf::from\([^\n]+\),)(?!\n\s+owner_sid:)', r'\1\n                    owner_sid: "S-1-5-21-1-2-3-1000".into(),', texts[path])
    replace("bin/command_runner/win.rs", '    let log_dir = req.codex_home.clone();', '    codex_windows_sandbox::bind_windows_sandbox_runner(&req.real_codex_home, &req.owner_sid)?;\n    let log_dir = req.codex_home.clone();')

    # Scope all WFP operations (including DELETE), not Windows layer/condition GUIDs.
    for name in ["PROVIDER_KEY", "SUBLAYER_KEY"]:
        path = source_dir / "wfp.rs"
        pattern = rf'const {name}: GUID = (GUID::from_u128\(0x[0-9a-f]+\));'
        if len(re.findall(pattern, texts[path])) == 0 and f"fn {name.lower()}() -> GUID" in texts[path]:
            continue
        if len(re.findall(pattern, texts[path])) != 1:
            raise RuntimeError(f"WFP key anchor changed: {name}")
        texts[path] = re.sub(pattern, rf'fn {name.lower()}() -> GUID {{ crate::owner_identity::owner().guid(\1) }}', texts[path])
        texts[path] = re.sub(rf'\b{name}\b', name.lower() + "()", texts[path])
    path = source_dir / "wfp.rs"
    texts[path] = texts[path].replace('&spec.key', '&crate::owner_identity::owner().guid(spec.key)').replace('filterKey: spec.key', 'filterKey: crate::owner_identity::owner().guid(spec.key)')
    replace("wfp.rs", '    let engine = Engine::open(INFINITE)?;', '    anyhow::ensure!(account == crate::setup::offline_username(), "WFP account does not match sandbox owner");\n    let engine = Engine::open(INFINITE)?;')
    resources = '''
pub fn windows_sandbox_owner_resources(owner: &crate::OwnerIdentity) -> serde_json::Value {
    fn key(g: GUID) -> String { format!("{:08x}-{:04x}-{:04x}-{:02x}{:02x}-{:02x}{:02x}{:02x}{:02x}{:02x}{:02x}", g.data1,g.data2,g.data3,g.data4[0],g.data4[1],g.data4[2],g.data4[3],g.data4[4],g.data4[5],g.data4[6],g.data4[7]) }
    let mut value = serde_json::to_value(owner).unwrap();
    value["wfp_provider"] = key(owner.guid(GUID::from_u128(0x4b39a5c12b6a5d5b9c8d6b524c06ae0b))).into();
    value["wfp_sublayer"] = key(owner.guid(GUID::from_u128(0xbfa916f0cae0514fb9d234cca4a93bb7))).into();
    value["wfp_filters"] = serde_json::to_value(FILTER_SPECS.iter().map(|spec| key(owner.guid(spec.key))).collect::<Vec<_>>()).unwrap();
    value["firewall_rules"] = serde_json::to_value(["minicode_sandbox_offline_block_outbound", "minicode_sandbox_offline_block_inbound", "minicode_sandbox_offline_block_loopback_tcp", "minicode_sandbox_offline_block_loopback_udp", "minicode_sandbox_offline_allow_loopback_proxy"].map(|name| owner.firewall_name(name))).unwrap();
    value
}
'''
    if 'pub fn windows_sandbox_owner_resources(' not in texts[path]:
        texts[path] += resources
    # Firewall setup's friendly descriptions may stay stable; identities may not.
    replace("setup_provisioning.rs", 'mod firewall;', 'pub(crate) mod firewall;')
    replace("setup_provisioning/firewall.rs", 'struct FirewallComApartment {', 'pub(crate) struct FirewallComApartment {')
    replace("setup_provisioning/firewall.rs", '    fn initialize() -> Result<Self> {', '    pub(crate) fn initialize() -> Result<Self> {')
    replace("uninstall_windows/firewall.rs", 'pub(super) fn cleanup_firewall_rules() -> Result<()> {', 'pub(super) fn cleanup_firewall_rules() -> Result<()> {\n    let _apartment = crate::setup_provisioning::firewall::FirewallComApartment::initialize()?;')
    names = ["OFFLINE_BLOCK_RULE_NAME", "OFFLINE_BLOCK_INBOUND_RULE_NAME", "OFFLINE_BLOCK_LOOPBACK_TCP_RULE_NAME", "OFFLINE_BLOCK_LOOPBACK_UDP_RULE_NAME", "OFFLINE_PROXY_ALLOW_RULE_NAME"]
    path = source_dir / "setup_provisioning/firewall.rs"
    for name in names:
        pattern = rf'const {name}: &str = "([^"]+)";'
        matches = re.findall(pattern, texts[path])
        if not matches and f"fn {name.lower()}() -> String" in texts[path]:
            continue
        if len(matches) != 1:
            raise RuntimeError(f"Firewall anchor changed: {name}")
        base = matches[0]
        # Return owned String; borrow it only for the immediate COM call/spec.
        texts[path] = re.sub(pattern, f'fn {name.lower()}() -> String {{ crate::owner_identity::owner().firewall_name("{base}") }}', texts[path])
        texts[path] = re.sub(rf'\b{name}\b', '&' + name.lower() + '()', texts[path])
    replace("uninstall_windows/firewall.rs", '    for name in [', '    for base in [')
    replace("uninstall_windows/firewall.rs", '    ] {\n        if let Err(error)', '''    ] {
        let name = crate::owner_identity::owner().firewall_name(base);
        match unsafe { rules.Item(&BSTR::from(name.as_str())) } {
            Ok(_) => {}
            Err(error) if error.code().0 == 0x80070002u32 as i32 => continue,
            Err(error) => return Err(error).context("query owner firewall rule during cleanup"),
        }
        if let Err(error)''')
    replace("uninstall_windows/firewall.rs", 'rules.Remove(&BSTR::from(name))', 'rules.Remove(&BSTR::from(name.as_str()))')
    replace("uninstall_windows.rs", 'principals::remove_sandbox_principal("MiniCodeSandboxUsers")', 'principals::remove_sandbox_principal(crate::winutil::sandbox_users_group())')
    replace("uninstall_windows/principals.rs", 'name == "MiniCodeSandboxUsers"', 'name == crate::winutil::sandbox_users_group()')
    replace("uninstall_windows.rs", '        self.users.validate_current()?;', '        if let Some(home) = codex_home {\n            anyhow::ensure!(crate::path_normalization::canonical_path_key(home) == crate::path_normalization::canonical_path_key(&crate::owner_identity::owner().home), "cleanup home does not match sandbox owner");\n        }\n        self.users.validate_current()?;')
    # The MiniCode helper must not grant its group access to public Codex runtimes.
    path = source_dir / "setup_provisioning/setup_runtime_bin.rs"
    texts[path] = texts[path].replace('.join("OpenAI").join("Codex")', '.join("MiniCode")').replace('"codex-runtimes"', '"minicode-runtimes"')
    path = source_dir / "setup_provisioning/setup_runtime_bin_tests.rs"
    texts[path] = texts[path].replace(r'AppData\Local\OpenAI\Codex', r'AppData\Local\MiniCode').replace('codex-runtimes', 'minicode-runtimes')


def _add_full_network_block(text: str) -> str:
    if "minicode_wfp_all_connect_v4" in text:
        raise RuntimeError("MiniCode full-network WFP filters already exist")
    filters = []
    for family in ("v4", "v6"):
        key = uuid.uuid5(NAMESPACE, f"windows-sandbox/wfp/all-connect-{family}")
        filters.append(
            "    FilterSpec {\n"
            f"        key: GUID::from_u128(0x{key.hex}),\n"
            f'        name: "minicode_wfp_all_connect_{family}",\n'
            f'        description: "Block all offline sandbox-account outbound connections {family}",\n'
            f"        layer_key: FWPM_LAYER_ALE_AUTH_CONNECT_{family.upper()},\n"
            "        conditions: &[ConditionSpec::User],\n"
            "    },\n"
        )
    before, ending = text.rsplit("];", 1)
    return before + "".join(filters) + "];" + ending


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path)
    parser.add_argument("--fingerprint", action="store_true")
    args = parser.parse_args()
    native = Path(__file__).resolve().parents[1] / "native-windows-sandbox"
    inputs = [Path(__file__).resolve(), *sorted(p for p in native.rglob("*") if p.is_file() and (p.suffix == ".rs" or p.name == "Cargo.toml"))]
    fingerprint = hashlib.sha256(b"".join(p.name.encode("utf-8") + b"\0" + p.read_bytes() + b"\0" for p in inputs)).hexdigest()
    if args.fingerprint:
        print(fingerprint)
        return 0
    if args.source is None:
        parser.error("--source is required unless --fingerprint is used")
    root = args.source.expanduser().resolve()
    workspace = root / "codex-rs"
    manifest = workspace / "Cargo.toml"
    if f'version = "{UPSTREAM_VERSION}"' not in manifest.read_text(encoding="utf-8"):
        raise RuntimeError(f"Expected Codex source version {UPSTREAM_VERSION}")

    source_dir = workspace / "windows-sandbox-rs" / "src"
    identity_marker = workspace / ".minicode-windows-identity.json"
    filter_specs_path = source_dir / "wfp" / "filter_specs.rs"
    if identity_marker.exists():
        identity = json.loads(identity_marker.read_text(encoding="utf-8"))
        if identity.get("patch_version") == PATCH_VERSION:
            # Refresh only files we generated. Refuse to overwrite locally edited outputs.
            for relative, previous_hash in identity["owned_files"].items():
                target = workspace / relative
                if hashlib.sha256(target.read_bytes()).hexdigest() != previous_hash:
                    raise RuntimeError(f"Generated native source has local changes: {target}")
            texts = {path: path.read_text(encoding="utf-8") for path in source_dir.rglob("*.rs") if path.name not in {"owner_identity.rs", "owner_identity_tests.rs"}}
            _owner_namespace_edits(source_dir, texts)
            for path, text in texts.items():
                if path.read_text(encoding="utf-8") != text:
                    path.write_text(text, encoding="utf-8")
            _copy_native_sources(native, workspace, identity)
            identity["input_sha256"] = fingerprint
            identity_marker.write_text(json.dumps(identity, indent=2) + "\n", encoding="utf-8")
            print("MiniCode Windows sandbox source identity is current")
            return 0
        if identity.get("upstream_version") != UPSTREAM_VERSION or (identity.get("patch_version"), identity.get("wfp_guids")) not in {(1, 14), (2, 16)}:
            raise RuntimeError("Existing Windows sandbox source has an unknown identity")
        texts = {path: path.read_text(encoding="utf-8") for path in source_dir.rglob("*.rs")}
        for relative, expected in {
            "Cargo.toml": "fa5feb190c70a033c7e30b52a00103f3d513e9e29d0beac994bf950b41b1cf42",
            "src/main.rs": "091237e74b0fe69d620876185af77f5ae785bee144e4d8552ac9b939228f6ab9",
        }.items():
            target = workspace / "minicode-sandbox-launcher" / relative
            if hashlib.sha256(target.read_bytes()).hexdigest() != expected:
                raise RuntimeError(f"v2 launcher has local changes; refusing to overwrite: {target}")
        if identity["patch_version"] == 1:
            texts[filter_specs_path] = _add_full_network_block(texts[filter_specs_path])
        _owner_namespace_edits(source_dir, texts)
        _write_owner_sources(native, workspace, texts, identity, fingerprint)
        identity_marker.write_text(json.dumps(identity, indent=2) + "\n", encoding="utf-8")
        print("Upgraded MiniCode Windows sandbox to owner namespace v3")
        return 0

    paths = sorted(source_dir.rglob("*.rs"))
    originals = {path: path.read_text(encoding="utf-8") for path in paths}
    combined = "\n".join(originals.values())
    for old, _new in IDENTITY_REPLACEMENTS:
        if old not in combined:
            raise RuntimeError(f"Upstream identity marker missing: {old}")

    edits: dict[Path, str] = {}
    guid_count = 0
    for path, original in originals.items():
        text = original
        for old, new in IDENTITY_REPLACEMENTS:
            text = text.replace(old, new)
        if path.name in {"wfp.rs", "filter_specs.rs"}:
            def replace_guid(match: re.Match[str]) -> str:
                nonlocal guid_count
                original_guid = match.group(1).replace("_", "")
                if len(original_guid) != 32:
                    return match.group(0)
                guid_count += 1
                minicode_guid = uuid.uuid5(NAMESPACE, f"windows-sandbox/wfp/{original_guid}")
                return f"GUID::from_u128(0x{minicode_guid.hex})"

            text = GUID_RE.sub(replace_guid, text)
        if text != original:
            edits[path] = text
    if guid_count != 14:
        raise RuntimeError(f"Expected 14 WFP GUIDs, found {guid_count}")
    edits[filter_specs_path] = _add_full_network_block(edits[filter_specs_path])
    if len("MiniCodeSbxOffline") > 20 or len("MiniCodeSbxOnline") > 20:
        raise RuntimeError("Windows local account name exceeds the 20-character limit")

    launcher_source = Path(__file__).resolve().parents[1] / "native-windows-sandbox"
    launcher_target = workspace / "minicode-sandbox-launcher"
    if launcher_target.exists():
        raise RuntimeError(f"Launcher source already exists: {launcher_target}")
    members = manifest.read_text(encoding="utf-8")
    if members.count("members = [") != 1:
        raise RuntimeError("Upstream workspace members changed")
    members = members.replace(
        "members = [",
        'members = [\n    "minicode-sandbox-launcher",',
        1,
    )
    for path, text in edits.items():
        originals[path] = text
    _owner_namespace_edits(source_dir, originals)
    manifest.write_text(members, encoding="utf-8")
    shutil.copytree(launcher_source, launcher_target)
    identity = {"upstream_version": UPSTREAM_VERSION, "wfp_guids": 16}
    _write_owner_sources(native, workspace, originals, identity, fingerprint)
    identity_marker.write_text(
        json.dumps(identity, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"Patched {len(edits)} Windows sandbox files and {guid_count + 2} WFP GUIDs")
    return 0


def _copy_native_sources(native: Path, workspace: Path, identity: dict) -> None:
    mappings = {
        native / "Cargo.toml": workspace / "minicode-sandbox-launcher" / "Cargo.toml",
        native / "src" / "main.rs": workspace / "minicode-sandbox-launcher" / "src" / "main.rs",
        native / "owner_identity.rs": workspace / "windows-sandbox-rs" / "src" / "owner_identity.rs",
    }
    test = native / "owner_identity_tests.rs"
    if test.exists():
        mappings[test] = workspace / "windows-sandbox-rs" / "src" / "owner_identity_tests.rs"
        mappings[native / "src" / ".." / "owner_identity_tests.rs"] = workspace / "minicode-sandbox-launcher" / "owner_identity_tests.rs"
    for source, target in mappings.items():
        content = source.read_bytes()
        if not target.exists() or target.read_bytes() != content:
            target.write_bytes(content)
    identity["owned_files"] = {target.relative_to(workspace).as_posix(): hashlib.sha256(target.read_bytes()).hexdigest() for target in mappings.values()}


def _write_owner_sources(native: Path, workspace: Path, texts: dict[Path, str], identity: dict, fingerprint: str) -> None:
    cargo = workspace / "windows-sandbox-rs" / "Cargo.toml"
    content = cargo.read_text(encoding="utf-8")
    if 'sha2 = { workspace = true }' in content:
        raise RuntimeError("Owner namespace dependency already present before upgrade")
    content = content.replace('[dependencies]\n', '[dependencies]\nsha2 = { workspace = true }\n', 1)
    for path, text in texts.items():
        if path.read_text(encoding="utf-8") != text:
            path.write_text(text, encoding="utf-8")
    cargo.write_text(content, encoding="utf-8")
    _copy_native_sources(native, workspace, identity)
    for key in ["offline_account", "online_account", "group"]:
        identity.pop(key, None)
    identity.update(patch_version=PATCH_VERSION, wfp_guids=16, owner_namespace="windows-sid+canonical-home-v3", input_sha256=fingerprint)


if __name__ == "__main__":
    raise SystemExit(main())
