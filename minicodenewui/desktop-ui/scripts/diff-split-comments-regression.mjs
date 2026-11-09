// Run: node frontend/scripts/diff-split-comments-regression.mjs <before-source> <receipt-json>
// Exercises actual private renderer statements without adding production test exports.
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { readFileSync, writeFileSync } from "node:fs";
import { createRequire } from "node:module";
import { resolve } from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

const workspace = fileURLToPath(new URL("../../", import.meta.url));
const dep = createRequire(resolve(workspace, "frontend/package.json"));
const ts = dep("typescript");
const sourcePath = resolve(workspace, "frontend/src.v2/panels/DiffPanel.tsx");
const source = readFileSync(sourcePath, "utf8");
const before = readFileSync(resolve(process.argv[2]), "utf8");
const options = { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX };
const sha = (value) => createHash("sha256").update(value).digest("hex");
const declaration = (text, names) => {
  const ast = ts.createSourceFile(sourcePath, text, ts.ScriptTarget.Latest, true);
  return ast.statements.filter((statement) => ts.isVariableStatement(statement)
    && statement.declarationList.declarations.some((item) => names.includes(item.name.getText(ast))))
    .map((statement) => statement.getText(ast)).join("\n");
};

const { JSDOM } = dep("jsdom");
const dom = new JSDOM('<!doctype html><html><body><div id="root"></div></body></html>', { url: "http://localhost/" });
globalThis.window = dom.window;
globalThis.document = dom.window.document;
Object.defineProperty(globalThis, "navigator", { value: dom.window.navigator, configurable: true });
globalThis.HTMLElement = dom.window.HTMLElement;
globalThis.IS_REACT_ACT_ENVIRONMENT = true;
const React = dep("react");
const { createRoot } = dep("react-dom/client");
const act = React.act || dep("react-dom/test-utils").act;
const rendererSource = declaration(source, ["INLINE_COLORIZE_LINE_LIMIT", "bgForKind", "colorForKind", "InlineCommentInput", "UnifiedDiffBody", "SplitDiffBody", "SplitRowPair"]);
const rendererJs = ts.transpileModule(rendererSource, { fileName: "current-renderer.tsx", compilerOptions: options }).outputText;
const Button = ({ children, onClick }) => React.createElement("button", { onClick }, children);
const components = new Function("require", "exports", "useMemo", "useState", "useAppStore", "useColorizedLines", "guessLanguageFromPath", "extractFilePathFromDiff", "workspaceFilePathsEqual", "MessageCircle", "Button", "DiffTruncationNotice",
  rendererJs + "\nreturn {SplitDiffBody, UnifiedDiffBody};")(
  dep, {}, React.useMemo, React.useState,
  (selector) => selector({ workingDirectory: "C:/audit-workspace" }),
  () => null, () => "plaintext", () => "file.ts", (a, b) => a === b,
  () => null, Button, () => null,
);

const filePath = "src/reviewed.ts";
const paired = [{ kind: "del", text: "old" }, { kind: "add", text: "new" }];
const rootElement = document.getElementById("root");
const root = createRoot(rootElement);
let renderId = 0;
let submissions = [];
const receipt = {
  method: "Actual TypeScript renderer statements rendered in React/jsdom; unrelated colorizer, store context, icon and Button shell stubbed",
  before_sha256: sha(before),
  after_sha256: sha(source),
  observations: {},
  limits: ["Not full-App/store or approval transport execution", "No browser layout/OS IME proof", "No semantic typecheck"],
};

function Harness({ mode, lines, initialComments }) {
  const [activeLine, setActiveLine] = React.useState(null);
  const [comments, setComments] = React.useState(initialComments);
  const Component = mode === "unified" ? components.UnifiedDiffBody : components.SplitDiffBody;
  return React.createElement(Component, {
    lines, comments, filePath,
    activeCommentLine: activeLine,
    onLineClick: (index) => setActiveLine((current) => current === index ? null : index),
    onCommentSubmit: (lineIndex, content) => {
      const comment = { filePath, lineIndex, content };
      submissions.push(comment);
      setComments((current) => [...current, comment]);
      setActiveLine(null);
    },
    onCommentCancel: () => setActiveLine(null),
  });
}

async function render({ mode = "split", lines = paired, comments = [] } = {}) {
  submissions = [];
  await act(async () => root.render(React.createElement(Harness, { key: ++renderId, mode, lines, initialComments: comments })));
}
const lineButton = (index, side = 0) => rootElement.querySelectorAll(`[role="button"][aria-label="评论 Diff 第 ${index + 1} 行"]`)[side];
const input = () => rootElement.querySelector("input");
const occurrences = (text) => rootElement.textContent.split(text).length - 1;
async function click(element) {
  await act(async () => element.dispatchEvent(new window.MouseEvent("click", { bubbles: true })));
}
async function key(element, keyName, extra = {}) {
  const event = new window.KeyboardEvent("keydown", { key: keyName, bubbles: true, cancelable: true, ...extra });
  await act(async () => element.dispatchEvent(event));
  return event;
}
async function type(text) {
  await act(async () => {
    Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, "value").set.call(input(), text);
    input().dispatchEvent(new window.Event("input", { bubbles: true }));
  });
}
function record(name, value) {
  receipt.observations[name] = value;
}

await test("current split diff comment DOM regressions", { concurrency: false }, async (t) => {
  t.after(async () => { await act(async () => root.unmount()); dom.window.close(); });

  await t.test("syntax and unchanged owner, IME, keyboard, unified and container CSS", () => {
    const diagnostics = ts.transpileModule(source, { fileName: sourcePath, compilerOptions: options, reportDiagnostics: true }).diagnostics;
    const unchanged = ["ActiveReviewTab", "InlineCommentInput", "SplitRowPair", "UnifiedDiffBody"];
    for (const name of unchanged) assert.equal(declaration(source, [name]), declaration(before, [name]), name);
    const cssSha = sha(readFileSync(resolve(workspace, "frontend/src.v2/panels/DiffPanel.css")));
    record("preserved_source", { unchanged, syntaxDiagnosticCount: diagnostics.length, cssSha256: cssSha });
    assert.equal(diagnostics.length, 0);
    assert.equal(cssSha, "ae0ac079b71a3e7aa0fca3a2a8906a6b284bff4f2b2b403985c8acc8193392e9");
  });

  await t.test("paired deleted-line mouse click opens its own input", async () => {
    await render(); await click(lineButton(0));
    record("paired_left_click", { inputCount: rootElement.querySelectorAll("input").length, label: input()?.getAttribute("aria-label") });
    assert.equal(input().getAttribute("aria-label"), "评论 Diff 第 1 行");
    assert.equal(document.activeElement, input());
  });

  await t.test("paired deleted-line Enter opens its own input", async () => {
    await render(); const event = await key(lineButton(0), "Enter");
    record("paired_left_enter", { inputCount: rootElement.querySelectorAll("input").length, label: input()?.getAttribute("aria-label"), defaultPrevented: event.defaultPrevented });
    assert.equal(input().getAttribute("aria-label"), "评论 Diff 第 1 行");
    assert.equal(event.defaultPrevented, true);
  });

  await t.test("paired deleted-line Space opens input without scrolling", async () => {
    await render(); const event = await key(lineButton(0), " ");
    record("paired_left_space", { inputCount: rootElement.querySelectorAll("input").length, defaultPrevented: event.defaultPrevented });
    assert.equal(input().getAttribute("aria-label"), "评论 Diff 第 1 行");
    assert.equal(event.defaultPrevented, true);
  });

  await t.test("paired added-line keyboard behavior is symmetric", async () => {
    await render(); await key(lineButton(1), "Enter");
    record("paired_right_enter", { inputCount: rootElement.querySelectorAll("input").length, label: input()?.getAttribute("aria-label") });
    assert.equal(input().getAttribute("aria-label"), "评论 Diff 第 2 行");
  });

  await t.test("left IME Enter and keyCode229 do not submit; regular Enter submits the left index", async () => {
    await render(); await key(lineButton(0), "Enter"); await type("中文左侧意见");
    await key(input(), "Enter", { isComposing: true });
    const afterComposition = submissions.length;
    await key(input(), "Enter", { keyCode: 229 });
    const after229 = submissions.length;
    await key(input(), "Enter");
    record("left_ime_and_submit", { afterComposition, after229, submissions: [...submissions], visibleComments: occurrences("中文左侧意见"), inputCount: rootElement.querySelectorAll("input").length });
    assert.equal(afterComposition, 0); assert.equal(after229, 0);
    assert.deepEqual(submissions, [{ filePath, lineIndex: 0, content: "中文左侧意见" }]);
    assert.equal(occurrences("中文左侧意见"), 1); assert.equal(input(), null);
  });

  await t.test("right submit keeps the right index and preserves the stored left comment", async () => {
    await render({ comments: [{ filePath, lineIndex: 0, content: "stored-left" }] });
    await key(lineButton(1), " "); await type("submitted-right"); await key(input(), "Enter");
    record("right_submit", { submissions: [...submissions], leftVisible: occurrences("stored-left"), rightVisible: occurrences("submitted-right") });
    assert.deepEqual(submissions, [{ filePath, lineIndex: 1, content: "submitted-right" }]);
    assert.equal(occurrences("stored-left"), 1); assert.equal(occurrences("submitted-right"), 1);
  });

  await t.test("switching sides does not transplant a left draft into the right input", async () => {
    await render(); await click(lineButton(0)); await type("left-only-draft"); await click(lineButton(1));
    record("side_switch", { label: input().getAttribute("aria-label"), value: input().value });
    assert.equal(input().getAttribute("aria-label"), "评论 Diff 第 2 行"); assert.equal(input().value, "");
  });

  await t.test("both stored comments render once and another file remains excluded", async () => {
    await render({ comments: [{ filePath, lineIndex: 0, content: "owned-left" }, { filePath, lineIndex: 1, content: "owned-right" }, { filePath: "src/other.ts", lineIndex: 0, content: "foreign-owner" }] });
    record("stored_and_file_scope", { left: occurrences("owned-left"), right: occurrences("owned-right"), foreign: occurrences("foreign-owner") });
    assert.equal(occurrences("owned-left"), 1); assert.equal(occurrences("owned-right"), 1); assert.equal(occurrences("foreign-owner"), 0);
  });

  await t.test("a shared context index renders one comment and opens one input", async () => {
    await render({ lines: [{ kind: "context", text: "unchanged" }], comments: [{ filePath, lineIndex: 0, content: "context-comment" }] });
    await click(lineButton(0, 1));
    record("shared_context", { commentCount: occurrences("context-comment"), inputCount: rootElement.querySelectorAll("input").length });
    assert.equal(occurrences("context-comment"), 1); assert.equal(rootElement.querySelectorAll("input").length, 1);
  });

  await t.test("unpaired deletion and addition keep their indices", async () => {
    const actual = [];
    for (const kind of ["del", "add"]) {
      await render({ lines: [{ kind, text: kind }] }); await click(lineButton(0));
      actual.push({ kind, label: input().getAttribute("aria-label") });
      assert.equal(input().getAttribute("aria-label"), "评论 Diff 第 1 行");
    }
    record("unpaired", actual);
  });

  await t.test("an uneven replacement block locates the second deletion row", async () => {
    await render({ lines: [{ kind: "del", text: "old-a" }, { kind: "del", text: "old-b" }, { kind: "add", text: "new-a" }] });
    await click(lineButton(1)); await type("second-deletion"); await click([...rootElement.querySelectorAll("button")].find((button) => button.textContent === "添加"));
    record("uneven_replacement", { submissions: [...submissions], visibleComments: occurrences("second-deletion") });
    assert.deepEqual(submissions, [{ filePath, lineIndex: 1, content: "second-deletion" }]); assert.equal(occurrences("second-deletion"), 1);
  });

  await t.test("Escape cancels a left comment without submitting", async () => {
    await render(); await key(lineButton(0), "Enter"); await type("cancelled-left"); await key(input(), "Escape");
    record("left_escape", { inputCount: rootElement.querySelectorAll("input").length, submissions: submissions.length });
    assert.equal(input(), null); assert.equal(submissions.length, 0);
  });

  await t.test("unified deleted-line authoring remains functional", async () => {
    await render({ mode: "unified" }); await key(lineButton(0), "Enter"); await type("unified-comment"); await key(input(), "Enter");
    record("unified_left", { submissions: [...submissions], visibleComments: occurrences("unified-comment") });
    assert.deepEqual(submissions, [{ filePath, lineIndex: 0, content: "unified-comment" }]); assert.equal(occurrences("unified-comment"), 1);
  });
});

writeFileSync(resolve(process.argv[3]), JSON.stringify(receipt, null, 2) + "\n");
