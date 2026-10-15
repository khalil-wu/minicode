use minicode_execpolicy::{Request, execute};
use serde_json::{Value, json};

fn request(command: &[&str], configured_shell: &str) -> Value {
    json!({"operation": "command_requirement", "sources": [], "environment_sources": [],
        "ignore_allow_prefix_rules": false, "command": command, "configured_shell": configured_shell,
        "shell_mode": "normal", "approval_policy": "on-request", "prefix_rule": null,
        "permission_profile": {"managed": false, "file_system": {"kind": "unrestricted", "full_disk_write": true}},
        "windows_sandbox_level": "disabled", "sandbox_permissions": "use_default"})
}

fn policy(value: &mut Value, contents: &str) {
    value["sources"] = json!([{"path": "user/rules/default.rules", "contents": contents}]);
}

fn run(value: Value) -> Value {
    execute(serde_json::from_value::<Request>(value).unwrap()).unwrap()
}

fn restricted(value: &mut Value) {
    value["permission_profile"] = json!({"managed": true,
        "file_system": {"kind": "restricted", "full_disk_write": false}});
    value["windows_sandbox_level"] = json!("elevated");
}

fn granular(rules: bool, sandbox: bool) -> Value {
    json!({"granular": {"rules": rules, "sandbox_approval": sandbox, "mcp_elicitations": false}})
}

#[test]
fn ordinary_unmatched_safe_command_really_skips_approval_without_bypassing_sandbox() {
    let result = run(request(&["bash", "-lc", "git status"], "bash"));
    assert_eq!(result["commands"], json!([["git", "status"]]));
    assert_eq!(result["requirement"]["kind"], "skip");
    assert_eq!(result["requirement"]["bypass_sandbox"], false);
    assert_eq!(result["requirement"]["proposed_execpolicy_amendment"]["command"], json!(["git", "status"]));
}

#[test]
fn restricted_real_backend_safe_command_skips_but_explicit_override_prompts() {
    let mut value = request(&["bash", "-lc", "git status"], "bash");
    restricted(&mut value);
    assert_eq!(run(value.clone())["requirement"]["kind"], "skip");
    value["sandbox_permissions"] = json!("with_additional_permissions");
    assert_eq!(run(value.clone())["requirement"]["kind"], "needs_approval");
    value["approval_policy"] = json!("never");
    assert_eq!(run(value)["requirement"]["kind"], "skip");
}

#[test]
fn actual_bash_segments_all_need_explicit_rules_for_bypass() {
    let mut value = request(&["bash", "-lc", "git status && git diff --stat"], "bash");
    policy(&mut value, "prefix_rule(pattern=[\"git\", \"status\"], decision=\"allow\")\nprefix_rule(pattern=[\"git\", \"diff\"], decision=\"allow\")");
    let result = run(value.clone());
    assert_eq!(result["commands"], json!([["git", "status"], ["git", "diff", "--stat"]]));
    assert_eq!(result["requirement"]["bypass_sandbox"], true);
    value["command"] = json!(["bash", "-lc", "cd /tmp && git status"]);
    assert_eq!(run(value)["requirement"]["bypass_sandbox"], false);
}

#[test]
fn source_parser_leaves_dynamic_bash_opaque_instead_of_guessing_argv() {
    let mut value = request(&["bash", "-lc", "$CMD status"], "bash");
    value["approval_policy"] = json!("untrusted");
    policy(&mut value, "prefix_rule(pattern=[\"git\", \"status\"], decision=\"allow\")");
    let result = run(value);
    assert_eq!(result["commands"], json!([["bash", "-lc", "$CMD status"]]));
    assert_eq!(result["requirement"]["kind"], "needs_approval");
}

#[test]
fn forced_rm_never_uses_source_forbidden_reason() {
    let mut value = request(&["bash", "-lc", "rm -rf scratch"], "bash");
    value["approval_policy"] = json!("never");
    let result = run(value);
    assert_eq!(result["requirement"]["kind"], "forbidden");
    assert!(result["requirement"]["reason"].as_str().unwrap().contains("rm -f style commands are not permitted"));
}

#[test]
fn granular_rule_and_sandbox_approvals_keep_their_source_precedence() {
    let mut value = request(&["bash", "-lc", "git push origin"], "bash");
    restricted(&mut value);
    value["sandbox_permissions"] = json!("require_escalated");
    policy(&mut value, "prefix_rule(pattern=[\"git\", \"push\"], decision=\"prompt\", justification=\"review deployment\")");
    value["approval_policy"] = granular(false, true);
    let denied = run(value.clone());
    assert_eq!(denied["requirement"]["kind"], "forbidden");
    assert!(denied["requirement"]["reason"].as_str().unwrap().contains("Granular.rules is false"));
    value["approval_policy"] = granular(true, false);
    let allowed_prompt = run(value.clone());
    assert_eq!(allowed_prompt["requirement"]["kind"], "needs_approval");
    assert!(allowed_prompt["requirement"]["reason"].as_str().unwrap().contains("review deployment"));
    assert_eq!(allowed_prompt["requirement"]["proposed_execpolicy_amendment"], Value::Null);
    value["sources"] = json!([]);
    let denied_sandbox = run(value);
    assert_eq!(denied_sandbox["requirement"]["kind"], "forbidden");
    assert!(denied_sandbox["requirement"]["reason"].as_str().unwrap().contains("Granular.sandbox_approval is false"));
}

#[test]
fn ignore_rules_model_cannot_reuse_or_propose_allow_prefixes() {
    let mut value = request(&["bash", "-lc", "git status"], "bash");
    value["approval_policy"] = json!("untrusted");
    policy(&mut value, "prefix_rule(pattern=[\"git\", \"status\"], decision=\"allow\")");
    assert_eq!(run(value.clone())["requirement"]["kind"], "skip");
    value["ignore_allow_prefix_rules"] = json!(true);
    let result = run(value);
    assert_eq!(result["requirement"]["kind"], "needs_approval");
    assert_eq!(result["requirement"]["proposed_execpolicy_amendment"], Value::Null);
    assert_eq!(result["prompt_policy"]["allowed_prefixes"], json!([]));
}

#[test]
fn environment_rules_survive_model_filtering_without_entering_base_prompt_prefixes() {
    let mut value = request(&["bash", "-lc", "git status"], "bash");
    value["approval_policy"] = json!("untrusted");
    value["ignore_allow_prefix_rules"] = json!(true);
    value["environment_sources"] = json!([{"path": "environment.rules", "contents": "prefix_rule(pattern=[\"git\", \"status\"], decision=\"allow\")"}]);
    let result = run(value);
    assert_eq!(result["requirement"]["kind"], "skip");
    assert_eq!(result["requirement"]["bypass_sandbox"], true);
    assert_eq!(result["prompt_policy"]["allowed_prefixes"], json!([]));
    assert_eq!(result["execution_policy"]["allowed_prefixes"], json!([["git", "status"]]));
}

#[test]
fn requested_prefix_is_checked_after_real_shell_decomposition() {
    let mut value = request(&["bash", "-lc", "git status --short && git diff"], "bash");
    value["approval_policy"] = json!("untrusted");
    value["prefix_rule"] = json!(["git", "status"]);
    let result = run(value);
    assert_eq!(result["requirement"]["proposed_execpolicy_amendment"]["command"], json!(["git", "status", "--short"]));
}

#[test]
fn native_permission_prefix_text_uses_the_source_token_order_and_json_escaping() {
    let mut value = request(&["git", "status"], "git");
    policy(&mut value, r#"
prefix_rule(pattern=["longer", "command"], decision="allow")
prefix_rule(pattern=["z"], decision="allow")
prefix_rule(pattern=["a\"b"], decision="allow")
prefix_rule(pattern=["a"], decision="allow")
"#);
    assert_eq!(run(value)["prompt_policy"]["allowed_prefixes_text"], "- [\"a\"]\n- [\"z\"]\n- [\"a\\\"b\"]\n- [\"longer\", \"command\"]");
}

#[test]
fn native_permission_prefix_text_keeps_source_count_and_character_limit_marker() {
    let mut value = request(&["git", "status"], "git");
    let rules = (0..105).map(|index| format!("prefix_rule(pattern=[\"cmd{index:03}\"], decision=\"allow\")\n")).collect::<String>();
    policy(&mut value, &rules);
    let result = run(value);
    let formatted = result["prompt_policy"]["allowed_prefixes_text"].as_str().unwrap();
    assert!(formatted.ends_with("...\n[Some commands were truncated]"));
    assert!(formatted.contains("cmd099"));
    assert!(!formatted.contains("cmd100"));
    let mut long_value = request(&["git", "status"], "git");
    let long_token = "文".repeat(5100);
    policy(&mut long_value, &format!("prefix_rule(pattern=[{}], decision=\"allow\")", serde_json::to_string(&long_token).unwrap()));
    let long_result = run(long_value);
    let long_text = long_result["prompt_policy"]["allowed_prefixes_text"].as_str().unwrap();
    let marker = "...\n[Some commands were truncated]";
    assert_eq!(long_text.strip_suffix(marker).unwrap().chars().count(), 5000);
}

#[cfg(windows)]
#[test]
fn windows_managed_filesystem_without_backend_prompts_even_safe_commands() {
    let mut value = request(&["bash", "-lc", "git status"], "bash");
    restricted(&mut value);
    value["windows_sandbox_level"] = json!("disabled");
    assert_eq!(run(value.clone())["requirement"]["kind"], "needs_approval");
    value["approval_policy"] = json!("never");
    assert_eq!(run(value.clone())["requirement"]["kind"], "forbidden");
    value["permission_profile"]["file_system"]["full_disk_write"] = json!(true);
    assert_eq!(run(value)["requirement"]["kind"], "skip");
}

#[cfg(windows)]
#[test]
fn unfamiliar_powershell_executable_cannot_borrow_inner_git_prefix_trust() {
    let mut value = request(&["C:/unknown/powershell.exe", "-Command", "git status"], "powershell.exe");
    value["approval_policy"] = json!("untrusted");
    policy(&mut value, "prefix_rule(pattern=[\"git\", \"status\"], decision=\"allow\")");
    let result = run(value);
    assert_eq!(result["commands"], json!([["C:/unknown/powershell.exe"], ["git", "status"]]));
    assert_eq!(result["approval_command"], json!(["C:/unknown/powershell.exe"]));
    assert_eq!(result["requirement"]["kind"], "needs_approval");
}

#[cfg(windows)]
#[test]
fn configured_powershell_lowers_real_literal_commands_and_keeps_dynamic_script_opaque() {
    let mut value = request(&["powershell.exe", "-NoProfile", "-Command", "git status; git diff"], "powershell.exe");
    value["approval_policy"] = json!("untrusted");
    policy(&mut value, "prefix_rule(pattern=[\"git\", [\"status\", \"diff\"]], decision=\"allow\")");
    let literal = run(value.clone());
    assert_eq!(literal["commands"], json!([["git", "status"], ["git", "diff"]]));
    assert_eq!(literal["command_origin"], "PowerShell");
    assert_eq!(literal["requirement"]["bypass_sandbox"], true);
    value["command"] = json!(["powershell.exe", "-Command", "& $program status"]);
    let dynamic = run(value);
    assert_eq!(dynamic["commands"], json!([["powershell.exe", "-Command", "& $program status"]]));
    assert_eq!(dynamic["requirement"]["kind"], "needs_approval");
}
