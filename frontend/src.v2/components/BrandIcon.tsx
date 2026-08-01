import { Icon } from "@iconify/react";
import type { IconifyIcon } from "@iconify/types";
import braveIcon from "@iconify-icons/simple-icons/brave";
import cloudflareIcon from "@iconify-icons/simple-icons/cloudflare";
import discordIcon from "@iconify-icons/simple-icons/discord";
import dockerIcon from "@iconify-icons/simple-icons/docker";
import dropboxIcon from "@iconify-icons/simple-icons/dropbox";
import githubIcon from "@iconify-icons/simple-icons/github";
import googleDriveIcon from "@iconify-icons/simple-icons/googledrive";
import linearIcon from "@iconify-icons/simple-icons/linear";
import mongodbIcon from "@iconify-icons/simple-icons/mongodb";
import mysqlIcon from "@iconify-icons/simple-icons/mysql";
import npmIcon from "@iconify-icons/simple-icons/npm";
import playwrightIcon from "@iconify-icons/simple-icons/playwright";
import postgresqlIcon from "@iconify-icons/simple-icons/postgresql";
import puppeteerIcon from "@iconify-icons/simple-icons/puppeteer";
import redisIcon from "@iconify-icons/simple-icons/redis";
import sentryIcon from "@iconify-icons/simple-icons/sentry";
import slackIcon from "@iconify-icons/simple-icons/slack";
import sqliteIcon from "@iconify-icons/simple-icons/sqlite";
import stripeIcon from "@iconify-icons/simple-icons/stripe";
import supabaseIcon from "@iconify-icons/simple-icons/supabase";
import { Blend, Globe2, Sparkles } from "lucide-react";
import { useEffect, useState, type CSSProperties, type ReactNode } from "react";
import anthropicIcon from "@lobehub/icons-static-svg/icons/anthropic.svg?url";
import claudeIcon from "@lobehub/icons-static-svg/icons/claude-color.svg?url";
import deepseekIcon from "@lobehub/icons-static-svg/icons/deepseek-color.svg?url";
import figmaIcon from "@lobehub/icons-static-svg/icons/figma-color.svg?url";
import geminiIcon from "@lobehub/icons-static-svg/icons/gemini-color.svg?url";
import googleIcon from "@lobehub/icons-static-svg/icons/google-color.svg?url";
import microsoftIcon from "@lobehub/icons-static-svg/icons/microsoft-color.svg?url";
import notionIcon from "@lobehub/icons-static-svg/icons/notion.svg?url";
import openaiIcon from "@lobehub/icons-static-svg/icons/openai.svg?url";
import openrouterIcon from "@lobehub/icons-static-svg/icons/openrouter.svg?url";
import vercelIcon from "@lobehub/icons-static-svg/icons/vercel.svg?url";
import "./BrandIcon.css";

type BrandAsset = { label: string; asset: string };

const BRAND_ASSETS: Array<[RegExp, BrandAsset]> = [
  [/deepseek/, { label: "DeepSeek", asset: deepseekIcon }],
  [/chatgpt|openai|\bgpt[-_\d]|\bcodex\b|\bo[134](?:[-_\d]|$)/, { label: "OpenAI", asset: openaiIcon }],
  [/claude/, { label: "Claude", asset: claudeIcon }],
  [/anthropic/, { label: "Anthropic", asset: anthropicIcon }],
  [/gemini|google(?:\s|[-_/])?ai\b/, { label: "Google Gemini", asset: geminiIcon }],
  [/openrouter/, { label: "OpenRouter", asset: openrouterIcon }],
  [/figma/, { label: "Figma", asset: figmaIcon }],
  [/notion/, { label: "Notion", asset: notionIcon }],
  [/vercel/, { label: "Vercel", asset: vercelIcon }],
  [/microsoft/, { label: "Microsoft", asset: microsoftIcon }],
  [/\bgoogle\b/, { label: "Google", asset: googleIcon }],
];

const BRAND_ICONS: Array<[RegExp, { label: string; icon: IconifyIcon }]> = [
  [/github/, { label: "GitHub", icon: githubIcon }],
  [/slack/, { label: "Slack", icon: slackIcon }],
  [/discord/, { label: "Discord", icon: discordIcon }],
  [/docker/, { label: "Docker", icon: dockerIcon }],
  [/cloudflare/, { label: "Cloudflare", icon: cloudflareIcon }],
  [/linear/, { label: "Linear", icon: linearIcon }],
  [/sentry/, { label: "Sentry", icon: sentryIcon }],
  [/stripe/, { label: "Stripe", icon: stripeIcon }],
  [/supabase/, { label: "Supabase", icon: supabaseIcon }],
  [/mongodb/, { label: "MongoDB", icon: mongodbIcon }],
  [/mysql/, { label: "MySQL", icon: mysqlIcon }],
  [/redis/, { label: "Redis", icon: redisIcon }],
  [/google\s*drive|googledrive/, { label: "Google Drive", icon: googleDriveIcon }],
  [/dropbox/, { label: "Dropbox", icon: dropboxIcon }],
  [/(?:^|\s|[/@_-])npm(?:$|\s|[/@_-])/, { label: "npm", icon: npmIcon }],
  [/postgres(?:ql)?|\bpostgres\b/, { label: "PostgreSQL", icon: postgresqlIcon }],
  [/sqlite/, { label: "SQLite", icon: sqliteIcon }],
  [/playwright/, { label: "Playwright", icon: playwrightIcon }],
  [/puppeteer/, { label: "Puppeteer", icon: puppeteerIcon }],
  [/brave/, { label: "Brave", icon: braveIcon }],
];

export type BrandIconFallback = "plugin" | "skill" | "web";

export const resolveBrandIcon = (value: string): BrandAsset | { label: string; icon: IconifyIcon } | null => {
  const normalized = value.trim().toLowerCase();
  if (!normalized) return null;
  return BRAND_ASSETS.find(([pattern]) => pattern.test(normalized))?.[1]
    ?? BRAND_ICONS.find(([pattern]) => pattern.test(normalized))?.[1]
    ?? null;
};

const safeWebUrl = (value?: string): URL | null => {
  if (!value?.trim()) return null;
  try {
    const url = new URL(value);
    if (url.protocol === "https:") return url;
    if (url.protocol === "http:" && ["localhost", "127.0.0.1", "::1"].includes(url.hostname)) return url;
  } catch {
    return null;
  }
  return null;
};

export const resolveWebsiteIconCandidates = (iconUrl?: string, websiteUrl?: string): string[] => {
  const candidates: string[] = [];
  const explicitIcon = safeWebUrl(iconUrl);
  if (explicitIcon) candidates.push(explicitIcon.toString());
  const website = safeWebUrl(websiteUrl);
  if (website) {
    const domainIcon = new URL("https://www.google.com/s2/favicons");
    domainIcon.searchParams.set("domain_url", website.origin);
    domainIcon.searchParams.set("sz", "64");
    candidates.push(domainIcon.toString());
    candidates.push(new URL("/favicon.ico", website.origin).toString());
  }
  return [...new Set(candidates)];
};

export const resolveWebsiteIcon = (iconUrl?: string, websiteUrl?: string): string =>
  resolveWebsiteIconCandidates(iconUrl, websiteUrl)[0] ?? "";

export const BrandIcon = ({
  value,
  size = 20,
  fallback = "plugin",
  className,
  title,
  iconUrl,
  websiteUrl,
  fallbackIcon,
}: {
  value: string;
  size?: number;
  fallback?: BrandIconFallback;
  className?: string;
  title?: string;
  iconUrl?: string;
  websiteUrl?: string;
  fallbackIcon?: ReactNode;
}) => {
  const brand = resolveBrandIcon(value);
  const remoteCandidates = resolveWebsiteIconCandidates(iconUrl, websiteUrl);
  const remoteCandidateKey = remoteCandidates.join("\n");
  const [failedRemoteIcons, setFailedRemoteIcons] = useState<string[]>([]);
  useEffect(() => setFailedRemoteIcons([]), [remoteCandidateKey]);
  const remoteIcon = remoteCandidates.find((candidate) => !failedRemoteIcons.includes(candidate)) ?? "";
  // Prefer bundled, verified brand assets. Remote favicons are only a fallback
  // for unknown sites, avoiding an unnecessary third-party request whenever
  // the product/domain is already recognized locally.
  const showRemoteIcon = !brand && Boolean(remoteIcon);
  const style = { width: size, height: size } satisfies CSSProperties;
  const accessibleTitle = title ?? brand?.label ?? value;

  return (
    <span className={`brand-icon${className ? ` ${className}` : ""}`} style={style} title={accessibleTitle} aria-hidden="true" data-brand={brand?.label.toLowerCase() ?? (showRemoteIcon ? "website" : "generic")}>
      {showRemoteIcon ? (
        <img
          src={remoteIcon}
          alt=""
          width={size}
          height={size}
          referrerPolicy="no-referrer"
          onError={() => setFailedRemoteIcons((failed) => failed.includes(remoteIcon) ? failed : [...failed, remoteIcon])}
        />
      ) : brand && "asset" in brand ? (
        <img src={brand.asset} alt="" width={size} height={size} />
      ) : brand && "icon" in brand ? (
        <Icon icon={brand.icon} width={size} height={size} />
      ) : fallbackIcon ? (
        fallbackIcon
      ) : fallback === "web" ? (
        <Globe2 size={size} strokeWidth={1.8} />
      ) : fallback === "skill" ? (
        <Sparkles size={size} strokeWidth={1.8} />
      ) : (
        <Blend size={size} strokeWidth={1.8} />
      )}
    </span>
  );
};
