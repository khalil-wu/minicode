// Included only in Source's bwrap module. This changes the shape of existing
// READ mount targets; the protocol's permission/protected-path rules stay intact.
const MINICODE_READ_METADATA_NAMES: &[&str] = &[
    ".git",
    ".minicode",
    ".mcp.json",
    ".gitconfig",
    ".gitmodules",
    "settings.json",
    "settings.local.json",
];

fn is_protected_metadata_name(name: &std::ffi::OsStr) -> bool {
    codex_protocol::permissions::is_protected_metadata_name(name)
        || MINICODE_READ_METADATA_NAMES
            .iter()
            .any(|metadata_name| name == std::ffi::OsStr::new(metadata_name))
}
