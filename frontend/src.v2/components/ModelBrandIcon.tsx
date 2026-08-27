import { Sparkles } from "lucide-react";
import type { CSSProperties } from "react";
import "./ModelBrandIcon.css";
import { BrandIcon } from "./BrandIcon";
import anthropicIcon from "@lobehub/icons-static-svg/icons/anthropic.svg?url";
import claudeIcon from "@lobehub/icons-static-svg/icons/claude-color.svg?url";
import cohereIcon from "@lobehub/icons-static-svg/icons/cohere-color.svg?url";
import deepseekIcon from "@lobehub/icons-static-svg/icons/deepseek-color.svg?url";
import geminiIcon from "@lobehub/icons-static-svg/icons/gemini-color.svg?url";
import grokIcon from "@lobehub/icons-static-svg/icons/grok.svg?url";
import groqIcon from "@lobehub/icons-static-svg/icons/groq.svg?url";
import kimiIcon from "@lobehub/icons-static-svg/icons/kimi.svg?url";
import metaIcon from "@lobehub/icons-static-svg/icons/meta-color.svg?url";
import minimaxIcon from "@lobehub/icons-static-svg/icons/minimax-color.svg?url";
import mistralIcon from "@lobehub/icons-static-svg/icons/mistral-color.svg?url";
import moonshotIcon from "@lobehub/icons-static-svg/icons/moonshot.svg?url";
import nvidiaIcon from "@lobehub/icons-static-svg/icons/nvidia-color.svg?url";
import ollamaIcon from "@lobehub/icons-static-svg/icons/ollama.svg?url";
import openaiIcon from "@lobehub/icons-static-svg/icons/openai.svg?url";
import openrouterIcon from "@lobehub/icons-static-svg/icons/openrouter.svg?url";
import perplexityIcon from "@lobehub/icons-static-svg/icons/perplexity-color.svg?url";
import qwenIcon from "@lobehub/icons-static-svg/icons/qwen-color.svg?url";
import xiaomiMimoIcon from "@lobehub/icons-static-svg/icons/xiaomimimo.svg?url";
import zhipuIcon from "@lobehub/icons-static-svg/icons/zhipu-color.svg?url";

type BrandDefinition = {
  id: string;
  label: string;
  icon: string;
  color: boolean;
};

const BRAND_DEFINITIONS: BrandDefinition[] = [
  { id: "openrouter", label: "OpenRouter", icon: openrouterIcon, color: false },
  { id: "deepseek", label: "DeepSeek", icon: deepseekIcon, color: true },
  { id: "claude", label: "Claude", icon: claudeIcon, color: true },
  { id: "anthropic", label: "Anthropic", icon: anthropicIcon, color: false },
  { id: "gemini", label: "Gemini", icon: geminiIcon, color: true },
  { id: "qwen", label: "Qwen", icon: qwenIcon, color: true },
  { id: "mistral", label: "Mistral", icon: mistralIcon, color: true },
  { id: "kimi", label: "Kimi", icon: kimiIcon, color: false },
  { id: "moonshot", label: "Moonshot AI", icon: moonshotIcon, color: false },
  { id: "mimo", label: "Xiaomi MiMo", icon: xiaomiMimoIcon, color: false },
  { id: "minimax", label: "MiniMax", icon: minimaxIcon, color: true },
  { id: "zhipu", label: "Zhipu AI", icon: zhipuIcon, color: true },
  { id: "groq", label: "Groq", icon: groqIcon, color: false },
  { id: "grok", label: "xAI Grok", icon: grokIcon, color: false },
  { id: "perplexity", label: "Perplexity", icon: perplexityIcon, color: true },
  { id: "cohere", label: "Cohere", icon: cohereIcon, color: true },
  { id: "nvidia", label: "NVIDIA", icon: nvidiaIcon, color: true },
  { id: "ollama", label: "Ollama", icon: ollamaIcon, color: false },
  { id: "meta", label: "Meta", icon: metaIcon, color: true },
  { id: "openai", label: "OpenAI", icon: openaiIcon, color: false },
];

const aliases: Array<[RegExp, string]> = [
  [/openrouter/, "openrouter"],
  [/deepseek/, "deepseek"],
  [/claude/, "claude"],
  [/anthropic/, "anthropic"],
  [/gemini|google\/?(?:ai)?/, "gemini"],
  [/qwen|tongyi|alibaba/, "qwen"],
  [/mistral|mixtral/, "mistral"],
  [/kimi/, "kimi"],
  [/moonshot/, "moonshot"],
  [/xiaomi|mimo/, "mimo"],
  [/minimax/, "minimax"],
  [/zhipu|chatglm|\bglm[-_\d]/, "zhipu"],
  [/groq/, "groq"],
  [/grok|\bxai\b/, "grok"],
  [/perplexity|sonar/, "perplexity"],
  [/cohere|command-r/, "cohere"],
  [/nvidia|nemotron/, "nvidia"],
  [/ollama/, "ollama"],
  [/meta|llama/, "meta"],
  [/openai|chatgpt|\bgpt[-_\d]|\bcodex\b|\bo[134](?:[-_\d]|$)/, "openai"],
];

export const resolveModelBrand = (value: string): BrandDefinition | null => {
  const normalized = value.trim().toLowerCase();
  if (!normalized) return null;
  const id = aliases.find(([pattern]) => pattern.test(normalized))?.[1];
  return id ? BRAND_DEFINITIONS.find((brand) => brand.id === id) ?? null : null;
};

export const ModelBrandIcon = ({
  model,
  provider,
  size = 16,
  framed = false,
  className,
  websiteUrl,
}: {
  model?: string;
  provider?: string;
  size?: number;
  framed?: boolean;
  className?: string;
  websiteUrl?: string;
}) => {
  const brand = resolveModelBrand(model ?? "") ?? resolveModelBrand(provider ?? "");
  const iconSize = framed ? Math.max(13, Math.round(size * 0.72)) : size;
  const wrapperStyle: CSSProperties = {
    width: size,
    height: size,
    flex: "0 0 auto",
    display: "inline-flex",
    alignItems: "center",
    justifyContent: "center",
    borderRadius: framed ? "var(--radius-sm, 6px)" : 0,
    background: framed ? "color-mix(in srgb, var(--surface-soft) 72%, transparent)" : "transparent",
    border: framed ? "1px solid color-mix(in srgb, var(--border-subtle) 72%, transparent)" : 0,
    color: "var(--text-secondary)",
  };

  return (
    <span
      className={`model-brand-icon${framed ? " model-brand-icon-framed" : ""}${className ? ` ${className}` : ""}`}
      style={wrapperStyle}
      aria-hidden="true"
      data-model-brand={brand?.id ?? "custom"}
    >
      {!brand ? (
        <BrandIcon
          value={`${model || ""} ${provider || ""}`}
          websiteUrl={websiteUrl}
          fallback="skill"
          fallbackIcon={<Sparkles size={iconSize} strokeWidth={1.8} />}
          size={iconSize}
        />
      ) : (
        <img
          src={brand.icon}
          alt=""
          width={iconSize}
          height={iconSize}
          className={brand.color ? "model-brand-icon-color" : "model-brand-icon-mono"}
          data-icon-kind={brand.color ? "color" : "mono"}
        />
      )}
    </span>
  );
};
