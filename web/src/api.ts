// Talks to the studio server: ADK sessions, /run_sse streaming, artifacts, and /api routes.

export type Part = {
  text?: string;
  thought?: boolean;
  inlineData?: { mimeType: string; data: string; displayName?: string };
  functionCall?: { id?: string; name: string; args?: Record<string, unknown> };
  functionResponse?: { id?: string; name: string; response?: Record<string, unknown> };
};
export type AdkEvent = {
  id: string;
  author: string;
  partial?: boolean;
  timestamp?: number;
  content?: { role?: string; parts?: Part[] };
  actions?: { stateDelta?: Record<string, unknown>; artifactDelta?: Record<string, number> };
  errorMessage?: string;
};
export type Session = { id: string; lastUpdateTime?: number; events?: AdkEvent[]; state?: Record<string, unknown> };
export type Usage = { calls: number; cost_usd: number };
export type Pricing = {
  currency: string;
  credit_note: string;
  free: { price: number; credits: number };
  pro: { monthly: number; annual: number; credits: number };
  topup: { price: number; credits: number };
  typical: { post: number; brand: number; short_motion: number; film_1min: number; looks: number };
};
export type Billing = {
  plan: "free" | "pro";
  owner: boolean;
  allowance: number;
  topup: number;
  used: number;
  balance: number | null;
  period_start: string;
  period_end: string | null;
  subscription: { status: string | null; access_until: string | null };
  can_manage: boolean;
  billing_ready: boolean;
  credits_per_usd: number;
};
export type BillingItem = "pro_monthly" | "pro_annual" | "topup";
export type Config = {
  auth: "off" | "firebase";
  app_name: string;
  pricing: Pricing;
  firebase: { apiKey: string; authDomain: string; projectId: string; appId: string } | null;
};

let getToken: () => Promise<string | null> = async () => null;
let uid = "dev";
let appName = "app";

export function setIdentity(id: string, tokenFn: () => Promise<string | null>) {
  uid = id;
  getToken = tokenFn;
}

async function call(path: string, init: RequestInit = {}): Promise<Response> {
  const token = await getToken();
  const headers = new Headers(init.headers);
  if (token) headers.set("Authorization", `Bearer ${token}`);
  if (init.body && typeof init.body === "string") headers.set("Content-Type", "application/json");
  const res = await fetch(path, { ...init, headers });
  if (!res.ok) {
    let detail = `${res.status} ${res.statusText}`;
    try {
      const body = await res.json();
      detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail ?? body);
    } catch {
      /* not JSON */
    }
    throw Object.assign(new Error(detail), { status: res.status });
  }
  return res;
}

export async function loadConfig(): Promise<Config> {
  const c: Config = await (await fetch("/api/config")).json();
  appName = c.app_name;
  return c;
}

const base = () => `/apps/${appName}/users/${encodeURIComponent(uid)}/sessions`;

export const listSessions = async (): Promise<Session[]> => (await call(base())).json();
export const createSession = async (): Promise<Session> => (await call(base(), { method: "POST", body: "{}" })).json();
export const getSession = async (id: string): Promise<Session> => (await call(`${base()}/${id}`)).json();
/** Deletes a request with every file it produced and stops its background work. */
export const deleteRequest = async (id: string): Promise<{ files: number }> =>
  (await call(`/api/requests/${encodeURIComponent(id)}`, { method: "DELETE" })).json();

export async function getArtifact(sessionId: string, name: string): Promise<{ url: string; mime: string }> {
  const part: Part = await (await call(`${base()}/${sessionId}/artifacts/${encodeURIComponent(name)}`)).json();
  const blob = part.inlineData;
  if (!blob) throw new Error(`artifact ${name} has no data`);
  const bytes = Uint8Array.from(atob(blob.data.replace(/-/g, "+").replace(/_/g, "/")), (c) => c.charCodeAt(0));
  return { url: URL.createObjectURL(new Blob([bytes], { type: blob.mimeType })), mime: blob.mimeType };
}

export const getBrand = async () => (await call("/api/brand")).json();
export const getBrandDraft = async () => (await call("/api/brand/draft")).json();
export const activateBrandDraft = async () => (await call("/api/brand/draft/activate", { method: "POST" })).json();
export const discardBrandDraft = async () => call("/api/brand/draft", { method: "DELETE" });
export async function putBrand(file: File) {
  const form = new FormData();
  form.append("file", file);
  return (await call("/api/brand", { method: "PUT", body: form })).json();
}
export const getBilling = async (): Promise<Billing> => (await call("/api/billing")).json();
export const billingCheckout = async (item: BillingItem): Promise<string> =>
  (await (await call("/api/billing/checkout", { method: "POST", body: JSON.stringify({ item }) })).json()).url;
export const billingPortal = async (): Promise<string> =>
  (await (await call("/api/billing/portal", { method: "POST", body: "{}" })).json()).url;
/** True for the API's "out of credits" refusal (HTTP 402). */
export const isOutOfCredits = (e: unknown) => (e as { status?: number })?.status === 402;
export const getUsage = async (): Promise<{ this_month: Usage & { tokens: number }; all_time: Usage }> =>
  (await call("/api/usage")).json();

export async function fileToPart(file: File): Promise<Part> {
  const data = await new Promise<string>((ok, fail) => {
    const r = new FileReader();
    r.onload = () => ok(String(r.result).split(",", 2)[1]);
    r.onerror = () => fail(r.error);
    r.readAsDataURL(file);
  });
  return { inlineData: { mimeType: file.type || "application/octet-stream", data, displayName: file.name } };
}

// ---------------------------------------------------------------- background jobs

export type Job = {
  id: string;
  session_id: string;
  status: "queued" | "running" | "succeeded" | "failed";
  attempts: number;
  error: string | null;
  created_at: string;
  updated_at: string;
  finished_at: string | null;
};
export const isActive = (j?: Job | null) => !!j && (j.status === "queued" || j.status === "running");

export async function startJob(sessionId: string | null, parts: Part[]): Promise<{ job: Job; session_id: string }> {
  const body = { ...(sessionId ? { session_id: sessionId } : {}), message: { role: "user", parts } };
  return (await call("/api/jobs", { method: "POST", body: JSON.stringify(body) })).json();
}
export const listJobs = async (): Promise<Job[]> => (await call("/api/jobs")).json();
export const getJob = async (id: string): Promise<Job> => (await call(`/api/jobs/${id}`)).json();
export const getMe = async (): Promise<{ workspace_id: string; email: string | null }> => (await call("/api/me")).json();

export type Task = {
  id: string;
  label: string;
  effort: string;
  status: "queued" | "running" | "succeeded" | "failed" | "cancelled";
  progress: string | null;
  cost_usd?: number;
  created_at: string;
  finished_at: string | null;
};
export const listTasks = async (sessionId: string): Promise<Task[]> =>
  (await call(`/api/tasks?session_id=${encodeURIComponent(sessionId)}`)).json();
export const taskActive = (t: Task) => t.status === "queued" || t.status === "running";
/** A lead turn that carries helpers' endings, written by the server, not the person. */
export const NOTICE = "[Background notice, not from the person]";
export const CHECK_IN = "[Check-in, not from the person]";
export const isNotice = (text: string) => text.startsWith(NOTICE) || text.startsWith(CHECK_IN);
/** A check-in with nothing to report (design-agent's quiet replies): never shown. */
export const QUIET = "[quiet]";

// ---------------------------------------------------------------- background work, in plain words

export type Tone = "done" | "stopped" | "attention" | "info" | "live";
export type BackgroundEvent = { tone: Tone; title: string; detail?: string };

/** What a helper is, for a person: "Look A", "Render · Friday sting", "Production · Launch film". */
export function friendlyTask(label: string, effort?: string): { kind: string; name: string } {
  const t = friendlyTaskRaw(label, effort);
  // The agent often starts a label with the same word ("Render Friday sting"): say it once.
  const name = t.name.replace(new RegExp(`^${t.kind}\\s*[:·-]?\\s*`, "i"), "");
  return { kind: t.kind, name: name ? name.charAt(0).toUpperCase() + name.slice(1) : "" };
}

function friendlyTaskRaw(label: string, effort?: string): { kind: string; name: string } {
  const look = /^look ([a-z0-9]+):/i.exec(label);
  if (look) return { kind: `Look ${look[1].toUpperCase()}`, name: "" };
  if (label.startsWith("render: ") || effort === "render") return { kind: "Render", name: label.replace(/^render: /, "") };
  if (label.startsWith("pipeline: ") || effort === "pipeline") {
    const name = label.replace(/^pipeline: /, "").replace(/_/g, " ");
    return { kind: "Production", name: name.charAt(0).toUpperCase() + name.slice(1) };
  }
  if (label.startsWith("learn from job: ")) return { kind: "Notes for next time", name: "" };
  return { kind: "Helper", name: label };
}

const clean = (s: string, max = 140) => {
  let line = s.split(/[\n{]/)[0].replace(/^\[urgent\]\s*/, "").trim();
  line = line.charAt(0).toUpperCase() + line.slice(1);
  return line.length > max ? `${line.slice(0, max - 1).trimEnd()}…` : line;
};

/** Background notices and check-ins (server-written turns) as short events; reports stay for the agent. */
export function readNotice(text: string): BackgroundEvent[] {
  if (text.startsWith(CHECK_IN)) {
    const reason = /^\(([^)]*)\)/.exec(text.slice(CHECK_IN.length).trim())?.[1];
    return [{ tone: "info", title: "Checked in", detail: reason || undefined }];
  }
  const out: BackgroundEvent[] = [];
  for (const block of text.split(NOTICE).map((b) => b.trim()).filter(Boolean)) {
    let m = /^Task \S+ "(.+?)" \((\w+)\) (finished|failed|was cancelled)\./.exec(block);
    if (m) {
      const { kind, name } = friendlyTask(m[1], m[2]);
      const verb = m[3] === "finished" ? (kind.startsWith("Look") ? "ready" : "done") : m[3] === "failed" ? "stopped" : "cancelled";
      const interrupted = /Interrupted/.test(block);
      out.push({
        tone: m[3] === "finished" ? "done" : "stopped",
        title: `${kind} ${interrupted ? "interrupted" : verb}${name ? ` · ${name}` : ""}`,
      });
      continue;
    }
    if ((m = /^Task \S+ "(.+?)" needs you now: ([\s\S]*)/.exec(block))) {
      const { kind, name } = friendlyTask(m[1]);
      out.push({ tone: "attention", title: `${kind} needs attention${name ? ` · ${name}` : ""}`, detail: clean(m[2]) });
      continue;
    }
    if ((m = /^render "(.+?)" shows no new output for (\d+) min/.exec(block))) {
      out.push({ tone: "attention", title: `Render may be stuck · ${m[1]}`, detail: `No progress for ${m[2]} min` });
      continue;
    }
    if ((m = /^Pipeline \S+ \((\w+)\) asks: ([\s\S]*?) Options:/.exec(block))) {
      out.push({ tone: "attention", title: "Production needs your answer", detail: clean(m[2]) });
      continue;
    }
    out.push({ tone: "info", title: "Background update", detail: clean(block) });
  }
  return out;
}

/** One helper row: its status in words. */
export function helperFraction(t: Task): number | null {
  const m = /^(\d{1,3})%/.exec(t.progress ?? "");
  return taskActive(t) && m ? Math.min(100, Number(m[1])) / 100 : null;
}

export function helperStatus(t: Task): { tone: Tone; text: string } {
  if (t.status === "succeeded") return { tone: "done", text: "Done" };
  if (t.status === "failed") return { tone: "stopped", text: t.progress?.startsWith("Interrupted") ? "Interrupted" : "Stopped" };
  if (t.status === "cancelled") return { tone: "stopped", text: "Cancelled" };
  if ((t.status as string) === "waiting") return { tone: "attention", text: "Waiting for you" };
  if (t.progress?.startsWith("[urgent]")) return { tone: "attention", text: clean(t.progress, 90) };
  if (t.status === "queued") return { tone: "live", text: "Starting…" };
  return { tone: "live", text: t.progress ? clean(t.progress, 90) : "Working…" };
}
