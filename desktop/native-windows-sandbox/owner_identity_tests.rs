use super::*;
use std::collections::BTreeSet;

fn identity(home: &str, sid: &str) -> OwnerIdentity {
    OwnerIdentity::from_canonical(PathBuf::from(home), sid.into())
}

#[test]
fn owner_namespace_is_stable_across_case_and_path_separators() {
    let a = identity(r"C:\Users\Alice\State", "S-1-5-21-1-2-3-1001");
    let b = identity("c:/users/alice/state", "S-1-5-21-1-2-3-1001");
    assert_eq!(a.owner_id, b.owner_id);
    assert_eq!(a.offline_username, b.offline_username);
    assert_eq!(a.online_username, b.online_username);
    assert_eq!(a.offline_username.len(), 19);
    assert_eq!(a.online_username.len(), 19);
    assert_ne!(a.offline_username, a.online_username);
}

#[test]
fn different_home_or_windows_user_has_disjoint_global_resources() {
    let owners = [
        identity(r"C:\State\A", "S-1-5-21-1-2-3-1001"),
        identity(r"C:\State\B", "S-1-5-21-1-2-3-1001"),
        identity(r"C:\State\A", "S-1-5-21-1-2-3-1002"),
    ];
    for field in ["owner_id", "offline_username", "online_username", "group", "installation_key", "core_installation_key", "setup_mutex", "read_acl_mutex", "service", "pipe", "wfp_provider", "wfp_sublayer"] {
        let values = owners.iter().map(|o| crate::windows_sandbox_owner_resources(o)[field].as_str().unwrap().to_owned()).collect::<BTreeSet<_>>();
        assert_eq!(values.len(), owners.len(), "resource collision: {field}");
    }
    for field in ["wfp_filters", "firewall_rules"] {
        let mut all = BTreeSet::new();
        for owner in &owners {
            let resources = crate::windows_sandbox_owner_resources(owner);
            let values = resources[field].as_array().unwrap();
            assert_eq!(values.len(), if field == "wfp_filters" { 14 } else { 5 });
            for value in values { assert!(all.insert(value.as_str().unwrap().to_owned()), "{field} collision"); }
        }
    }
}

#[test]
fn namespaced_wfp_keys_and_accounts_cannot_target_v2_global_resources() {
    let old_keys = [
        0x4b39a5c12b6a5d5b9c8d6b524c06ae0b, 0xbfa916f0cae0514fb9d234cca4a93bb7,
        0x95d20d3a711451bf87a902c15b275378, 0xd51a616a61f055b2849f114419a498d9,
        0x9ebad511017c525898ab42dcaba0477b, 0x4957f061275858e9939f65f07abb12ec,
        0xda80b32340ad5b108d9e37e1c8ed6013, 0xf31060053138559e868cb2b0e1531e87,
        0xc95b3c5cf3055c8b9a2664b1ca85e5df, 0xd7f2804095c25cde957d3b8d75d60210,
        0xf27b3fc6175552e2b47049713f246c80, 0x8c89c8aa64bf590a8584f8214af0d6d5,
        0x77daf944448458a3b562bde035d68310, 0x636833e99e455ea7ab129754b7ac1b5d,
        0xef9b83976e4653f4a138e38500d00ac4, 0x55b3cd86d00753218f392c8a8cd53330,
    ];
    for home in [r"C:\State\A", r"C:\State\B"] {
        let owner = identity(home, "S-1-5-21-1-2-3-1001");
        assert_ne!(owner.offline_username, "MiniCodeSbxOffline");
        assert_ne!(owner.online_username, "MiniCodeSbxOnline");
        assert_ne!(owner.group, "MiniCodeSandboxUsers");
        assert!(!owner.installation_key.contains("OpenAI"));
        for seed in old_keys {
            let result = owner.guid(GUID::from_u128(seed));
            assert!(old_keys.iter().all(|old| {
                let old = GUID::from_u128(*old);
                (result.data1, result.data2, result.data3, result.data4) != (old.data1, old.data2, old.data3, old.data4)
            }));
        }
    }
}

#[test]
fn legacy_and_copied_credential_homes_are_rejected_without_writes() -> Result<()> {
    let source = tempfile::tempdir()?;
    let destination = tempfile::tempdir()?;
    let user = crate::current_setup_user()?;
    let original = crate::windows_sandbox_owner_identity(source.path())?;
    let directory = crate::sandbox_secrets_dir(destination.path());
    std::fs::create_dir(&directory)?;
    let file = directory.join("sandbox_users.json");
    for bytes in [
        br#"{"version":5,"offline":{"username":"MiniCodeSbxOffline","password":"ciphertext"},"online":{"username":"MiniCodeSbxOnline","password":"ciphertext"}}"#.to_vec(),
        serde_json::to_vec(&serde_json::json!({
            "version":6, "owner_id":original.owner_id,
            "offline":{"username":original.offline_username,"password":"ciphertext"},
            "online":{"username":original.online_username,"password":"ciphertext"}
        }))?,
    ] {
        std::fs::write(&file, &bytes)?;
        assert!(crate::bind_windows_sandbox_owner(destination.path(), &user).is_err());
        assert_eq!(std::fs::read(&file)?, bytes);
        assert!(!crate::sandbox_dir(destination.path()).exists());
        assert!(!crate::sandbox_bin_dir(destination.path()).exists());
    }
    Ok(())
}

#[test]
fn namespace_derivation_matches_fixed_cross_language_vector() {
    let owner = identity(r"C:\State\A", "S-1-5-21-1-2-3-1001");
    let expected = Sha256::digest(b"minicode.windows-sandbox.owner.v3\0S-1-5-21-1-2-3-1001\0c:/state/a");
    assert_eq!(owner.owner_id, expected.iter().map(|b| format!("{b:02x}")).collect::<String>());
    // Full receipt is retained even though the Windows account label is shorter.
    assert_eq!(owner.owner_id.len(), 64);
    assert!(owner.group.ends_with(&owner.owner_id));
}
