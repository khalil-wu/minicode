export type NetworkTargetRisk = "public" | "local" | "private" | "invalid";

export interface NetworkTargetAssessment {
  normalizedUrl: string;
  protocol: string;
  host: string;
  risk: NetworkTargetRisk;
  requiresReview: boolean;
  reason: string;
}

const LOCAL_HOSTS = new Set(["localhost", "localhost.localdomain", "127.0.0.1", "::1", "0.0.0.0"]);

const parseIpv4 = (host: string): number[] | null => {
  const parts = host.split(".").map((part) => Number(part));
  if (parts.length !== 4 || parts.some((part) => !Number.isInteger(part) || part < 0 || part > 255)) return null;
  return parts;
};

const isLocalIpv4 = (host: string): boolean => {
  const parts = parseIpv4(host);
  if (!parts) return false;
  return parts[0] === 127 || parts.every((part) => part === 0);
};

const isPrivateIpv4 = (host: string): boolean => {
  const parts = parseIpv4(host);
  if (!parts) return false;
  const [a, b] = parts;
  return a === 10 || (a === 172 && b >= 16 && b <= 31) || (a === 192 && b === 168) || (a === 169 && b === 254);
};

const isPrivateIpv6 = (host: string): boolean => {
  const normalized = host.toLowerCase();
  return normalized.startsWith("fc") || normalized.startsWith("fd") || normalized.startsWith("fe80");
};

export const assessNetworkTargetUrl = (rawUrl: string): NetworkTargetAssessment => {
  const raw = String(rawUrl ?? "").trim();
  const withProtocol = /^[a-z][a-z\d+.-]*:\/\//i.test(raw) ? raw : `http://${raw}`;
  let parsed: URL;
  try {
    parsed = new URL(withProtocol);
  } catch {
    return {
      normalizedUrl: raw,
      protocol: "",
      host: "",
      risk: "invalid",
      requiresReview: true,
      reason: "URL is invalid",
    };
  }

  const protocol = parsed.protocol.replace(/:$/, "").toLowerCase();
  const host = parsed.hostname.replace(/^\[|\]$/g, "").toLowerCase();
  if (protocol !== "http" && protocol !== "https") {
    return {
      normalizedUrl: withProtocol,
      protocol,
      host,
      risk: "invalid",
      requiresReview: true,
      reason: "Only http(s) URLs are supported",
    };
  }
  if (!host) {
    return {
      normalizedUrl: withProtocol,
      protocol,
      host,
      risk: "invalid",
      requiresReview: true,
      reason: "URL host is required",
    };
  }
  if (LOCAL_HOSTS.has(host) || host.endsWith(".localhost") || isLocalIpv4(host)) {
    return {
      normalizedUrl: parsed.toString(),
      protocol,
      host,
      risk: "local",
      requiresReview: true,
      reason: "Localhost target",
    };
  }
  if (isPrivateIpv4(host) || isPrivateIpv6(host)) {
    return {
      normalizedUrl: parsed.toString(),
      protocol,
      host,
      risk: "private",
      requiresReview: true,
      reason: "Private network target",
    };
  }
  return {
    normalizedUrl: parsed.toString(),
    protocol,
    host,
    risk: "public",
    requiresReview: false,
    reason: "Public network target",
  };
};
