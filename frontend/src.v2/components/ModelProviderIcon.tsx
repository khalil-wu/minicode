import { Icon } from "@iconify/react";
import xiaomiIcon from "@iconify-icons/simple-icons/xiaomi";
import Anthropic from "@lobehub/icons/es/Anthropic";
import ChatGLM from "@lobehub/icons/es/ChatGLM";
import Claude from "@lobehub/icons/es/Claude";
import DeepSeek from "@lobehub/icons/es/DeepSeek";
import Gemini from "@lobehub/icons/es/Gemini";
import Grok from "@lobehub/icons/es/Grok";
import Ollama from "@lobehub/icons/es/Ollama";
import OpenAI from "@lobehub/icons/es/OpenAI";
import OpenRouter from "@lobehub/icons/es/OpenRouter";
import Qwen from "@lobehub/icons/es/Qwen";
import { Sparkles } from "lucide-react";
import type { CSSProperties } from "react";

export type ModelBrand =
  | "openai"
  | "deepseek"
  | "anthropic"
  | "claude"
  | "gemini"
  | "qwen"
  | "glm"
  | "mimo"
  | "grok"
  | "ollama"
  | "openrouter"
  | "custom";

export const modelBrandFor = (model = "", provider = ""): ModelBrand => {
  const value = `${provider} ${model}`.trim().toLowerCase();
  if (/deepseek/.test(value)) return "deepseek";
  if (/\b(?:claude)\b/.test(value)) return "claude";
  if (/\b(?:anthropic)\b/.test(value)) return "anthropic";
  if (/\b(?:gemini|google)\b/.test(value)) return "gemini";
  if (/(?:^|[\s/_-])(?:qwen|dashscope|alibaba)/.test(value)) return "qwen";
  if (/(?:^|[\s/_-])(?:chatglm|glm|zhipu|bigmodel)/.test(value)) return "glm";
  if (/(?:^|[\s/_-])(?:mimo|xiaomi)/.test(value)) return "mimo";
  if (/\b(?:grok|xai|x\.ai)\b/.test(value)) return "grok";
  if (/\bollama\b/.test(value)) return "ollama";
  if (/\bopenrouter\b/.test(value)) return "openrouter";
  if (/\b(?:gpt|openai|o[134](?:\b|-))/.test(value)) return "openai";
  return "custom";
};

export const ModelProviderIcon = ({
  model,
  provider,
  size = 16,
  framed = false,
  className,
}: {
  model?: string;
  provider?: string;
  size?: number;
  framed?: boolean;
  className?: string;
}) => {
  const brand = modelBrandFor(model, provider);
  const iconProps = { width: size, height: size, "aria-hidden": true } as const;
  let icon: React.ReactNode;

  switch (brand) {
    case "openai": icon = <OpenAI {...iconProps} />; break;
    case "deepseek": icon = <DeepSeek.Color {...iconProps} />; break;
    case "anthropic": icon = <Anthropic {...iconProps} />; break;
    case "claude": icon = <Claude.Color {...iconProps} />; break;
    case "gemini": icon = <Gemini.Color {...iconProps} />; break;
    case "qwen": icon = <Qwen.Color {...iconProps} />; break;
    case "glm": icon = <ChatGLM.Color {...iconProps} />; break;
    case "mimo": icon = <Icon icon={xiaomiIcon} width={size} height={size} color="#ff6900" aria-hidden="true" />; break;
    case "grok": icon = <Grok {...iconProps} />; break;
    case "ollama": icon = <Ollama {...iconProps} />; break;
    case "openrouter": icon = <OpenRouter {...iconProps} />; break;
    default: icon = <Sparkles size={size} aria-hidden="true" />;
  }

  return (
    <span
      className={className}
      data-model-brand={brand}
      title={brand === "custom" ? "Custom model" : undefined}
      style={iconFrameStyle(size, framed)}
    >
      {icon}
    </span>
  );
};

const iconFrameStyle = (size: number, framed: boolean): CSSProperties => ({
  width: framed ? Math.max(size + 14, 32) : size,
  height: framed ? Math.max(size + 14, 32) : size,
  display: "inline-flex",
  alignItems: "center",
  justifyContent: "center",
  flexShrink: 0,
  borderRadius: framed ? "var(--radius-sm, 8px)" : undefined,
  border: framed ? "1px solid var(--border-subtle)" : undefined,
  background: framed ? "var(--surface-base)" : undefined,
  color: "var(--text-secondary)",
});
