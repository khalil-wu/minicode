use minicode_execpolicy::{Request, execute};
use serde_json::{Value, json};
use std::fs;

fn source(contents: &str) -> Value {
    json!({"path": "fixture.rules", "contents": contents})
}

fn evaluate(contents: &str, argv: &[&str], unmatched: &str, ignored: bool, prefix: Value, overlay: &str) -> Value {
    execute(serde_json::from_value::<Request>(json!({"operation": "evaluate",
        "sources": [source(contents)], "environment_sources": [source(overlay)],
        "commands": [{"argv": argv, "unmatched_decision": unmatched}],
        "ignore_allow_prefix_rules": ignored, "prefix_rule": prefix})).unwrap()).unwrap()
}

#[test]
fn native_starlark_functions_loops_and_examples_are_evaluated() {
    let result = evaluate(r#"
def add_rule(program):
    prefix_rule(pattern=[program, "status"], match=[[program, "status"]], not_match=[[program, "push"]])
for program in ["git", "hg"]:
    add_rule(program)
"#, &["hg", "status", "--short"], "prompt", false, Value::Null, "");
    assert_eq!(result["evaluation"]["decision"], "allow");
    assert_eq!(result["prompt_policy"]["allowed_prefixes"], json!([["git", "status"], ["hg", "status"]]));
    assert_eq!(result["bypass_sandbox"], true);
}

#[test]
fn actual_pattern_alternatives_and_f_strings_work() {
    let result = evaluate(r#"
verb = "status"
prefix_rule(pattern=[["git", "hg"], [f"{verb}", "diff"]], decision="allow")
"#, &["git", "diff"], "prompt", false, Value::Null, "");
    assert_eq!(result["evaluation"]["decision"], "allow");
    assert_eq!(result["prompt_policy"]["rule_count"], 2);
}

#[test]
fn explicit_forbidden_rules_win_over_allow() {
    let result = evaluate(r#"
prefix_rule(pattern=["git"], decision="allow")
prefix_rule(pattern=["git", "push"], decision="forbidden", justification="local workflow policy")
"#, &["git", "push", "origin"], "allow", false, json!(["git", "push"]), "");
    assert_eq!(result["evaluation"]["decision"], "forbidden");
    assert_eq!(result["proposed_amendment"], Value::Null);
    assert_eq!(result["bypass_sandbox"], false);
}

#[test]
fn ignored_allow_prefixes_preserve_prompt_forbidden_network_and_host_executable_data() {
    let path = if cfg!(windows) { "C:/git/bin/git.exe" } else { "/opt/git/bin/git" };
    let policy = format!(r#"
prefix_rule(pattern=["git", "status"], decision="allow")
prefix_rule(pattern=["git", "push"], decision="prompt")
prefix_rule(pattern=["rm"], decision="forbidden")
network_rule(host="example.org", protocol="https", decision="deny")
host_executable(name="git", paths=[{}])
"#, serde_json::to_string(path).unwrap());
    let result = evaluate(&policy, &["git", "status"], "prompt", true, json!(["git", "status"]), "");
    assert_eq!(result["evaluation"]["decision"], "prompt");
    assert_eq!(result["prompt_policy"]["allowed_prefixes"], json!([]));
    assert_eq!(result["prompt_policy"]["rule_count"], 2);
    assert_eq!(result["prompt_policy"]["network_rules"][0]["decision"], "forbidden");
    assert_eq!(result["prompt_policy"]["host_executables"].as_object().unwrap().len(), 1);
    assert_eq!(result["proposed_amendment"], Value::Null);
    assert_eq!(evaluate(&policy, &["git", "push"], "allow", true, Value::Null, "")["evaluation"]["decision"], "prompt");
    assert_eq!(evaluate(&policy, &["rm", "example"], "allow", true, Value::Null, "")["evaluation"]["decision"], "forbidden");
}

#[test]
fn environment_overlay_is_applied_after_filtering_and_kept_out_of_base_prompt_prefixes() {
    let result = evaluate(r#"prefix_rule(pattern=["git", "diff"], decision="allow")"#,
        &["git", "status"], "prompt", true, Value::Null,
        r#"prefix_rule(pattern=["git", "status"], decision="allow")"#);
    assert_eq!(result["evaluation"]["decision"], "allow");
    assert_eq!(result["prompt_policy"]["allowed_prefixes"], json!([]));
    assert_eq!(result["execution_policy"]["allowed_prefixes"], json!([["git", "status"]]));
    assert_eq!(result["bypass_sandbox"], true);
    assert_eq!(result["proposed_amendment"], Value::Null);
}

#[test]
fn an_unmatched_allow_is_not_an_explicit_sandbox_bypass() {
    let result = evaluate("", &["git", "status"], "allow", false, Value::Null, "");
    assert_eq!(result["evaluation"]["decision"], "allow");
    assert_eq!(result["bypass_sandbox"], false);
    assert_eq!(result["proposed_amendment"], json!(["git", "status"]));
}

#[test]
fn requested_prefix_is_validated_against_real_matching_not_string_starts_with() {
    let result = evaluate("", &["git", "status", "--short"], "prompt", false, json!(["git", "status"]), "");
    assert_eq!(result["proposed_amendment"], json!(["git", "status"]));
    let wrong = evaluate("", &["git", "status", "--short"], "prompt", false, json!(["git", "sta"]), "");
    assert_eq!(wrong["proposed_amendment"], json!(["git", "status", "--short"]));
}

#[test]
fn banned_broad_suggestion_does_not_replace_the_source_automatic_command_proposal() {
    let result = evaluate("", &["git", "status"], "prompt", false, json!(["git"]), "");
    assert_eq!(result["proposed_amendment"], json!(["git", "status"]));
}

#[test]
fn explicit_prompt_rule_cannot_be_removed_by_a_new_amendment() {
    let result = evaluate(r#"prefix_rule(pattern=["git", "push"], decision="prompt")"#,
        &["git", "push"], "allow", false, json!(["git", "push"]), "");
    assert_eq!(result["evaluation"]["decision"], "prompt");
    assert_eq!(result["proposed_amendment"], Value::Null);
}

#[test]
fn multiple_commands_keep_all_rule_evidence_and_require_every_segment_for_bypass() {
    let result = execute(serde_json::from_value::<Request>(json!({"operation": "evaluate",
        "sources": [source(r#"prefix_rule(pattern=["git", "status"], decision="allow")"#)],
        "environment_sources": [], "ignore_allow_prefix_rules": false, "prefix_rule": ["git", "status"],
        "commands": [{"argv": ["git", "status"], "unmatched_decision": "allow"},
            {"argv": ["python", "script.py"], "unmatched_decision": "prompt"}]})).unwrap()).unwrap();
    assert_eq!(result["evaluation"]["decision"], "prompt");
    assert_eq!(result["evaluation"]["matchedRules"].as_array().unwrap().len(), 2);
    assert_eq!(result["bypass_sandbox"], false);
    assert_eq!(result["proposed_amendment"], json!(["python", "script.py"]));
}

#[test]
fn real_host_executable_resolution_requires_the_declared_absolute_program() {
    let approved = if cfg!(windows) { "C:/git/bin/git.exe" } else { "/opt/git/bin/git" };
    let other = if cfg!(windows) { "C:/other/bin/git.exe" } else { "/other/bin/git" };
    let policy = format!(r#"prefix_rule(pattern=["git", "status"], decision="allow")
host_executable(name="git", paths=[{}])"#, serde_json::to_string(approved).unwrap());
    assert_eq!(evaluate(&policy, &[approved, "status"], "prompt", false, Value::Null, "")["evaluation"]["decision"], "allow");
    assert_eq!(evaluate(&policy, &[other, "status"], "prompt", false, Value::Null, "")["evaluation"]["decision"], "prompt");
}

#[test]
fn source_example_failure_keeps_file_and_one_based_location() {
    let error = execute(serde_json::from_value::<Request>(json!({"operation": "inspect",
        "sources": [{"path": "actual-user.rules", "contents": "# header\nprefix_rule(pattern=[\"git\"], match=[[\"hg\"]])\n"}],
        "ignore_allow_prefix_rules": false})).unwrap()).unwrap_err().json();
    assert_eq!(error["ok"], false);
    assert_eq!(error["error"]["location"]["path"], "actual-user.rules");
    assert_eq!(error["error"]["location"]["start"]["line"], 2);
}

#[test]
fn native_append_preserves_existing_rules_and_cold_parser_reads_escaped_tokens() {
    let temp = tempfile::tempdir().unwrap();
    let path = temp.path().join("rules/default.rules");
    fs::create_dir(path.parent().unwrap()).unwrap();
    fs::write(&path, "# preserved original rule file\n").unwrap();
    let prefix = vec!["example".to_string(), "quoted\"value".to_string(), "路径\\text".to_string()];
    for _ in 0..2 {
        execute(Request::AppendAllowPrefix {path: path.clone(), prefix: prefix.clone()}).unwrap();
    }
    let contents = fs::read_to_string(&path).unwrap();
    assert!(contents.starts_with("# preserved original rule file\n"));
    assert_eq!(contents.lines().count(), 2);
    let result = execute(serde_json::from_value::<Request>(json!({"operation": "evaluate",
        "sources": [{"path": path, "contents": contents}], "environment_sources": [],
        "commands": [{"argv": prefix, "unmatched_decision": "prompt"}],
        "ignore_allow_prefix_rules": false, "prefix_rule": null})).unwrap()).unwrap();
    assert_eq!(result["evaluation"]["decision"], "allow");
}

#[test]
fn independent_native_writers_use_source_file_locking() {
    let temp = tempfile::tempdir().unwrap();
    let path = temp.path().join("rules/default.rules");
    let writers = (0..4).map(|index| {
        let path = path.clone();
        std::thread::spawn(move || {
            execute(Request::AppendAllowPrefix {path, prefix: vec!["example".to_string(), index.to_string()]}).unwrap();
        })
    }).collect::<Vec<_>>();
    for writer in writers { writer.join().unwrap(); }
    let contents = fs::read_to_string(&path).unwrap();
    let result = execute(serde_json::from_value::<Request>(json!({"operation": "inspect",
        "sources": [{"path": path, "contents": contents}], "ignore_allow_prefix_rules": false})).unwrap()).unwrap();
    assert_eq!(result["policy"]["allowed_prefixes"].as_array().unwrap().len(), 4);
}
