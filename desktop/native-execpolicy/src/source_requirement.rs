// Adapted from pinned OpenAI Codex core/src/exec_policy.rs and
// exec_policy/executable_identity.rs (Apache-2.0). The algorithms below are
// copied from Source; bridge-only types carry the already-resolved host facts.
use codex_execpolicy::{Decision, Evaluation, MatchOptions, Policy, RuleMatch};
use minicode_source_shell as codex_shell_command;
use codex_shell_command::bash::parse_shell_lc_plain_commands;
use codex_shell_command::is_dangerous_command::{DangerousCommandMatch, DangerousCommandPlatform, dangerous_command_match_for_platform};
use codex_shell_command::powershell::{extract_powershell_command, parse_powershell_script_into_plain_commands};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use shlex::try_join as shlex_try_join;
use std::path::{Path, PathBuf};
use crate::source_amendment::{ExecPolicyAmendment, derive_requested_execpolicy_amendment_from_prefix_rule,
    try_derive_execpolicy_amendment_for_allow_rules, try_derive_execpolicy_amendment_for_prompt_rules};

const PROMPT_CONFLICT_REASON: &str = "approval required by policy, but AskForApproval is set to Never";
const REJECT_SANDBOX_APPROVAL_REASON: &str = "approval required by policy, but AskForApproval::Granular.sandbox_approval is false";
const REJECT_RULES_APPROVAL_REASON: &str = "approval required by policy rule, but AskForApproval::Granular.rules is false";

#[derive(Clone, Copy, Debug, Deserialize)]
#[serde(rename_all = "kebab-case")]
pub enum AskForApproval {
    #[serde(rename = "untrusted")]
    UnlessTrusted,
    #[serde(alias = "on-failure")]
    OnRequest,
    Granular(GranularApprovalConfig),
    Never,
}

#[derive(Clone, Copy, Debug, Deserialize)]
pub struct GranularApprovalConfig {
    pub sandbox_approval: bool,
    pub rules: bool,
    #[serde(default)]
    pub skill_approval: bool,
    #[serde(default)]
    pub request_permissions: bool,
    pub mcp_elicitations: bool,
}

impl GranularApprovalConfig {
    const fn allows_sandbox_approval(self) -> bool { self.sandbox_approval }
    const fn allows_rules_approval(self) -> bool { self.rules }
}

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq)]
#[serde(rename_all = "kebab-case")]
pub enum WindowsSandboxLevel { Disabled, RestrictedToken, Elevated }

#[derive(Clone, Copy, Debug, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum SandboxPermissions { UseDefault, RequireEscalated, WithAdditionalPermissions }

impl SandboxPermissions {
    fn requests_sandbox_override(self) -> bool { !matches!(self, Self::UseDefault) }
}

#[derive(Clone, Copy, Debug, Deserialize)]
#[serde(rename_all = "kebab-case")]
pub enum FileSystemSandboxKind { Restricted, Unrestricted, ExternalSandbox }

/// The host resolves the real profile once. No path policy is inferred here.
#[derive(Debug, Deserialize)]
pub struct ResolvedPermissionProfile {
    pub managed: bool,
    pub file_system: ResolvedFileSystemPolicy,
}

#[derive(Debug, Deserialize)]
pub struct ResolvedFileSystemPolicy {
    pub kind: FileSystemSandboxKind,
    pub full_disk_write: bool,
}

impl ResolvedPermissionProfile {
    fn file_system_sandbox_policy(&self) -> &ResolvedFileSystemPolicy { &self.file_system }
}

impl ResolvedFileSystemPolicy {
    fn has_full_disk_write_access(&self) -> bool { self.full_disk_write }
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum SourceShellMode { Normal, ZshFork }

#[derive(Debug, Deserialize)]
pub struct CommandRequirementInput {
    pub command: Vec<String>,
    pub configured_shell: PathBuf,
    pub shell_mode: SourceShellMode,
    pub approval_policy: AskForApproval,
    pub permission_profile: ResolvedPermissionProfile,
    pub windows_sandbox_level: WindowsSandboxLevel,
    pub sandbox_permissions: SandboxPermissions,
    pub prefix_rule: Option<Vec<String>>,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize)]
enum ExecPolicyCommandOrigin { Generic, PowerShell }

#[derive(Clone, Copy)]
struct UnmatchedCommandContext<'a> {
    approval_policy: AskForApproval,
    permission_profile: &'a ResolvedPermissionProfile,
    windows_sandbox_level: WindowsSandboxLevel,
    sandbox_permissions: SandboxPermissions,
    command_origin: ExecPolicyCommandOrigin,
}

#[derive(Debug, Eq, PartialEq)]
struct ExecPolicyCommands {
    commands: Vec<Vec<String>>,
    command_origin: ExecPolicyCommandOrigin,
}

#[derive(Serialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
enum ExecApprovalRequirement {
    Skip { bypass_sandbox: bool, proposed_execpolicy_amendment: Option<ExecPolicyAmendment> },
    NeedsApproval { reason: Option<String>, proposed_execpolicy_amendment: Option<ExecPolicyAmendment> },
    Forbidden { reason: String },
}

pub(crate) fn command_requirement(policy: &Policy, input: CommandRequirementInput, ignored: bool) -> Value {
    let command_platform = DangerousCommandPlatform::host();
    let executable = shell_approval_command(&input.command, &input.configured_shell, &input.shell_mode);
    let parsed = if executable.len() == input.command.len() {
        commands_for_exec_policy_for_platform(&input.command, command_platform)
    } else {
        let mut parsed = if command_platform == DangerousCommandPlatform::Windows {
            match extract_powershell_command(&input.command) {
                Some((_, script)) => ExecPolicyCommands {
                    commands: parse_powershell_script_into_plain_commands(script)
                        .unwrap_or_else(|| vec![input.command.clone()]),
                    command_origin: ExecPolicyCommandOrigin::PowerShell,
                },
                None => commands_for_exec_policy_for_platform(&input.command, command_platform),
            }
        } else { commands_for_exec_policy_for_platform(&input.command, command_platform) };
        // Source: inner commands can restrict an unfamiliar executable, but
        // cannot grant trust to an executable that can ignore its arguments.
        parsed.commands.insert(0, executable.to_vec());
        parsed
    };
    let ExecPolicyCommands { commands, command_origin } = parsed;
    let command = executable;
    let approval_policy = input.approval_policy;
    let permission_profile = input.permission_profile;
    let windows_sandbox_level = input.windows_sandbox_level;
    let sandbox_permissions = input.sandbox_permissions;
    let prefix_rule = input.prefix_rule;
    let exec_policy = policy;
    let auto_amendment_allowed = !ignored;
    let exec_policy_fallback = |cmd: &[String]| {
        render_decision_for_unmatched_command_for_platform(cmd, UnmatchedCommandContext {
            approval_policy, permission_profile: &permission_profile, windows_sandbox_level,
            sandbox_permissions, command_origin,
        }, command_platform)
    };
    let match_options = MatchOptions { resolve_host_executables: true };
    let evaluation = exec_policy.check_multiple_with_options(commands.iter(), &exec_policy_fallback, &match_options);
    let requested_amendment = if auto_amendment_allowed {
        derive_requested_execpolicy_amendment_from_prefix_rule(prefix_rule.as_ref(), &evaluation.matched_rules,
            exec_policy, &commands, &exec_policy_fallback, &match_options)
    } else { None };
    let requirement = match evaluation.decision {
        Decision::Forbidden => ExecApprovalRequirement::Forbidden {
            reason: derive_forbidden_reason(command, &evaluation, dangerous_command_match_for_heuristics(
                &evaluation, Decision::Forbidden, command_origin, command_platform)),
        },
        Decision::Prompt => {
            let prompt_is_rule = evaluation.matched_rules.iter().any(|rule_match|
                is_policy_match(rule_match) && rule_match.decision() == Decision::Prompt);
            match prompt_is_rejected_by_policy(approval_policy, prompt_is_rule) {
                Some(reason) if prompt_is_rule => ExecApprovalRequirement::Forbidden { reason: reason.to_string() },
                Some(reason) => ExecApprovalRequirement::Forbidden {
                    reason: derive_rejected_prompt_reason(reason, dangerous_command_match_for_heuristics(
                        &evaluation, Decision::Prompt, command_origin, command_platform)),
                },
                None => ExecApprovalRequirement::NeedsApproval {
                    reason: derive_prompt_reason(command, &evaluation),
                    proposed_execpolicy_amendment: requested_amendment.or_else(|| {
                        if auto_amendment_allowed { try_derive_execpolicy_amendment_for_prompt_rules(&evaluation.matched_rules) }
                        else { None }
                    }),
                },
            }
        },
        Decision::Allow => ExecApprovalRequirement::Skip {
            bypass_sandbox: commands.iter().all(|command| exec_policy.matches_for_command_with_options(
                command, None, &match_options).iter().any(|rule_match|
                    is_policy_match(rule_match) && rule_match.decision() == Decision::Allow)),
            proposed_execpolicy_amendment: if auto_amendment_allowed {
                try_derive_execpolicy_amendment_for_allow_rules(&evaluation.matched_rules)
            } else { None },
        },
    };
    json!({"requirement": requirement, "evaluation": evaluation, "commands": commands,
        "command_origin": command_origin, "approval_command": command})
}

fn is_policy_match(rule_match: &RuleMatch) -> bool {
    match rule_match {
        RuleMatch::PrefixRuleMatch { .. } => true,
        RuleMatch::HeuristicsRuleMatch { .. } => false,
    }
}

pub(crate) fn prompt_is_rejected_by_policy(
    approval_policy: AskForApproval,
    prompt_is_rule: bool,
) -> Option<&'static str> {
    match approval_policy {
        AskForApproval::Never => Some(PROMPT_CONFLICT_REASON),
        AskForApproval::OnRequest => None,
        AskForApproval::UnlessTrusted => None,
        AskForApproval::Granular(granular_config) => {
            if prompt_is_rule {
                if !granular_config.allows_rules_approval() {
                    Some(REJECT_RULES_APPROVAL_REASON)
                } else {
                    None
                }
            } else if !granular_config.allows_sandbox_approval() {
                Some(REJECT_SANDBOX_APPROVAL_REASON)
            } else {
                None
            }
        }
    }
}

fn dangerous_command_match_for_origin(
    command: &[String],
    command_origin: ExecPolicyCommandOrigin,
    command_platform: DangerousCommandPlatform,
) -> Option<DangerousCommandMatch> {
    match command_origin {
        ExecPolicyCommandOrigin::Generic => {
            dangerous_command_match_for_platform(command, command_platform)
        }
        ExecPolicyCommandOrigin::PowerShell => {
            codex_shell_command::is_dangerous_command::dangerous_powershell_words_match(
                command,
                command_platform,
            )
        }
    }
}

fn dangerous_command_match_for_heuristics(
    evaluation: &Evaluation,
    decision: Decision,
    command_origin: ExecPolicyCommandOrigin,
    command_platform: DangerousCommandPlatform,
) -> Option<DangerousCommandMatch> {
    evaluation
        .matched_rules
        .iter()
        .find_map(|rule_match| match rule_match {
            RuleMatch::HeuristicsRuleMatch {
                command,
                decision: matched_decision,
            } if *matched_decision == decision => {
                dangerous_command_match_for_origin(command, command_origin, command_platform)
            }
            _ => None,
        })
}

fn render_decision_for_unmatched_command_for_platform(
    command: &[String],
    context: UnmatchedCommandContext<'_>,
    command_platform: DangerousCommandPlatform,
) -> Decision {
    let dangerous_command_match =
        dangerous_command_match_for_origin(command, context.command_origin, command_platform);
    let UnmatchedCommandContext {
        approval_policy,
        permission_profile,
        windows_sandbox_level,
        sandbox_permissions,
        command_origin: _,
    } = context;
    let file_system_sandbox_policy = permission_profile.file_system_sandbox_policy();
    // When the Windows sandbox backend is disabled, managed filesystem
    // restrictions are only a policy shape; there is no platform sandbox to
    // enforce the boundary. Keep that legacy case conservative while still
    // relying on the real Windows sandbox when it is enabled.
    let windows_managed_fs_restrictions_without_sandbox_backend = cfg!(windows)
        && windows_sandbox_level == WindowsSandboxLevel::Disabled
        && profile_has_managed_filesystem_restrictions(permission_profile);

    // If the command is flagged as dangerous or we have no sandbox protection,
    // we should never allow it to run without approval.
    //
    // We prefer to prompt the user rather than outright forbid the command,
    // but if the user has explicitly disabled prompts, we must
    // forbid the command.
    if dangerous_command_match.is_some() || windows_managed_fs_restrictions_without_sandbox_backend
    {
        return match approval_policy {
            AskForApproval::Never => Decision::Forbidden,
            AskForApproval::OnRequest
            | AskForApproval::UnlessTrusted
            | AskForApproval::Granular(_) => Decision::Prompt,
        };
    }

    match approval_policy {
        AskForApproval::Never => {
            // We allow the command to run, relying on the sandbox for
            // protection.
            Decision::Allow
        }
        AskForApproval::UnlessTrusted => {
            // Projects marked untrusted require approval for every command
            // that is not explicitly allowed by an exec policy rule.
            Decision::Prompt
        }
        AskForApproval::OnRequest => {
            match file_system_sandbox_policy.kind {
                FileSystemSandboxKind::Unrestricted | FileSystemSandboxKind::ExternalSandbox => {
                    // The user has indicated we should "just run" commands
                    // in their unrestricted environment, so we do so since the
                    // command has not been flagged as dangerous.
                    Decision::Allow
                }
                FileSystemSandboxKind::Restricted => {
                    // In restricted sandboxes, do not prompt for non-escalated,
                    // non-dangerous commands; let the sandbox enforce
                    // restrictions without a user prompt.
                    if sandbox_permissions.requests_sandbox_override() {
                        Decision::Prompt
                    } else {
                        Decision::Allow
                    }
                }
            }
        }
        AskForApproval::Granular(_) => match file_system_sandbox_policy.kind {
            FileSystemSandboxKind::Unrestricted | FileSystemSandboxKind::ExternalSandbox => {
                // Mirror on-request behavior for unmatched commands; prompt-vs-reject is handled
                // by `prompt_is_rejected_by_policy`.
                Decision::Allow
            }
            FileSystemSandboxKind::Restricted => {
                if sandbox_permissions.requests_sandbox_override() {
                    Decision::Prompt
                } else {
                    Decision::Allow
                }
            }
        },
    }
}

fn profile_has_managed_filesystem_restrictions(permission_profile: &ResolvedPermissionProfile) -> bool {
    let file_system_sandbox_policy = permission_profile.file_system_sandbox_policy();
    permission_profile.managed
        && matches!(
            file_system_sandbox_policy.kind,
            FileSystemSandboxKind::Restricted
        )
        && !file_system_sandbox_policy.has_full_disk_write_access()
}

fn commands_for_exec_policy_for_platform(
    command: &[String],
    command_platform: DangerousCommandPlatform,
) -> ExecPolicyCommands {
    if let Some(commands) = parse_shell_lc_plain_commands(command)
        && !commands.is_empty()
    {
        return ExecPolicyCommands {
            commands,
            command_origin: ExecPolicyCommandOrigin::Generic,
        };
    }

    if command_platform == DangerousCommandPlatform::Windows
        && let Some(commands) =
            codex_shell_command::powershell::parse_powershell_command_into_plain_commands(command)
        && !commands.is_empty()
    {
        return ExecPolicyCommands {
            commands,
            command_origin: ExecPolicyCommandOrigin::PowerShell,
        };
    }

    ExecPolicyCommands {
        commands: vec![command.to_vec()],
        command_origin: ExecPolicyCommandOrigin::Generic,
    }
}

fn derive_prompt_reason(command_args: &[String], evaluation: &Evaluation) -> Option<String> {
    let command = render_shlex_command(command_args);

    let most_specific_prompt = evaluation
        .matched_rules
        .iter()
        .filter_map(|rule_match| match rule_match {
            RuleMatch::PrefixRuleMatch {
                matched_prefix,
                decision: Decision::Prompt,
                justification,
                ..
            } => Some((matched_prefix.len(), justification.as_deref())),
            _ => None,
        })
        .max_by_key(|(matched_prefix_len, _)| *matched_prefix_len);

    match most_specific_prompt {
        Some((_matched_prefix_len, Some(justification))) => {
            Some(format!("`{command}` requires approval: {justification}"))
        }
        Some((_matched_prefix_len, None)) => {
            Some(format!("`{command}` requires approval by policy"))
        }
        None => None,
    }
}

fn render_shlex_command(args: &[String]) -> String {
    shlex_try_join(args.iter().map(String::as_str)).unwrap_or_else(|_| args.join(" "))
}

fn derive_forbidden_reason(
    command_args: &[String],
    evaluation: &Evaluation,
    dangerous_command_match: Option<DangerousCommandMatch>,
) -> String {
    let command = render_shlex_command(command_args);

    let most_specific_forbidden = evaluation
        .matched_rules
        .iter()
        .filter_map(|rule_match| match rule_match {
            RuleMatch::PrefixRuleMatch {
                matched_prefix,
                decision: Decision::Forbidden,
                justification,
                ..
            } => Some((matched_prefix, justification.as_deref())),
            _ => None,
        })
        .max_by_key(|(matched_prefix, _)| matched_prefix.len());

    match most_specific_forbidden {
        Some((_matched_prefix, Some(justification))) => {
            format!("`{command}` rejected: {justification}")
        }
        Some((matched_prefix, None)) => {
            let prefix = render_shlex_command(matched_prefix);
            format!("`{command}` rejected: policy forbids commands starting with `{prefix}`")
        }
        None => {
            if let Some(dangerous_command_match) = dangerous_command_match {
                let reason = dangerous_command_rejection_reason(dangerous_command_match);
                format!("`{command}` rejected: {reason}")
            } else {
                format!("`{command}` rejected: blocked by policy")
            }
        }
    }
}

fn derive_rejected_prompt_reason(
    fallback_reason: &str,
    dangerous_command_match: Option<DangerousCommandMatch>,
) -> String {
    match dangerous_command_match {
        Some(dangerous_command_match @ DangerousCommandMatch::ForcedRm) => {
            dangerous_command_rejection_reason(dangerous_command_match).to_string()
        }
        Some(DangerousCommandMatch::Other) | None => fallback_reason.to_string(),
    }
}

fn dangerous_command_rejection_reason(
    dangerous_command_match: DangerousCommandMatch,
) -> &'static str {
    match dangerous_command_match {
        DangerousCommandMatch::ForcedRm => {
            "rm -f style commands are not permitted. Use a safer approach"
        }
        DangerousCommandMatch::Other => "blocked by policy",
    }
}

fn shell_approval_command<'a>(
    command: &'a [String],
    configured_shell: &Path,
    shell_mode: &SourceShellMode,
) -> &'a [String] {
    let Some(executable) = command.first() else {
        return command;
    };
    let executable_path = Path::new(executable);

    #[cfg(windows)]
    let is_system_shell = std::env::var_os("SystemRoot").is_some_and(|system_root| {
        let system_directory = Path::new(&system_root).join("System32");
        let powershell_directory = system_directory.join("WindowsPowerShell").join("v1.0");
        executable_path.parent().is_some_and(|parent| {
            parent
                .as_os_str()
                .eq_ignore_ascii_case(system_directory.as_os_str())
                || parent
                    .as_os_str()
                    .eq_ignore_ascii_case(powershell_directory.as_os_str())
        })
    });

    #[cfg(not(windows))]
    let is_system_shell = executable_path
        .parent()
        .is_some_and(|parent| parent == Path::new("/bin") || parent == Path::new("/usr/bin"));

    #[cfg(windows)]
    let is_configured_shell = executable_path
        .as_os_str()
        .eq_ignore_ascii_case(configured_shell.as_os_str());

    #[cfg(not(windows))]
    let is_configured_shell = executable_path == configured_shell;

    if is_configured_shell
        || is_system_shell
        || matches!(shell_mode, SourceShellMode::ZshFork)
    {
        command
    } else {
        // An unfamiliar executable can ignore its arguments, so evaluate the
        // executable separately from any restrictions on its apparent command.
        std::slice::from_ref(executable)
    }
}
