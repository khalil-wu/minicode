use minicode_execpolicy::{Request, execute};
use std::io::{self, Write};
use std::process::ExitCode;

fn main() -> ExitCode {
    let request = match serde_json::from_reader::<_, Request>(io::stdin().lock()) {
        Ok(request) => request,
        Err(error) => {
            eprintln!("{error}");
            return ExitCode::from(2);
        }
    };
    let (output, status) = match execute(request) {
        Ok(output) => (output, ExitCode::SUCCESS),
        Err(error) => (error.json(), ExitCode::FAILURE),
    };
    let mut stdout = io::stdout().lock();
    serde_json::to_writer(&mut stdout, &output).expect("write native policy result");
    writeln!(stdout).expect("finish native policy result");
    status
}
