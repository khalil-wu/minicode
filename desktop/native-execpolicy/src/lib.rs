//! JSON transport for the pinned Source policy engine; no shell execution.
use codex_execpolicy::{Decision, Evaluation, MatchOptions, Policy, PolicyParser, PrefixRule, RuleMatch};
use serde::Deserialize;
use serde_json::{Value, json};
use std::path::PathBuf;
use std::sync::Arc;

mod source_amendment;
mod source_prefix_format;
pub mod source_requirement;
#[path = "../vendor/source-config/requirements_exec_policy.rs"]
pub mod source_requirements;

#[derive(Debug, Deserialize)]
pub struct PolicySource {
    pub path: String,
    pub contents: String,
}

#[derive(Debug, Deserialize)]
pub struct CommandInput {
    pub argv: Vec<String>,
    /// Source's caller-owned unmatched-command decision. This low-level
    /// bridge does not claim to parse shells or implement core heuristics.
    pub unmatched_decision: Decision,
}

#[derive(Debug, Deserialize)]
pub struct PolicyInput {
    pub sources: Vec<PolicySource>,
    pub requirements_rules: Option<source_requirements::RequirementsExecPolicyToml>,
    /// Host-owned prefixes added to the loaded in-memory policy only after
    /// Source's locked durable amendment writer has succeeded.
    #[serde(default)]
    pub approved_prefixes: Vec<Vec<String>>,
}

#[derive(Debug, Deserialize)]
#[serde(tag = "operation", rename_all = "snake_case")]
pub enum Request {
    Inspect {
        #[serde(flatten)]
        policy: PolicyInput,
        ignore_allow_prefix_rules: bool,
    },
    Evaluate {
        #[serde(flatten)]
        policy: PolicyInput,
        environment_sources: Vec<PolicySource>,
        commands: Vec<CommandInput>,
        ignore_allow_prefix_rules: bool,
        prefix_rule: Option<Vec<String>>,
    },
    CommandRequirement {
        #[serde(flatten)]
        policy: PolicyInput,
        environment_sources: Vec<PolicySource>,
        ignore_allow_prefix_rules: bool,
        #[serde(flatten)]
        command: source_requirement::CommandRequirementInput,
    },
    AppendAllowPrefix {
        path: PathBuf,
        prefix: Vec<String>,
    },
    Identity,
}

#[derive(Debug, thiserror::Error)]
pub enum Error {
    #[error("{0}")]
    Policy(#[from] codex_execpolicy::Error),
    #[error("{0}")]
    Amend(#[from] codex_execpolicy::AmendError),
    #[error("{0}")]
    Requirements(#[from] source_requirements::RequirementsExecPolicyParseError),
    #[error("expected at least one parsed command")]
    EmptyCommands,
}

impl Error {
    pub fn json(&self) -> Value {
        let location = match self {
            Self::Policy(error) => error.location().map(|location| json!({
                "path": location.path,
                "start": {"line": location.range.start.line, "column": location.range.start.column},
                "end": {"line": location.range.end.line, "column": location.range.end.column},
            })),
            _ => None,
        };
        json!({"ok": false, "error": {"message": self.to_string(), "location": location}})
    }
}

fn parse(sources: &[PolicySource]) -> Result<Policy, Error> {
    let mut parser = PolicyParser::new();
    for source in sources {
        parser.parse(&source.path, &source.contents)?;
    }
    Ok(parser.build())
}

fn load_policy(input: PolicyInput) -> Result<Policy, Error> {
    let mut policy = parse(&input.sources)?;
    // Source load_exec_policy merges managed requirements before the admitted
    // model's filtering and before the environment-specific overlay.
    if let Some(requirements) = input.requirements_rules {
        policy = policy.merge_overlay(requirements.to_requirements_policy()?.as_ref());
    }
    let options = MatchOptions {resolve_host_executables: true};
    for prefix in input.approved_prefixes {
        // Source append_amendment_and_update checks whether the loaded policy
        // already explicitly allows this command before adding its new rule.
        let evaluation = policy.check_multiple_with_options([&prefix], &|_| Decision::Forbidden, &options);
        let already_allowed = evaluation.decision == Decision::Allow && evaluation.matched_rules.iter().any(
            |matched| matches!(matched, RuleMatch::PrefixRuleMatch {decision: Decision::Allow, ..}));
        if !already_allowed {
            policy.add_prefix_rule(&prefix, Decision::Allow)?;
        }
    }
    Ok(policy)
}

/// Source core/src/exec_policy/model_policy.rs::current_for_prefix_rules.
/// The two preserved collections are essential: ignoring reusable Allow
/// PrefixRules does not erase network policy or host-executable restrictions.
fn current_for_prefix_rules(policy: &Policy, ignored: bool) -> Policy {
    if !ignored {
        return policy.clone();
    }
    let rules = policy.rules().iter_all().flat_map(|(program, rules)| {
        rules.iter().filter_map(move |rule| {
            let is_allow_prefix = rule.as_any().downcast_ref::<PrefixRule>()
                .is_some_and(|prefix| prefix.decision == Decision::Allow);
            (!is_allow_prefix).then(|| (program.clone(), Arc::clone(rule)))
        })
    }).collect();
    Policy::from_parts(rules, policy.network_rules().to_vec(), policy.host_executables().clone())
}

fn snapshot(policy: &Policy) -> Value {
    json!({
        "allowed_prefixes": policy.get_allowed_prefixes(),
        "allowed_prefixes_text": source_prefix_format::format_allow_prefixes(policy.get_allowed_prefixes()),
        "network_rules": policy.network_rules().iter().map(|rule| json!({
            "host": rule.host, "protocol": rule.protocol.as_policy_string(),
            "decision": rule.decision, "justification": rule.justification,
        })).collect::<Vec<_>>(),
        "host_executables": policy.host_executables(),
        "rule_count": policy.rules().iter_all().map(|(_, rules)| rules.len()).sum::<usize>(),
    })
}

pub fn execute(request: Request) -> Result<Value, Error> {
    match request {
        Request::Identity => Ok(json!({"ok": true, "identity": "minicode-execpolicy",
            "source_commit": "822e58cc3d666166c7446c5b1ea2e52f5d09594c", "starlark": "0.14.2",
            "protocol": 1})),
        Request::Inspect {policy, ignore_allow_prefix_rules} => {
            let policy = current_for_prefix_rules(&load_policy(policy)?, ignore_allow_prefix_rules);
            Ok(json!({"ok": true, "policy": snapshot(&policy)}))
        }
        Request::CommandRequirement {policy, environment_sources, ignore_allow_prefix_rules, command} => {
            let base = current_for_prefix_rules(&load_policy(policy)?, ignore_allow_prefix_rules);
            let policy = base.merge_overlay(&parse(&environment_sources)?);
            let result = source_requirement::command_requirement(&policy, command, ignore_allow_prefix_rules);
            Ok(json!({"ok": true, "requirement": result["requirement"], "evaluation": result["evaluation"],
                "commands": result["commands"], "command_origin": result["command_origin"],
                "approval_command": result["approval_command"], "prompt_policy": snapshot(&base),
                "execution_policy": snapshot(&policy)}))
        }
        Request::AppendAllowPrefix {path, prefix} => {
            codex_execpolicy::blocking_append_allow_prefix_rule(&path, &prefix)?;
            Ok(json!({"ok": true, "path": path, "prefix": prefix}))
        }
        Request::Evaluate {policy, environment_sources, commands, ignore_allow_prefix_rules, prefix_rule} => {
            if commands.is_empty() {
                return Err(Error::EmptyCommands);
            }
            let base = current_for_prefix_rules(&load_policy(policy)?, ignore_allow_prefix_rules);
            let environment = parse(&environment_sources)?;
            // Source merges environment overlays AFTER admitted-model filtering.
            let policy = base.merge_overlay(&environment);
            let options = MatchOptions {resolve_host_executables: true};
            let fallback = |argv: &[String]| commands.iter().find(|input| input.argv == argv)
                .expect("Policy checks the supplied command inputs").unmatched_decision;
            let evaluation: Evaluation = policy.check_multiple_with_options(
                commands.iter().map(|input| &input.argv), &fallback, &options);
            let bypass_sandbox = evaluation.decision == Decision::Allow && commands.iter().all(|input| {
                policy.matches_for_command_with_options(&input.argv, None, &options).iter()
                    .any(|matched| matches!(matched, RuleMatch::PrefixRuleMatch {decision: Decision::Allow, ..}))
            });
            let parsed = commands.iter().map(|input| input.argv.clone()).collect::<Vec<_>>();
            let amendment = if ignore_allow_prefix_rules { None } else {
                match evaluation.decision {
                    Decision::Forbidden => None,
                    Decision::Prompt => source_amendment::derive_requested_execpolicy_amendment_from_prefix_rule(
                        prefix_rule.as_ref(), &evaluation.matched_rules, &policy, &parsed, &fallback, &options)
                        .or_else(|| source_amendment::try_derive_execpolicy_amendment_for_prompt_rules(&evaluation.matched_rules)),
                    Decision::Allow => source_amendment::try_derive_execpolicy_amendment_for_allow_rules(&evaluation.matched_rules),
                }
            };
            Ok(json!({"ok": true, "evaluation": evaluation, "bypass_sandbox": bypass_sandbox,
                "proposed_amendment": amendment.map(|amendment| amendment.command),
                "prompt_policy": snapshot(&base), "execution_policy": snapshot(&policy)}))
        }
    }
}
