// Unmodified formatting algorithms extracted from pinned Codex protocol/src/models.rs (Apache-2.0).
const MAX_RENDERED_PREFIXES: usize = 100;
const MAX_ALLOW_PREFIX_TEXT_BYTES: usize = 5000;
const TRUNCATED_MARKER: &str = "...\n[Some commands were truncated]";

pub fn format_allow_prefixes(prefixes: Vec<Vec<String>>) -> Option<String> {
    let mut truncated = false;
    if prefixes.len() > MAX_RENDERED_PREFIXES {
        truncated = true;
    }

    let mut prefixes = prefixes;
    prefixes.sort_by(|a, b| {
        a.len()
            .cmp(&b.len())
            .then_with(|| prefix_combined_str_len(a).cmp(&prefix_combined_str_len(b)))
            .then_with(|| a.cmp(b))
    });

    let full_text = prefixes
        .into_iter()
        .take(MAX_RENDERED_PREFIXES)
        .map(|prefix| format!("- {}", render_command_prefix(&prefix)))
        .collect::<Vec<_>>()
        .join("\n");

    // truncate to last UTF8 char
    let mut output = full_text;
    let byte_idx = output
        .char_indices()
        .nth(MAX_ALLOW_PREFIX_TEXT_BYTES)
        .map(|(i, _)| i);
    if let Some(byte_idx) = byte_idx {
        truncated = true;
        output = output[..byte_idx].to_string();
    }

    if truncated {
        Some(format!("{output}{TRUNCATED_MARKER}"))
    } else {
        Some(output)
    }
}

fn prefix_combined_str_len(prefix: &[String]) -> usize {
    prefix.iter().map(String::len).sum()
}

fn render_command_prefix(prefix: &[String]) -> String {
    let tokens = prefix
        .iter()
        .map(|token| serde_json::to_string(token).unwrap_or_else(|_| format!("{token:?}")))
        .collect::<Vec<_>>()
        .join(", ");
    format!("[{tokens}]")
}
