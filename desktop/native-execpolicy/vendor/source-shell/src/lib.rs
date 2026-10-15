//! The original pinned Source modules, with unused protocol/UI modules omitted.
//! Source files and algorithms are byte-for-byte copies; see source-manifest.
pub mod shell_detect;
pub mod bash;
pub mod powershell;
mod command_safety;
pub use command_safety::is_dangerous_command;
// Preserve the original subprocess test oracle's protocol module path while
// importing only its actual environment scrubber, never the protocol crate.
#[cfg(test)]
extern crate self as codex_protocol;
#[cfg(test)]
#[path = "source_test_environment.rs"]
pub mod shell_environment;
