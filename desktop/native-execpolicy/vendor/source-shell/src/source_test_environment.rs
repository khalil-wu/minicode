// Test oracle environment support extracted unchanged from pinned Codex protocol/src/shell_environment.rs.
pub const CODEX_SESSION_ID_ENV_VAR: &str = "CODEX_SESSION_ID";
pub const CODEX_THREAD_ID_ENV_VAR: &str = "CODEX_THREAD_ID";
pub const CODEX_EXEC_SERVER_NOISE_AUTH_TOKEN_ENV_VAR: &str = "CODEX_EXEC_SERVER_NOISE_AUTH_TOKEN";
pub const OPENAI_FEDERATION_RULE_ID_ENV_VAR: &str = "OPENAI_FEDERATION_RULE_ID";
pub const OPENAI_IDENTITY_TOKEN_FILE_ENV_VAR: &str = "OPENAI_IDENTITY_TOKEN_FILE";
pub const OPENAI_WORKLOAD_IDENTITY_CONTEXT_ENV_VAR: &str = "OPENAI_WORKLOAD_IDENTITY_CONTEXT";

/// Environment variables that model-reachable child processes must not inherit.
pub const NON_INHERITABLE_ENV_VARS: &[&str] = &[
    CODEX_EXEC_SERVER_NOISE_AUTH_TOKEN_ENV_VAR,
    "NODE_REPL_AUTH_TOKEN",
    "CODEX_GUARDIAN_DECISIONS_API_KEY",
    OPENAI_FEDERATION_RULE_ID_ENV_VAR,
    OPENAI_IDENTITY_TOKEN_FILE_ENV_VAR,
    OPENAI_WORKLOAD_IDENTITY_CONTEXT_ENV_VAR,
];

pub fn is_non_inheritable_env_var(name: &str) -> bool {
    NON_INHERITABLE_ENV_VARS
        .iter()
        .any(|restricted| restricted.eq_ignore_ascii_case(name))
}

/// Configures a child command to omit non-inheritable variables from the
/// process environment and explicit command overrides.
///
/// This prevents accidental propagation of Codex launch context; it is not a
/// filesystem security boundary for the referenced identity-token file.
pub fn scrub_non_inheritable_env_vars(command: &mut std::process::Command) {
    let configured_names = command
        .get_envs()
        .map(|(name, _)| name.to_os_string())
        .collect::<Vec<_>>();

    for name in NON_INHERITABLE_ENV_VARS {
        command.env_remove(name);
    }
    for name in std::env::vars_os()
        .map(|(name, _)| name)
        .chain(configured_names)
    {
        if name.to_str().is_some_and(is_non_inheritable_env_var) {
            command.env_remove(name);
        }
    }
}

