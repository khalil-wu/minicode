use minicode_execpolicy::{Request, execute};
use serde_json::{Value, json};

fn request() -> Value {
    json!({"operation": "evaluate", "sources": [], "environment_sources": [],
        "ignore_allow_prefix_rules": false, "commands": [{"argv": ["git", "push", "origin"], "unmatched_decision": "prompt"}],
        "prefix_rule": ["git", "push"]})
}

fn run(value: Value) -> Value {
    execute(serde_json::from_value::<Request>(value).unwrap()).unwrap()
}

fn managed_rule(decision: &str) -> Value {
    json!({"prefix_rules": [{"pattern": [{"token": "git"}, {"any_of": ["push", "pull"]}],
        "decision": decision, "justification": "managed workflow"}]})
}

#[test]
fn original_requirements_producer_keeps_managed_prompt_over_user_allow_and_model_filtering() {
    let mut value = request();
    value["sources"] = json!([{"path": "user.rules", "contents": "prefix_rule(pattern=[\"git\", \"push\"], decision=\"allow\")"}]);
    value["requirements_rules"] = managed_rule("prompt");
    assert_eq!(run(value.clone())["evaluation"]["decision"], "prompt");
    value["ignore_allow_prefix_rules"] = json!(true);
    let result = run(value);
    assert_eq!(result["evaluation"]["decision"], "prompt");
    assert_eq!(result["prompt_policy"]["allowed_prefixes"], json!([]));
    assert_eq!(result["proposed_amendment"], Value::Null);
}

#[test]
fn original_requirements_forbidden_remains_stricter_than_environment_allow() {
    let mut value = request();
    value["requirements_rules"] = managed_rule("forbidden");
    value["environment_sources"] = json!([{"path": "environment.rules", "contents": "prefix_rule(pattern=[\"git\", \"push\"], decision=\"allow\")"}]);
    assert_eq!(run(value)["evaluation"]["decision"], "forbidden");
}

#[test]
fn original_requirements_parser_rejects_allow_and_invalid_alternative_tokens() {
    let mut value = request();
    value["requirements_rules"] = managed_rule("allow");
    let error = execute(serde_json::from_value::<Request>(value.clone()).unwrap()).unwrap_err().json();
    assert!(error["error"]["message"].as_str().unwrap().contains("not permitted in requirements.toml"));
    value["requirements_rules"] = managed_rule("prompt");
    value["requirements_rules"]["prefix_rules"][0]["pattern"][0] = json!({"token": "git", "any_of": ["git"]});
    let error = execute(serde_json::from_value::<Request>(value).unwrap()).unwrap_err().json();
    assert!(error["error"]["message"].as_str().unwrap().contains("set either token or any_of, not both"));
}

#[test]
fn host_approved_prefix_updates_the_same_loaded_policy_for_matching_and_model_instructions() {
    let mut value = request();
    value["approved_prefixes"] = json!([["git", "push"]]);
    let result = run(value.clone());
    assert_eq!(result["evaluation"]["decision"], "allow");
    assert_eq!(result["bypass_sandbox"], true);
    assert_eq!(result["prompt_policy"]["allowed_prefixes_text"], "- [\"git\", \"push\"]");
    value["ignore_allow_prefix_rules"] = json!(true);
    let ignored = run(value);
    assert_eq!(ignored["evaluation"]["decision"], "prompt");
    assert_eq!(ignored["prompt_policy"]["allowed_prefixes"], json!([]));
}

#[test]
fn source_in_memory_update_avoids_narrow_duplicate_when_loaded_rule_already_allows_it() {
    let mut value = request();
    value["sources"] = json!([{"path": "user.rules", "contents": "prefix_rule(pattern=[\"git\"], decision=\"allow\")"}]);
    value["approved_prefixes"] = json!([["git", "push"], ["git", "push"]]);
    let result = run(value);
    assert_eq!(result["prompt_policy"]["allowed_prefixes"], json!([["git"]]));
    assert_eq!(result["prompt_policy"]["rule_count"], 1);
}

#[test]
fn active_prefixes_cannot_erase_managed_forbidden_rules() {
    let mut value = request();
    value["requirements_rules"] = managed_rule("forbidden");
    value["approved_prefixes"] = json!([["git", "push"]]);
    let result = run(value);
    assert_eq!(result["evaluation"]["decision"], "forbidden");
    assert_eq!(result["bypass_sandbox"], false);
}
