import { useState } from "react";
import { ChevronDown, Plus, RotateCcw, Trash2 } from "lucide-react";
import { useAppStore } from "../stores";
import { NumberInput } from "../components/NumberInput";
import { SelectMenu } from "../components/SelectMenu";
import { defaultWorkbenchPreferences, type CodeSnippet } from "../lib/workbench-preferences";

export function EditorPreferencesSettings() {
  const preferences = useAppStore((state) => state.workbenchPreferences);
  const update = useAppStore((state) => state.setWorkbenchPreferences);
  const textScale = useAppStore((state) => state.textScale);
  const codeTextScale = useAppStore((state) => state.codeTextScale);
  const models = useAppStore((state) => state.availableModels);
  const usage = useAppStore((state) => state.inlineCompletionUsage);
  const [snippet, setSnippet] = useState<CodeSnippet>({ id: "", language: "typescript", prefix: "", body: "", description: "" });
  return <>
    <section className="settings-group">
      <h3 className="settings-group-title">字号</h3>
      <p className="settings-section-description">Ctrl + ＋ / － 缩放界面，Ctrl + 0 恢复实际大小。</p>
      <div className="settings-card">
        {[
          { label: "界面字号", value: Math.round(textScale * 14), min: 11, max: 21, set: (value: number) => useAppStore.getState().setTextScale(value / 14) },
          { label: "正文字号", value: preferences.proseSize, min: 12, max: 24, set: (value: number) => update({ proseSize: value }) },
          { label: "编辑器字号", value: Math.round(codeTextScale * 14), min: 11, max: 24, set: (value: number) => useAppStore.getState().setCodeTextScale(value / 14) },
        ].map((item) => <label className="settings-row" key={item.label}><span className="settings-row-title">{item.label}</span><span className="settings-row-control">
          <NumberInput aria-label={item.label} min={item.min} max={item.max} value={item.value} onCommit={item.set} /> px</span></label>)}
        <div className="settings-font-preview"><p>从一个想法开始，把细节做好。 Make something thoughtful.</p><code>const greeting = "Hello, MiniCode";</code></div>
      </div>
      <div className="settings-preferences-actions"><button type="button" className="settings-action-button" onClick={() => {
        update({ proseSize: defaultWorkbenchPreferences.proseSize });
        useAppStore.getState().setTextScale(1);
        useAppStore.getState().setCodeTextScale(1);
      }}><RotateCcw size={14} />恢复默认字号</button></div>
    </section>
    <details className="settings-appearance-advanced">
      <summary className="settings-appearance-advanced-trigger">高级<ChevronDown size={15} aria-hidden="true" /></summary>
      <div className="settings-appearance-advanced-body">
    <section className="settings-group">
      <h3 className="settings-group-title">AI 代码预测</h3>
      <div className="settings-card">
        <div className="settings-row"><div className="settings-row-copy"><div className="settings-row-title">行内预测</div><p className="settings-row-description">暂停输入后，使用所选模型补全光标附近代码。Tab 接受，Esc 取消。</p></div>
          <button className="settings-toggle" type="button" role="switch" aria-label="AI 行内预测" aria-checked={preferences.aiEnabled} data-active={preferences.aiEnabled} onClick={() => update({ aiEnabled: !preferences.aiEnabled })}><span /></button></div>
        <label className="settings-row"><span>预测模型</span><SelectMenu ariaLabel="预测模型" className="settings-select" align="end" value={preferences.aiModel} onValueChange={(value) => update({ aiModel: value, aiProvider: value ? useAppStore.getState().currentProviderId || useAppStore.getState().currentProvider : "" })}>
          <option value="">跟随对话模型</option>{[...new Set([...models, preferences.aiModel].filter(Boolean))].map((model) => <option key={model} value={model}>{model}</option>)}</SelectMenu></label>
        <label className="settings-row"><span>每次最大输出</span><SelectMenu ariaLabel="预测输出上限" className="settings-select" align="end" value={String(preferences.aiMaxTokens)} onValueChange={(value) => update({ aiMaxTokens: Number(value) })}>{[64, 128, 256, 512].map((size) => <option key={size} value={size}>{size} tokens</option>)}</SelectMenu></label>
        <div className="settings-row-description">本次应用运行：{usage.requests} 次请求 · 输入 {usage.inputTokens} / 输出 {usage.outputTokens} tokens。代码按需发送到所选模型服务。</div>
        {usage.lastError && <p role="alert" className="settings-inline-error">{usage.lastError}</p>}
      </div>
    </section>
    <section className="settings-group">
      <h3 className="settings-group-title">编辑器</h3>
      <div className="settings-card">
        <label className="settings-row"><span className="settings-row-title">缩进宽度</span><SelectMenu ariaLabel="缩进宽度" className="settings-select" align="end" value={String(preferences.tabSize)} onValueChange={(value) => update({ tabSize: Number(value) })}>{[2, 4, 8].map((size) => <option key={size} value={size}>{size} 个字符</option>)}</SelectMenu></label>
        {([["insertSpaces", "使用空格缩进"], ["formatOnSave", "保存时格式化"], ["wordWrap", "自动换行"], ["minimap", "代码缩略图"],
          ["lineNumbers", "显示行号"], ["stickyScroll", "固定作用域标题"], ["previewTabs", "单击文件时使用预览标签"]] as const).map(([key, label]) =>
          <div className="settings-row" key={key}><span className="settings-row-title">{label}</span><button type="button" className="settings-toggle" role="switch" aria-label={label}
            aria-checked={preferences[key]} data-active={preferences[key]} onClick={() => update({ [key]: !preferences[key] })}><span /></button></div>)}
      </div>
      <details className="settings-detail-block"><summary>自定义代码模板 · {preferences.snippets.length}</summary>
        <p className="settings-section-description">输入前缀后选择模板，Tab 跳到下一处占位。支持 $1、$2 和 $0。</p>
        {preferences.snippets.map((item) => <div className="settings-row" key={item.id}><span>{item.prefix}<small> · {item.language} · {item.description}</small></span><button type="button" className="settings-icon-button" aria-label={"删除模板 " + item.prefix} onClick={() => update({ snippets: preferences.snippets.filter((entry) => entry.id !== item.id) })}><Trash2 size={14} /></button></div>)}
        <form className="settings-snippet-form" onSubmit={(event) => { event.preventDefault(); update({ snippets: [...preferences.snippets, { ...snippet, id: crypto.randomUUID() }] }); setSnippet({ ...snippet, prefix: "", body: "", description: "" }); }}>
          <SelectMenu ariaLabel="模板语言" value={snippet.language} onValueChange={(value) => setSnippet({ ...snippet, language: value })}>{["typescript", "javascript", "python", "yaml", "html", "css", "json", "cpp", "c"].map((language) => <option key={language} value={language}>{language}</option>)}</SelectMenu>
          <input aria-label="模板前缀" required placeholder="触发前缀" value={snippet.prefix} onChange={(event) => setSnippet({ ...snippet, prefix: event.target.value })} />
          <input aria-label="模板说明" placeholder="说明" value={snippet.description} onChange={(event) => setSnippet({ ...snippet, description: event.target.value })} />
          <textarea aria-label="模板正文" required rows={4} placeholder="输入代码和 $1 占位" value={snippet.body} onChange={(event) => setSnippet({ ...snippet, body: event.target.value })} />
          <button type="submit" className="settings-action-button"><Plus size={14} />添加模板</button>
        </form>
      </details>
      <div className="settings-preferences-actions"><button type="button" className="settings-action-button" onClick={() => {
        const keys = ["wordWrap", "minimap", "tabSize", "insertSpaces", "formatOnSave", "lineNumbers", "stickyScroll", "previewTabs"] as const;
        update(Object.fromEntries(keys.map((key) => [key, defaultWorkbenchPreferences[key]])));
      }}><RotateCcw size={14} />恢复编辑器默认设置</button></div>
    </section>
      </div>
    </details>
  </>;
}
