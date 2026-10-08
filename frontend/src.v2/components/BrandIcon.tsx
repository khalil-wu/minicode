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
import { Blocks, BookOpenText, Globe2 } from "../lib/icons";
import { useState, type CSSProperties, type ReactNode } from "react";
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

type BrandAsset = { label: string; asset: string; color: boolean };

const BRAND_ASSETS: Array<[RegExp, BrandAsset]> = [
  [/deepseek/, { label: "DeepSeek", asset: deepseekIcon, color: true }],
  [/chatgpt|openai|\bgpt[-_\d]|\bcodex\b|\bo[134](?:[-_\d]|$)/, { label: "OpenAI", asset: openaiIcon, color: false }],
  [/claude/, { label: "Claude", asset: claudeIcon, color: true }],
  [/anthropic/, { label: "Anthropic", asset: anthropicIcon, color: false }],
  [/gemini|google(?:\s|[-_/])?ai\b/, { label: "Google Gemini", asset: geminiIcon, color: true }],
  [/openrouter/, { label: "OpenRouter", asset: openrouterIcon, color: false }],
  [/figma/, { label: "Figma", asset: figmaIcon, color: true }],
  [/notion/, { label: "Notion", asset: notionIcon, color: false }],
  [/vercel/, { label: "Vercel", asset: vercelIcon, color: false }],
  [/microsoft/, { label: "Microsoft", asset: microsoftIcon, color: true }],
  [/\bgoogle\b(?![\s/_-]*drive)/, { label: "Google", asset: googleIcon, color: true }],
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
  [/google[\s/_-]*drive|googledrive/, { label: "Google Drive", icon: googleDriveIcon }],
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

const webUrl = (value?: string): URL | null => {
  if (!value?.trim()) return null;
  try {
    const url = value.startsWith("/") && typeof window !== "undefined"
      ? new URL(value, window.location.origin) : new URL(value);
    if (url.protocol === "https:" || url.protocol === "http:") return url;
  } catch {
    return null;
  }
  return null;
};

const safeWebIconUrl = (value?: string): URL | null => {
  const url = webUrl(value);
  return url && (url.protocol === "https:" || ["localhost", "127.0.0.1", "[::1]"].includes(url.hostname)
    || (typeof window !== "undefined" && url.origin === window.location.origin)) ? url : null;
};

const WEBSITE_BRANDS: Array<[string, string]> = [
  ["drive.google.com", "Google Drive"], ["gemini.google.com", "Gemini"], ["ai.google.dev", "Gemini"],
  ["openai.com", "OpenAI"], ["chatgpt.com", "OpenAI"], ["claude.ai", "Claude"], ["anthropic.com", "Anthropic"],
  ["deepseek.com", "DeepSeek"], ["openrouter.ai", "OpenRouter"], ["figma.com", "Figma"], ["notion.so", "Notion"],
  ["vercel.com", "Vercel"], ["microsoft.com", "Microsoft"], ["google.com", "Google"],
  ["github.com", "GitHub"], ["slack.com", "Slack"], ["discord.com", "Discord"], ["discord.gg", "Discord"],
  ["docker.com", "Docker"], ["cloudflare.com", "Cloudflare"], ["linear.app", "Linear"], ["sentry.io", "Sentry"],
  ["stripe.com", "Stripe"], ["supabase.com", "Supabase"], ["mongodb.com", "MongoDB"], ["mysql.com", "MySQL"],
  ["redis.io", "Redis"], ["dropbox.com", "Dropbox"], ["npmjs.com", "npm"], ["npmjs.org", "npm"],
  ["postgresql.org", "PostgreSQL"], ["sqlite.org", "SQLite"], ["playwright.dev", "Playwright"],
  ["pptr.dev", "Puppeteer"], ["brave.com", "Brave"],
];

export const resolveWebsiteBrandIcon = (value?: string): ReturnType<typeof resolveBrandIcon> => {
  const host = webUrl(value)?.hostname.toLowerCase();
  const brand = host && WEBSITE_BRANDS.find(([domain]) => host === domain || host.endsWith(`.${domain}`));
  return brand ? resolveBrandIcon(brand[1]) : null;
};

export const resolveWebsiteIconCandidates = (iconUrl?: string, websiteUrl?: string): string[] => {
  const candidates: string[] = [];
  const explicitIcon = safeWebIconUrl(iconUrl);
  if (explicitIcon) candidates.push(explicitIcon.toString());
  const website = safeWebIconUrl(websiteUrl);
  if (website) {
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
  inferBrand = true,
}: {
  value: string;
  size?: number;
  fallback?: BrandIconFallback;
  className?: string;
  title?: string;
  iconUrl?: string;
  websiteUrl?: string;
  fallbackIcon?: ReactNode;
  inferBrand?: boolean;
}) => {
  const brand = inferBrand ? websiteUrl ? resolveWebsiteBrandIcon(websiteUrl) : resolveBrandIcon(value) : null;
  const remoteCandidates = fallback === "web" && brand ? [] : resolveWebsiteIconCandidates(iconUrl, brand ? undefined : websiteUrl);
  const remoteCandidateKey = remoteCandidates.join("\n");
  const [failedRemoteIcons, setFailedRemoteIcons] = useState<{ scope: string; urls: string[] }>({ scope: remoteCandidateKey, urls: [] });
  const failures = failedRemoteIcons.scope === remoteCandidateKey ? failedRemoteIcons.urls : [];
  const remoteIcon = remoteCandidates.find((candidate) => !failures.includes(candidate)) ?? "";
  const [remoteImage, setRemoteImage] = useState({ url: remoteIcon, loaded: false });
  if (remoteImage.url !== remoteIcon) setRemoteImage({ url: remoteIcon, loaded: false });
  // Known websites use their bundled identity. Declared extension logos and
  // unknown sites replace the local icon only after the image really loads.
  const showRemoteIcon = Boolean(remoteIcon) && remoteImage.url === remoteIcon && remoteImage.loaded;
  const style = { width: size, height: size } satisfies CSSProperties;
  const accessibleTitle = title ?? brand?.label ?? value;

  return (
    <span className={`brand-icon${className ? ` ${className}` : ""}`} style={style} title={accessibleTitle} aria-hidden="true" data-brand={showRemoteIcon ? "website" : brand?.label.toLowerCase() ?? "generic"}>
      {remoteIcon && (
        <img
          key={remoteIcon}
          src={remoteIcon}
          alt=""
          width={size}
          height={size}
          referrerPolicy="no-referrer"
          className="brand-icon-remote"
          hidden={!showRemoteIcon}
          onLoad={() => setRemoteImage({ url: remoteIcon, loaded: true })}
          onError={() => setFailedRemoteIcons((failed) => ({ scope: remoteCandidateKey,
            urls: [...(failed.scope === remoteCandidateKey ? failed.urls : []), remoteIcon] }))}
        />
      )}
      {!showRemoteIcon && (brand && "asset" in brand ? (
        <img src={brand.asset} alt="" width={size} height={size} className="brand-icon-image" data-icon-kind={brand.color ? "color" : "mono"} />
      ) : brand && "icon" in brand ? (
        <Icon icon={brand.icon} width={size} height={size} />
      ) : fallbackIcon ? (
        fallbackIcon
      ) : fallback === "web" ? (
        <Globe2 size={size} strokeWidth={1.8} />
      ) : fallback === "skill" ? (
        <BookOpenText size={size} strokeWidth={1.8} />
      ) : (
        <Blocks size={size} strokeWidth={1.8} />
      ))}
    </span>
  );
};
