import type { ToolCallRecord } from "../../../lib/tool-call-reducer";
import { safeJsonParse } from "../../../lib/safe-parse";
import "./tool-text-renderer.css";

export const isUserQuestionRecord = (record: ToolCallRecord): boolean =>
  record.name === "ask_user" || record.name === "request_user_input";

export function usesCodeTypography(record: ToolCallRecord): boolean {
  if (record.activityKind === "commandExecution" || record.activityKind === "fileRead") return true;
  if (["command", "code", "file"].includes(record.resultKind || "")) return true;
  if (["run_command", "exec_command", "shell_command", "bash", "read_file", "tool_exec", "tool_wait"].includes(record.name)) return true;
  return ["evaluate", "get_dom", "get_html", "get_console_logs", "get_network_logs"].includes(String(record.args.action || ""));
}

const isStackTrace = (text: string): boolean =>
  /(?:^|\n)\s*(?:Traceback \(most recent call last\):|File ".+", line \d+|at .+\(.+:\d+(?::\d+)?\))/.test(text);

export function ToolResultText({ record, text, className = "", label, error = false }: {
  record: ToolCallRecord;
  text: string;
  className?: string;
  label?: string;
  error?: boolean;
}) {
  const monospace = usesCodeTypography(record) || (error && isStackTrace(text));
  const Tag = monospace ? "pre" : "div";
  return <Tag className={`${className} ${monospace ? "tool-result-code" : "tool-result-text"}`} aria-label={label}>{text}</Tag>;
}

type InputQuestion = { id: string; question: string };
type InputAnswers = { answers?: Record<string, { answers: string[] }> };

export function UserQuestionResult({ record, text }: { record: ToolCallRecord; text: string }) {
  const questions: InputQuestion[] = record.name === "ask_user"
    ? [{ id: "question", question: String(record.args.question || "") }]
    : (Array.isArray(record.args.questions) ? record.args.questions as InputQuestion[] : []);
  const structured = record.name === "request_user_input" ? safeJsonParse<InputAnswers>(text, {}) : {};
  const answer = text.startsWith("User answer:") ? text.slice("User answer:".length).trim() : "";
  const dismissed = text.startsWith("The user dismissed the question without answering.");
  const unanswered = dismissed || text.startsWith("The user did not answer the question.");
  return (
    <div className="tool-question-result">
      {questions.map((question) => {
        const response = structured.answers?.[question.id]?.answers.join("、") || answer;
        return (
          <div key={question.id} className="tool-question-result-item">
            {question.question && <div className="tool-question-result-question">{question.question}</div>}
            {response ? <div className="tool-question-result-answer"><span>你的回答</span><p>{response}</p></div>
              : unanswered ? <div className="tool-question-result-status">{dismissed ? "已关闭，未作答" : "未作答"}</div>
                : record.status === "pending" || record.status === "running" ? <div className="tool-question-result-status">等待你回复</div>
                  : <div className="tool-result-text">{text}</div>}
          </div>
        );
      })}
    </div>
  );
}
