import { Fragment, useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import * as api from "./api";
import type { AdkEvent, Config, Part, Session } from "./api";
import { addSteps, Hand, WorkLog, type Step } from "./progress";

type Proof = { name: string; version: number; url?: string; mime?: string; error?: string };
type Turn =
  | { kind: "user"; id: string; text: string; files: string[] }
  | { kind: "agent"; id: string; steps: Step[]; end: number; text: string; error?: string; working: boolean };

const EXAMPLES = [
  "A LinkedIn card announcing our new pricing, 1200×627",
  "Three directions for our launch post, then I'll pick one",
  "A 6-second title sting with a voiceover saying “Welcome to Design Content Studio”",
  "A bar chart of monthly signups: Jan 120, Feb 180, Mar 260, Apr 410",
  "A one-page PDF explaining our design review service",
];

// ---------------------------------------------------------------- events -> view

function toTurns(events: AdkEvent[], working: boolean): Turn[] {
  const turns: Turn[] = [];
  for (const ev of events) {
    const parts = ev.content?.parts ?? [];
    if (ev.author === "user") {
      if (parts.some((p) => p.functionResponse)) continue;
      turns.push({
        kind: "user",
        id: ev.id,
        text: parts.map((p) => p.text ?? "").join(""),
        files: parts.flatMap((p) => (p.inlineData ? [p.inlineData.displayName ?? "attachment"] : [])),
      });
      continue;
    }
    let last = turns[turns.length - 1];
    if (!last || last.kind !== "agent") {
      last = { kind: "agent", id: ev.id, steps: [], end: 0, text: "", working: false };
      turns.push(last);
    }
    const at = ev.timestamp ?? Date.now() / 1000;
    last.end = Math.max(last.end, at);
    addSteps(last.steps, parts, at);
    for (const p of parts) {
      if (p.text && !p.thought && !ev.partial) last.text += (last.text ? "\n\n" : "") + p.text;
    }
    if (ev.errorMessage) last.error = ev.errorMessage;
  }
  const tail = turns[turns.length - 1];
  if (working) {
    if (tail?.kind === "agent") tail.working = true;
    else turns.push({ kind: "agent", id: "pending", steps: [], end: 0, text: "", working: true });
  }
  return turns;
}

function proofsFrom(events: AdkEvent[]): Map<string, number> {
  const latest = new Map<string, number>();
  for (const ev of events) for (const [name, v] of Object.entries(ev.actions?.artifactDelta ?? {})) latest.set(name, v);
  return latest;
}

function costFrom(events: AdkEvent[], state?: Record<string, unknown>): number | null {
  let usage = state?.usage as api.Usage | undefined;
  for (const ev of events) if (ev.actions?.stateDelta?.usage) usage = ev.actions.stateDelta.usage as api.Usage;
  return usage ? usage.cost_usd : null;
}

const money = (n: number) => (n < 0.01 ? "under $0.01" : `$${n.toFixed(2)}`);

function ago(seconds?: number): string {
  if (!seconds) return "";
  const s = Date.now() / 1000 - seconds;
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
  if (s < 172800) return "yesterday";
  return new Date(seconds * 1000).toLocaleDateString(undefined, { day: "numeric", month: "short" });
}

const initials = (name: string) =>
  name
    .split(/[\s@._-]+/)
    .filter(Boolean)
    .slice(0, 2)
    .map((w) => w[0]!.toUpperCase())
    .join("") || "?";

// ---------------------------------------------------------------- markdown-lite (no HTML injection)

function inline(text: string): ReactNode[] {
  return text.split(/(\*\*[^*]+\*\*|`[^`]+`)/g).map((chunk, i) =>
    chunk.startsWith("**") && chunk.endsWith("**") && chunk.length > 4 ? (
      <strong key={i}>{chunk.slice(2, -2)}</strong>
    ) : chunk.startsWith("`") && chunk.endsWith("`") && chunk.length > 2 ? (
      <code key={i}>{chunk.slice(1, -1)}</code>
    ) : (
      <Fragment key={i}>{chunk}</Fragment>
    ),
  );
}

function Prose({ text }: { text: string }) {
  const blocks: ReactNode[] = [];
  let list: { ordered: boolean; items: string[] } | null = null;
  const flush = () => {
    if (!list) return;
    const items = list.items.map((it, i) => <li key={i}>{inline(it)}</li>);
    blocks.push(list.ordered ? <ol key={blocks.length}>{items}</ol> : <ul key={blocks.length}>{items}</ul>);
    list = null;
  };
  for (const line of text.split("\n")) {
    const bullet = line.match(/^\s*[-*•]\s+(.*)/);
    const numbered = line.match(/^\s*\d+[.)]\s+(.*)/);
    if (bullet || numbered) {
      const ordered = !!numbered;
      if (list && list.ordered !== ordered) flush();
      list ??= { ordered, items: [] };
      list.items.push((bullet ?? numbered)![1]!);
    } else if (/^#{1,4}\s/.test(line)) {
      flush();
      blocks.push(<h4 key={blocks.length}>{inline(line.replace(/^#+\s*/, ""))}</h4>);
    } else if (line.trim()) {
      flush();
      blocks.push(<p key={blocks.length}>{inline(line)}</p>);
    } else flush();
  }
  flush();
  return <div className="prose">{blocks}</div>;
}

// ---------------------------------------------------------------- app

type AuthApi = {
  google: () => Promise<void>;
  signInEmail: (email: string, password: string) => Promise<void>;
  signUpEmail: (email: string, password: string) => Promise<void>;
  resetPassword: (email: string) => Promise<void>;
  resendVerification: () => Promise<void>;
  checkVerified: () => Promise<boolean>;
  signOut: () => Promise<void>;
};

/** Firebase error codes in plain words. */
function authMessage(e: unknown): string {
  const code = (e as { code?: string })?.code ?? "";
  const words: Record<string, string> = {
    "auth/invalid-credential": "That email and password don't match an account.",
    "auth/wrong-password": "That email and password don't match an account.",
    "auth/user-not-found": "That email and password don't match an account.",
    "auth/email-already-in-use": "An account with this email already exists. Sign in instead.",
    "auth/weak-password": "Use a password of at least 8 characters.",
    "auth/invalid-email": "That doesn't look like an email address.",
    "auth/too-many-requests": "Too many attempts. Wait a minute and try again.",
    "auth/popup-closed-by-user": "The Google window was closed before signing in.",
    "auth/network-request-failed": "No connection. Check your network and try again.",
  };
  return words[code] ?? `Sign-in did not finish (${code || (e as Error)?.message || "unknown error"}).`;
}

export default function App() {
  const [config, setConfig] = useState<Config | null>(null);
  const [user, setUser] = useState<{ uid: string; name: string } | null>(null);
  const [authReady, setAuthReady] = useState(false);
  const [authApi, setAuthApi] = useState<AuthApi | null>(null);
  const [unverified, setUnverified] = useState<string | null>(null); // email awaiting verification
  const [signOut, setSignOut] = useState<(() => Promise<void>) | null>(null);
  const [bootError, setBootError] = useState("");
  const [hash, setHash] = useState(window.location.hash);
  useEffect(() => {
    const onHash = () => setHash(window.location.hash);
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);

  useEffect(() => {
    (async () => {
      const c = await api.loadConfig();
      setConfig(c);
      if (c.auth === "off") {
        api.setIdentity("dev", async () => null);
        setUser({ uid: "dev", name: "Local dev" });
        setAuthReady(true);
        return;
      }
      const { initializeApp } = await import("firebase/app");
      const fa = await import("firebase/auth");
      const auth = fa.getAuth(initializeApp(c.firebase!));
      const accept = (u: import("firebase/auth").User) => {
        // Email/password accounts verify their address first (the server enforces it too).
        const password = u.providerData.some((p) => p.providerId === "password");
        if (password && !u.emailVerified) {
          setUser(null);
          setUnverified(u.email);
          return;
        }
        setUnverified(null);
        api.setIdentity(u.uid, () => u.getIdToken());
        setUser({ uid: u.uid, name: u.displayName || u.email || "Signed in" });
      };
      setAuthApi({
        google: async () => void (await fa.signInWithPopup(auth, new fa.GoogleAuthProvider())),
        signInEmail: async (email, password) => void (await fa.signInWithEmailAndPassword(auth, email, password)),
        signUpEmail: async (email, password) => {
          const cred = await fa.createUserWithEmailAndPassword(auth, email, password);
          await fa.sendEmailVerification(cred.user);
        },
        resetPassword: async (email) => fa.sendPasswordResetEmail(auth, email),
        resendVerification: async () => {
          if (auth.currentUser) await fa.sendEmailVerification(auth.currentUser);
        },
        checkVerified: async () => {
          const u = auth.currentUser;
          if (!u) return false;
          await u.reload();
          if (!u.emailVerified) return false;
          await u.getIdToken(true); // a fresh token carries email_verified
          accept(u);
          return true;
        },
        signOut: async () => fa.signOut(auth),
      });
      setSignOut(() => async () => fa.signOut(auth));
      fa.onAuthStateChanged(auth, (u) => {
        setAuthReady(true);
        if (!u) {
          setUnverified(null);
          return setUser(null);
        }
        accept(u);
      });
    })().catch((e) => setBootError(String(e)));
  }, []);

  if (bootError) return <p className="boot">Design Content Studio could not start: {bootError}. Reload the page to try again.</p>;
  if (!config || !authReady) return <p className="boot">Opening Design Content Studio…</p>;
  if (!user && unverified && authApi) return <VerifyEmail email={unverified} auth={authApi} />;
  if (!user && (hash === "#signin" || hash === "#signup"))
    return <SignIn key={hash} auth={authApi} initial={hash === "#signup" ? "signup" : "signin"} />;
  if (!user) return <Landing auth={authApi} pricing={config.pricing} />;
  return <Workspace who={user.name} onSignOut={signOut} pricing={config.pricing} />;
}

const MAKES = [
  { what: "Stills", line: "Posts, banners and covers that look like your brand made them.", brief: "A LinkedIn card announcing our new pricing" },
  { what: "Ad sets", line: "One idea, cut to every size you run, with the words rewrapped for each.", brief: "Our spring sale for Instagram, LinkedIn and Stories" },
  { what: "Charts and diagrams", line: "Your numbers drawn cleanly, labelled, in your colours.", brief: "Monthly signups: Jan 120, Feb 180, Mar 260" },
  { what: "PDFs", line: "One-pagers, price sheets and guides, laid out and ready to send.", brief: "A one-page PDF about our design review service" },
  { what: "Films", line: "Short animated films with a voiceover and music, timed to the words.", brief: "A 20-second product sting with a voiceover" },
  { what: "Your brand", line: "Give it your website. It finds your colours, type and logo, and works from them.", brief: "Set up our brand from halden.co" },
];

const HOW: { phase: "plan" | "design" | "check" | "deliver"; title: string; line: string }[] = [
  { phase: "plan", title: "Ask in plain words", line: "Type what you need the way you'd tell a designer. Attach a file if it helps." },
  { phase: "design", title: "It designs", line: "Design Content Studio writes a short brief, picks a direction and makes the files, in the background." },
  { phase: "check", title: "It checks its own work", line: "Every file is checked against your brand: colours, type, logo space, spelling and size." },
  { phase: "deliver", title: "You get the files", line: "Finished files land on your proof wall. We email you if you've closed the tab." },
];

const CHECKS = ["Colours from the Halden palette", "Headline in Halden Sans", "Logo clear space kept", "Copy spelled as written", "Each file at its exact size"];

/** The ad set from the hero brief: one message, three sizes, in the sample brand. */
function Ad({ shape }: { shape: "square" | "wide" | "tall" }) {
  return (
    <div className={`ad ad-${shape}`}>
      <span className="ad-logo">
        <span className="ad-mark" />
        Halden Cycles
      </span>
      <span className="ad-head">Spring tune-ups, 20% off</span>
      <span className="ad-cta">Book by 30 April</span>
    </div>
  );
}

function Pricing({ pricing: p }: { pricing: api.Pricing }) {
  const t = p.typical;
  return (
    <section className="land-price" id="pricing" aria-labelledby="price-title">
      <div className="price-head">
        <h2 id="price-title">Pricing</h2>
        <p>
          Pay for what you make. {p.credit_note}; bigger pieces take more.
        </p>
      </div>
      <div className="price-plans">
        <article className="price-plan">
          <h3>Free</h3>
          <p className="price">
            <strong>$0</strong>
          </p>
          <p className="price-credits">{p.free.credits} credits a month</p>
          <ul>
            <li>Posts, ad sizes, charts, diagrams, PDFs and GIF edits</li>
            <li>Your brand, set up from your website</li>
            <li>Every piece checked against your brand before delivery</li>
          </ul>
          <a className="secondary" href="#signup">
            Start free
          </a>
        </article>
        <article className="price-plan pro">
          <h3>Pro</h3>
          <p className="price">
            <strong>${p.pro.monthly}</strong> a month
          </p>
          <p className="price-credits">
            {p.pro.credits} credits a month · or ${p.pro.annual} a year, two months free
          </p>
          <ul>
            <li>Everything in Free</li>
            <li>Launch films and motion with voiceover and music</li>
            <li>Three directions side by side, then pick one</li>
            <li>Long work in the background, with an email when it's ready</li>
            <li>
              Top up anytime: {p.topup.credits} credits for ${p.topup.price}, and they don't expire
            </li>
          </ul>
          <a className="primary" href="#signup">
            Get Pro
          </a>
        </article>
      </div>
      <dl className="price-typical">
        <div>
          <dt>A post, chart or PDF</dt>
          <dd>about {t.post} credit</dd>
        </div>
        <div>
          <dt>A short motion piece</dt>
          <dd>about {t.short_motion} credits</dd>
        </div>
        <div>
          <dt>A 1-minute launch film</dt>
          <dd>about {t.film_1min} credits</dd>
        </div>
        <div>
          <dt>Three directions</dt>
          <dd>about {t.looks} credits</dd>
        </div>
      </dl>
    </section>
  );
}

function Landing({ auth, pricing }: { auth: AuthApi | null; pricing: api.Pricing }) {
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const google = async () => {
    if (!auth) return;
    setBusy(true);
    setError("");
    try {
      await auth.google();
    } catch (e) {
      setError(authMessage(e));
    } finally {
      setBusy(false);
    }
  };
  const start = (
    <div className="land-start">
      <button className="primary large" disabled={busy || !auth} onClick={google}>
        Continue with Google
      </button>
      <a className="secondary" href="#signup">
        Sign up with email
      </a>
      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}
    </div>
  );
  return (
    <div className="land">
      <header className="land-top">
        <span className="brandmark">Design Content Studio</span>
        <nav aria-label="Account">
          <a className="link" href="#pricing">
            Pricing
          </a>
          <a className="link" href="#signin">
            Sign in
          </a>
          <a className="primary" href="#signup">
            Get started
          </a>
        </nav>
      </header>

      <main>
        <section className="land-hero" aria-labelledby="land-title">
          <div className="land-intro">
            <h1 id="land-title">Say what you need. Design Content Studio hands back the files.</h1>
            <p>
              A designer that works from your brand. Ask for a post, an ad set, a chart, a PDF or a short film, and Design Content Studio
              designs it, checks it against your brand, and delivers files ready to use.
            </p>
            {start}
          </div>
          <figure className="land-wall">
            <blockquote className="slip">
              <p>Our spring sale ad for Instagram, LinkedIn and Stories. Use our brand.</p>
              <footer>The whole brief</footer>
            </blockquote>
            <div className="adset">
              <div className="pin adset-square">
                <div className="crop">
                  <Ad shape="square" />
                </div>
                <span className="pin-cap">1080 × 1080, Instagram</span>
              </div>
              <div className="pin adset-wide">
                <div className="crop">
                  <Ad shape="wide" />
                </div>
                <span className="pin-cap">1200 × 627, LinkedIn</span>
              </div>
              <div className="pin adset-tall">
                <div className="crop">
                  <Ad shape="tall" />
                </div>
                <span className="pin-cap">1080 × 1920, Stories</span>
              </div>
            </div>
            <figcaption className="checks">
              <span>Checked before delivery</span>
              <ul>
                {CHECKS.map((c) => (
                  <li key={c}>{c}</li>
                ))}
              </ul>
            </figcaption>
          </figure>
        </section>

        <section className="land-makes" aria-labelledby="makes-title">
          <div className="makes-copy">
            <h2 id="makes-title">What it makes</h2>
            <dl>
              {MAKES.map((m) => (
                <div key={m.what}>
                  <dt>{m.what}</dt>
                  <dd>
                    {m.line}
                    <q>{m.brief}</q>
                  </dd>
                </div>
              ))}
            </dl>
          </div>
          <div className="signin-mat makes-mat" aria-hidden="true">
            <Board />
          </div>
        </section>

        <section className="land-how" aria-labelledby="how-title">
          <h2 id="how-title">How it works</h2>
          <ol>
            {HOW.map((h) => (
              <li key={h.phase}>
                <Hand phase={h.phase} />
                <h3>{h.title}</h3>
                <p>{h.line}</p>
              </li>
            ))}
          </ol>
        </section>

        <Pricing pricing={pricing} />

        <section className="land-end" aria-labelledby="end-title">
          <h2 id="end-title">Your first brief takes a minute to write.</h2>
          <p>Sign up, add your website so Design Content Studio learns your brand, then ask for the first thing on your list.</p>
          {start}
          <p className="land-have">
            Have an account? <a href="#signin">Sign in</a>
          </p>
        </section>
      </main>
    </div>
  );
}

const SIGNIN_BRIEFS = [
  "Our launch post for LinkedIn, X and Instagram",
  "A 20-second product sting with a voiceover",
  "Set up our brand from acme.com",
];

/** The waveform under the film proof: a fixed, speech-like envelope (no randomness between renders). */
const WAVE = [3, 5, 9, 14, 11, 7, 12, 18, 15, 9, 4, 3, 6, 13, 17, 12, 8, 14, 10, 5, 3, 2, 4, 8, 12, 9, 5, 3];

function SignIn({ auth, initial }: { auth: AuthApi | null; initial: "signin" | "signup" }) {
  const [error, setError] = useState("");
  const [note, setNote] = useState("");
  const [mode, setMode] = useState<"signin" | "signup" | "reset">(initial);
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const run = async (fn: () => Promise<void>, done = "") => {
    setBusy(true);
    setError("");
    setNote("");
    try {
      await fn();
      if (done) setNote(done);
    } catch (e) {
      setError(authMessage(e));
    } finally {
      setBusy(false);
    }
  };
  const submit = (e: React.FormEvent) => {
    e.preventDefault();
    if (!auth) return;
    if (mode === "reset") return run(() => auth.resetPassword(email.trim()), `If ${email.trim()} has an account, a reset link is on its way.`);
    if (mode === "signup" && password.length < 8) return setError("Use a password of at least 8 characters.");
    return run(() => (mode === "signup" ? auth.signUpEmail(email.trim(), password) : auth.signInEmail(email.trim(), password)));
  };
  return (
    <main className="signin">
      <section className="signin-copy">
        <a className="brandmark" href="#" aria-label="Design Content Studio, back to the home page">
          Design Content Studio
        </a>
        <h1>A one-line brief in. Finished, on‑brand work out.</h1>
        <p>
          Posts and ad sets in every size, charts, one-pagers, GIFs and short films with voiceover. Design Content Studio learns your
          brand from your website, checks every file against it, and hands back files ready to post.
        </p>
        <ul className="signin-briefs" aria-label="Things people ask for">
          {SIGNIN_BRIEFS.map((b) => (
            <li key={b}>{b}</li>
          ))}
        </ul>
        <div className="auth">
          <button className="primary large google" disabled={busy || !auth} onClick={() => auth && run(auth.google)}>
            Continue with Google
          </button>
          <div className="auth-or" aria-hidden="true">
            <span>or use your email</span>
          </div>
          <form className="auth-form" onSubmit={submit}>
            <label>
              <span>Email</span>
              <input type="email" autoComplete="email" required value={email} onChange={(e) => setEmail(e.target.value)} />
            </label>
            {mode !== "reset" && (
              <label>
                <span>Password</span>
                <input
                  type="password"
                  autoComplete={mode === "signup" ? "new-password" : "current-password"}
                  required
                  minLength={mode === "signup" ? 8 : undefined}
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                />
              </label>
            )}
            <button className="secondary" type="submit" disabled={busy || !auth}>
              {busy ? "One moment…" : mode === "signup" ? "Create account" : mode === "reset" ? "Send reset link" : "Sign in"}
            </button>
            <p className="auth-switch">
              {mode === "signin" ? (
                <>
                  New here?{" "}
                  <button type="button" className="link" onClick={() => (setMode("signup"), setError(""), setNote(""))}>
                    Create an account
                  </button>
                  {" · "}
                  <button type="button" className="link" onClick={() => (setMode("reset"), setError(""), setNote(""))}>
                    Forgot password?
                  </button>
                </>
              ) : (
                <>
                  {mode === "signup" ? "Have an account?" : "Remembered it?"}{" "}
                  <button type="button" className="link" onClick={() => (setMode("signin"), setError(""), setNote(""))}>
                    Sign in
                  </button>
                </>
              )}
            </p>
          </form>
          {error && <p className="error" role="alert">{error}</p>}
          {note && <p className="auth-note" role="status">{note}</p>}
        </div>
        <p className="signin-note">Work runs in the background. Close the tab and we'll email you when it's ready.</p>
      </section>
      <section className="signin-mat" aria-hidden="true">
        <Board />
      </section>
    </main>
  );
}

/** A pinned board of the kinds of files Design Content Studio hands back, at true proportions. */
function Board() {
  return (
    <div className="board">
      <figure className="pin pin-film">
        <div className="crop">
          <div className="film">
            <div className="film-frame">
              <span className="film-mark" />
              <span className="film-title">Hello, Design Content Studio</span>
            </div>
            <div className="film-track">
              <div className="wave">
                {WAVE.map((h, i) => (
                  <span key={i} style={{ height: `${h * 5}%` }} />
                ))}
              </div>
              <span className="playhead" />
            </div>
          </div>
        </div>
        <figcaption>1920 × 1080 · 0:06 · voiceover</figcaption>
      </figure>
      <figure className="pin pin-post">
        <div className="crop">
          <div className="post">
            <span className="post-rule" />
            <span className="post-head">Studio hours are back</span>
          </div>
        </div>
        <figcaption>1080 × 1080</figcaption>
      </figure>
      <figure className="pin pin-chart">
        <div className="crop">
          <div className="chart">
            <span className="chart-title">Monthly active studios</span>
            <div className="bars">
              {[30, 44, 63, 100].map((h, i) => (
                <span key={i} className={i === 3 ? "hot" : ""} style={{ height: `${h}%` }} />
              ))}
            </div>
          </div>
        </div>
        <figcaption>1200 × 675</figcaption>
      </figure>
      <figure className="pin pin-page">
        <div className="crop">
          <div className="page">
            <span className="page-title">Design review</span>
            <span className="page-line" />
            <span className="page-line short" />
            <span className="page-card" />
            <span className="page-card" />
            <span className="page-price">From $400</span>
          </div>
        </div>
        <figcaption>US Letter · PDF</figcaption>
      </figure>
      <figure className="pin pin-story">
        <div className="crop">
          <div className="story">
            <span className="story-dot" />
            <span className="story-head">Friday reviews</span>
          </div>
        </div>
        <figcaption>1080 × 1920</figcaption>
      </figure>
    </div>
  );
}

function VerifyEmail({ email, auth }: { email: string; auth: AuthApi }) {
  const [msg, setMsg] = useState("");
  const [busy, setBusy] = useState(false);
  const act = async (fn: () => Promise<void>) => {
    setBusy(true);
    setMsg("");
    try {
      await fn();
    } catch (e) {
      setMsg(authMessage(e));
    } finally {
      setBusy(false);
    }
  };
  return (
    <main className="verify">
      <div className="verify-card">
        <div className="brandmark">Design Content Studio</div>
        <h1>Check your inbox</h1>
        <p>
          We sent a link to <strong>{email}</strong>. Open it to confirm the address, then come back here. Check spam if it
          hasn't arrived in a minute.
        </p>
        <div className="verify-actions">
          <button
            className="primary"
            disabled={busy}
            onClick={() =>
              act(async () => {
                if (!(await auth.checkVerified())) setMsg("Not confirmed yet. Open the link in the email, then try again.");
              })
            }
          >
            I've confirmed it
          </button>
          <button className="link" disabled={busy} onClick={() => act(async () => (await auth.resendVerification(), setMsg("Sent another link.")))}>
            Send the link again
          </button>
          <button className="link" disabled={busy} onClick={() => act(auth.signOut)}>
            Use a different account
          </button>
        </div>
        {msg && <p className="auth-note" role="status">{msg}</p>}
      </div>
    </main>
  );
}

function Workspace({ who, onSignOut, pricing }: { who: string; onSignOut: (() => Promise<void>) | null; pricing: api.Pricing }) {
  const [sessions, setSessions] = useState<Session[]>([]);
  const [current, setCurrent] = useState<string | null>(null);
  const [events, setEvents] = useState<AdkEvent[]>([]);
  const [state, setState] = useState<Record<string, unknown>>();
  const [jobs, setJobs] = useState<Map<string, api.Job>>(new Map());
  const [email, setEmail] = useState<string | null>(null);
  const [error, setError] = useState("");
  const [panel, setPanel] = useState<"brand" | "plan" | null>(null);
  const [billing, setBilling] = useState<api.Billing | null>(null);
  const [outOfCredits, setOutOfCredits] = useState(false);
  const [paid, setPaid] = useState(false);
  const refreshBilling = useCallback(() => api.getBilling().then(setBilling).catch(() => {}), []);
  const [sampleBrand, setSampleBrand] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState<string | null>(null);
  const [deleting, setDeleting] = useState(false);
  useEffect(() => {
    if (panel) return;
    api.getBrand().then((b) => setSampleBrand(!!b?.is_sample)).catch(() => {});
  }, [panel]);
  const [proofs, setProofs] = useState<Proof[]>([]);
  const [draft, setDraft] = useState("");
  const proofCache = useRef(new Map<string, Proof>());
  const scroller = useRef<HTMLDivElement>(null);

  const refreshSessions = useCallback(async () => {
    const list = await api.listSessions();
    list.sort((a, b) => (b.lastUpdateTime ?? 0) - (a.lastUpdateTime ?? 0));
    setSessions(list);
    return list;
  }, []);

  const refreshJobs = useCallback(async () => {
    const list = await api.listJobs();
    setJobs(new Map(list.map((j) => [j.session_id, j])));
    return list;
  }, []);

  const open = useCallback(async (id: string) => {
    setCurrent(id);
    setError("");
    window.history.replaceState(null, "", `?session=${encodeURIComponent(id)}`);
    const s = await api.getSession(id);
    setEvents(s.events ?? []);
    setState(s.state);
  }, []);

  useEffect(() => {
    api.getMe().then((m) => setEmail(m.email)).catch(() => {});
    Promise.all([refreshSessions(), refreshJobs()])
      .then(([list]) => {
        const linked = new URLSearchParams(window.location.search).get("session");
        const target = list.find((s) => s.id === linked) ?? list[0];
        return target ? open(target.id) : undefined;
      })
      .catch((e) => setError(String(e.message ?? e)));
  }, [refreshSessions, refreshJobs, open]);

  // The agent runs on the server; while this request's job is active, follow its saved progress.
  const job = current ? jobs.get(current) : undefined;
  const working = api.isActive(job);
  const [helpers, setHelpers] = useState<api.Task[]>([]);
  const helpersActive = helpers.some(api.taskActive);
  useEffect(() => {
    if (!current) return setHelpers([]);
    let stop = false;
    const load = () =>
      api
        .listTasks(current)
        .then((t) => !stop && setHelpers(t))
        .catch(() => {});
    load();
    if (!working && !helpersActive) return () => void (stop = true);
    const timer = window.setInterval(load, 5000);
    return () => {
      stop = true;
      window.clearInterval(timer);
    };
  }, [current, working, helpersActive]);
  useEffect(() => {
    if (!current || !job || !api.isActive(job)) return;
    let stop = false;
    const tick = async () => {
      try {
        const [s, j] = await Promise.all([api.getSession(current), api.getJob(job.id)]);
        if (stop) return;
        setEvents(s.events ?? []);
        setState(s.state);
        setJobs((m) => new Map(m).set(j.session_id, j));
        if (!api.isActive(j)) refreshSessions();
      } catch {
        /* transient; the next tick retries */
      }
    };
    const timer = window.setInterval(tick, 2500);
    return () => {
      stop = true;
      window.clearInterval(timer);
    };
  }, [current, job?.id, job?.status, refreshSessions]); // eslint-disable-line react-hooks/exhaustive-deps

  // Keep the sidebar's status dots fresh for other requests still working.
  useEffect(() => {
    if (![...jobs.values()].some((j) => api.isActive(j) && j.session_id !== current)) return;
    const timer = window.setInterval(() => refreshJobs().catch(() => {}), 6000);
    return () => window.clearInterval(timer);
  }, [jobs, current, refreshJobs]);

  useEffect(() => {
    if (!working) refreshBilling();
  }, [working, panel, refreshBilling]);

  // Back from Dodo checkout: the plan changes when its webhook lands, usually within seconds.
  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    if (params.get("billing") !== "done") return;
    params.delete("billing");
    window.history.replaceState(null, "", params.toString() ? `?${params}` : window.location.pathname);
    setPaid(true);
    setPanel("plan");
    let n = 0;
    const timer = window.setInterval(() => {
      refreshBilling();
      if (++n >= 20) window.clearInterval(timer);
    }, 3000);
    return () => window.clearInterval(timer);
  }, [refreshBilling]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && setPanel(null);
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  useEffect(() => {
    scroller.current?.scrollTo({ top: scroller.current.scrollHeight });
  }, [events.length, working]);

  // Fetch delivered files for the proof wall whenever a new artifact version appears.
  const wanted = useMemo(() => proofsFrom(events), [events]);
  useEffect(() => {
    if (!current) return;
    let live = true;
    Promise.all(
      [...wanted.entries()].map(async ([name, version]) => {
        const key = `${current}/${name}@${version}`;
        const hit = proofCache.current.get(key);
        if (hit) return hit;
        try {
          const { url, mime } = await api.getArtifact(current, name);
          const p: Proof = { name, version, url, mime };
          proofCache.current.set(key, p);
          return p;
        } catch (e) {
          return { name, version, error: String((e as Error).message) };
        }
      }),
    ).then((list) => live && setProofs(list.reverse()));
    return () => {
      live = false;
    };
  }, [wanted, current]);

  function startNew() {
    window.history.replaceState(null, "", window.location.pathname);
    setCurrent(null);
    setEvents([]);
    setState(undefined);
    setProofs([]);
    setError("");
  }

  async function removeRequest(id: string) {
    setDeleting(true);
    try {
      await api.deleteRequest(id);
      for (const key of [...proofCache.current.keys()]) if (key.startsWith(`${id}/`)) proofCache.current.delete(key);
      if (id === current) startNew();
      setConfirmDelete(null);
      await Promise.all([refreshSessions(), refreshJobs()]);
    } catch (e) {
      setError(`Could not delete: ${(e as Error).message}`);
    } finally {
      setDeleting(false);
    }
  }

  async function send(text: string, files: File[], fresh = false) {
    const target = fresh ? null : current;
    setError("");
    setOutOfCredits(false);
    const parts: Part[] = [...(text ? [{ text }] : []), ...(await Promise.all(files.map(api.fileToPart)))];
    setEvents((prev) => [...prev, { id: `local-${Date.now()}`, author: "user", content: { role: "user", parts } }]);
    try {
      const { job: started, session_id } = await api.startJob(target, parts);
      setJobs((m) => new Map(m).set(session_id, started));
      if (session_id !== target) {
        setCurrent(session_id);
        window.history.replaceState(null, "", `?session=${encodeURIComponent(session_id)}`);
      }
      refreshSessions();
    } catch (e) {
      setOutOfCredits(api.isOutOfCredits(e));
      setError(api.isOutOfCredits(e) ? (e as Error).message : `Could not start: ${(e as Error).message}`);
      if (target) api.getSession(target).then((s) => setEvents(s.events ?? [])).catch(() => {});
      else setEvents([]);
    }
  }

  const turns = toTurns(events, working);
  const leadCost = costFrom(events, state);
  const helperCost = helpers.reduce((sum, h) => sum + (h.cost_usd ?? 0), 0);
  const cost = leadCost === null && helperCost === 0 ? null : (leadCost ?? 0) + helperCost;
  const firstAsk = turns.find((t) => t.kind === "user")?.text;
  const title = tidyTitle(
    String((state?.title as string) || sessions.find((s) => s.id === current)?.state?.title || firstAsk || "New request"),
  );

  return (
    <div className="shell">
      <aside className="rail">
        <div className="brandmark">Design Content Studio</div>
        <button className="new" onClick={startNew}>
          <span aria-hidden="true">+</span> New request
        </button>
        <nav aria-label="Requests" className="threads">
          {sessions.length === 0 && <p className="rail-empty">Your requests will be listed here.</p>}
          {sessions.map((s) =>
            confirmDelete === s.id && s.id !== current ? (
              <div key={s.id} className="thread confirm" role="group" aria-label="Confirm delete">
                <span className="thread-title">Delete this request and its files?</span>
                <span className="confirm-actions">
                  <button className="link danger" disabled={deleting} onClick={() => removeRequest(s.id)}>
                    {deleting ? "Deleting…" : "Delete"}
                  </button>
                  <button className="link" disabled={deleting} onClick={() => setConfirmDelete(null)}>
                    Cancel
                  </button>
                </span>
              </div>
            ) : (
              <div key={s.id} className="thread-row">
                <button
                  className={s.id === current ? "thread active" : "thread"}
                  aria-current={s.id === current ? "page" : undefined}
                  onClick={() => open(s.id)}
                >
                  <span className="thread-title">
                    {tidyTitle((s.state?.title as string) || (s.id === current && firstAsk) || "Untitled request")}
                  </span>
                  <span className="thread-time">
                    {api.isActive(jobs.get(s.id)) ? (
                      <span className="thread-live">
                        <span className="pulse" aria-hidden="true" /> Working
                      </span>
                    ) : jobs.get(s.id)?.status === "failed" ? (
                      <span className="thread-failed">Stopped</span>
                    ) : (
                      ago(s.lastUpdateTime)
                    )}
                  </span>
                </button>
                <button
                  className="thread-delete"
                  aria-label={`Delete “${tidyTitle((s.state?.title as string) || "Untitled request")}”`}
                  title="Delete"
                  onClick={() => setConfirmDelete(s.id)}
                >
                  <svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true">
                    <path d="M3 4.5h10M6.5 4.5V3h3v1.5M4.5 4.5l.6 8.5h5.8l.6-8.5" fill="none" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round" strokeLinejoin="round" />
                  </svg>
                </button>
              </div>
            ),
          )}
        </nav>
        <div className="account">
          <div className="account-links">
            <button className="link" onClick={() => setPanel("brand")}>
              Brand
            </button>
            <button className="link" onClick={() => setPanel("plan")}>
              {billing ? planLabel(billing) : "Plan"}
            </button>
          </div>
          <div className="me">
            <span className="avatar" aria-hidden="true">
              {initials(who)}
            </span>
            <span className="me-name">{who}</span>
            {onSignOut && (
              <button className="link" onClick={() => onSignOut()}>
                Sign out
              </button>
            )}
          </div>
        </div>
      </aside>

      <main className="conversation">
        <header className="conv-head">
          <h1 title={title}>{title}</h1>
          {cost !== null && billing && (
            <span className="cost">{billing.owner ? `${money(cost)} so far` : `about ${credits(cost * billing.credits_per_usd)} so far`}</span>
          )}
          {current && confirmDelete !== current && (
            <button className="link head-delete" onClick={() => setConfirmDelete(current)}>
              Delete
            </button>
          )}
        </header>
        {current && confirmDelete === current && (
          <div className="confirm-bar" role="alert">
            <span>
              Delete this request? Its files and any background work go with it. This can't be undone.
            </span>
            <button className="primary danger-btn" disabled={deleting} onClick={() => removeRequest(current)}>
              {deleting ? "Deleting…" : "Delete request"}
            </button>
            <button className="link" disabled={deleting} onClick={() => setConfirmDelete(null)}>
              Cancel
            </button>
          </div>
        )}
        <div className="scroll" ref={scroller}>
          {turns.length === 0 ? (
            <div className="empty">
              <h2>What should we make?</h2>
              <p>
                Posts and ad sets in every size, charts and diagrams, one-pagers, GIFs and short films with voiceover.
                Say where it will run and any words it must carry; attach a draft, a photo or a clip to work from.
              </p>
              <div className="examples">
                {EXAMPLES.map((ex) => (
                  <button key={ex} className="example" onClick={() => setDraft(ex)}>
                    {ex}
                  </button>
                ))}
              </div>
              {sampleBrand && (
                <p className="brand-nudge">
                  You're using the sample brand.{" "}
                  <button className="link" onClick={() => setPanel("brand")}>
                    Set up yours from your website
                  </button>
                </p>
              )}
            </div>
          ) : (
            turns.map((t) =>
              t.kind === "agent" && t.text.trim() === api.QUIET && !t.working ? null : t.kind === "user" && api.isNotice(t.text) ? (
                <ul key={t.id} className="events" aria-label="Background updates">
                  {api.readNotice(t.text).map((e, i) => (
                    <li key={i} className={`event ${e.tone}`}>
                      <Glyph tone={e.tone} />
                      <span className="event-title">{e.title}</span>
                      {e.detail && <span className="event-detail">{e.detail}</span>}
                    </li>
                  ))}
                </ul>
              ) : t.kind === "user" ? (
                <div key={t.id} className="msg user">
                  {t.text && <p>{t.text}</p>}
                  {t.files.length > 0 && (
                    <div className="chips">
                      {t.files.map((f) => (
                        <span key={f} className="chip">
                          {f}
                        </span>
                      ))}
                    </div>
                  )}
                </div>
              ) : (
                <div key={t.id} className="msg agent">
                  {(t.steps.length > 0 || t.working) && <WorkLog steps={t.steps} working={t.working} end={t.end} />}
                  {t.text && <Prose text={t.text} />}
                  {t.error && <p className="error">The request stopped: {t.error}</p>}
                </div>
              ),
            )
          )}
          {helpers.length > 0 && (
            <section className="helpers" aria-label="Background work">
              <h3>
                {helpersActive ? "Working in the background" : "Background work"}
                <span className="helpers-count">
                  {helpers.filter((h) => !api.taskActive(h)).length} of {helpers.length} finished
                </span>
              </h3>
              <ul>
                {helpers.map((h) => {
                  const { kind, name } = api.friendlyTask(h.label, h.effort);
                  const status = api.helperStatus(h);
                  const fraction = api.helperFraction(h);
                  return (
                    <li key={h.id} className={`helper ${status.tone}`}>
                      <Glyph tone={status.tone} />
                      <span className="helper-name">
                        <strong>{kind}</strong>
                        {name && <span title={name}>{name}</span>}
                      </span>
                      <span className="helper-status" title={status.text}>
                        {status.text}
                      </span>
                      {fraction !== null && (
                        <span className="helper-bar" role="progressbar" aria-valuenow={Math.round(fraction * 100)} aria-valuemin={0} aria-valuemax={100}>
                          <span style={{ width: `${fraction * 100}%` }} />
                        </span>
                      )}
                    </li>
                  );
                })}
              </ul>
            </section>
          )}
          {working && (
            <p className="background-note" role="status">
              Working in the background. You can close this tab
              {email ? `; we'll email ${email} when it's done.` : " and come back later."}
            </p>
          )}
          {job?.status === "failed" && !working && (
            <p className="error banner" role="alert">
              This request stopped before it finished. Send a short note like “continue” to pick it up again.
            </p>
          )}
          {error && (
            <p className="error banner" role="alert">
              {error}
              {outOfCredits && (
                <button className="link" onClick={() => setPanel("plan")}>
                  See plans
                </button>
              )}
            </p>
          )}
        </div>
        <Composer disabled={working} onSend={send} text={draft} setText={setDraft} />
      </main>

      <section className="wall" aria-label="Delivered files">
        {proofs.length === 0 ? (
          <p className="wall-empty">Finished files land here at their real proportions: images, PDFs, GIFs and video.</p>
        ) : (
          <>
            <p className="wall-count">
              {proofs.length} {proofs.length === 1 ? "file" : "files"} delivered
            </p>
            {proofs.map((p) => (
              <ProofCard key={`${p.name}@${p.version}`} proof={p} />
            ))}
          </>
        )}
      </section>

      {panel && (
        <div className="scrim" onClick={() => setPanel(null)}>
          <div
            className="panel"
            role="dialog"
            aria-modal="true"
            aria-label={panel === "brand" ? "Brand" : "Plan and credits"}
            onClick={(e) => e.stopPropagation()}
          >
            <button className="link close" onClick={() => setPanel(null)}>
              Close
            </button>
            {panel === "brand" ? (
              <BrandPanel
                onFromWebsite={(url) => {
                  setPanel(null);
                  startNew();
                  send(`Set up our brand from our website: ${url}`, [], true);
                }}
              />
            ) : (
              <PlanPanel billing={billing} pricing={pricing} paid={paid} />
            )}
          </div>
        </div>
      )}
    </div>
  );
}

/** Titles saved before word-boundary truncation end mid-word at 80 characters: end them on a word. */
function tidyTitle(title: string): string {
  if (title.length < 80 || title.endsWith("…")) return title;
  return title.slice(0, title.lastIndexOf(" ")).replace(/[\s,.;:-]+$/, "") + "…";
}

function Glyph({ tone }: { tone: api.Tone }) {
  const mark = { done: "✓", stopped: "✕", attention: "!", info: "·", live: "" }[tone];
  return (
    <span className={`glyph ${tone}`} aria-hidden="true">
      {mark}
    </span>
  );
}

/** Files travel inline with the request; Cloud Run caps a request at 32 MB, base64 adds a third. */
const MAX_ATTACH_BYTES = 20 * 1024 * 1024;

function Composer({
  disabled,
  onSend,
  text,
  setText,
}: {
  disabled: boolean;
  onSend: (t: string, f: File[]) => void;
  text: string;
  setText: (t: string) => void;
}) {
  const [files, setFiles] = useState<File[]>([]);
  const [tooBig, setTooBig] = useState(false);
  const fileInput = useRef<HTMLInputElement>(null);
  const area = useRef<HTMLTextAreaElement>(null);
  useEffect(() => {
    if (text) area.current?.focus();
  }, [text]);
  const ready = !disabled && !tooBig && (text.trim() || files.length > 0);
  const submit = () => {
    if (!ready) return;
    onSend(text.trim(), files);
    setText("");
    setFiles([]);
  };
  return (
    <form
      className="composer"
      onSubmit={(e) => {
        e.preventDefault();
        submit();
      }}
    >
      <label htmlFor="brief" className="sr-only">
        Brief
      </label>
      <textarea
        id="brief"
        ref={area}
        rows={2}
        value={text}
        placeholder={disabled ? "Design Content Studio is working. You can draft your next note." : "Describe what you need…"}
        onChange={(e) => setText(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter" && !e.shiftKey) {
            e.preventDefault();
            submit();
          }
        }}
      />
      {files.length > 0 && (
        <div className="chips">
          {files.map((f, i) => (
            <span key={f.name + i} className="chip">
              {f.name}
              <span className="chip-size">{(f.size / 1024 / 1024).toFixed(1)} MB</span>
              <button
                type="button"
                className="chip-x"
                aria-label={`Remove ${f.name}`}
                onClick={() => {
                  const next = files.filter((_, j) => j !== i);
                  setFiles(next);
                  setTooBig(next.reduce((n, x) => n + x.size, 0) > MAX_ATTACH_BYTES);
                }}
              >
                ×
              </button>
            </span>
          ))}
        </div>
      )}
      {tooBig && (
        <p className="error attach-error">
          Attachments add up to more than 20 MB. Remove one, or share a link to it in your message instead.
        </p>
      )}
      <div className="composer-bar">
        <button type="button" className="link" onClick={() => fileInput.current?.click()}>
          Attach file
        </button>
        <input
          ref={fileInput}
          type="file"
          multiple
          accept="image/*,video/*,audio/*,application/pdf"
          hidden
          onChange={(e) => {
            const next = [...files, ...(e.target.files ?? [])].slice(0, 5);
            const total = next.reduce((n, f) => n + f.size, 0);
            setTooBig(total > MAX_ATTACH_BYTES);
            setFiles(next);
            e.target.value = "";
          }}
        />
        <span className="hint">Enter sends, Shift+Enter adds a line</span>
        <button className="primary" type="submit" disabled={!ready}>
          {disabled ? "Working…" : "Send"}
        </button>
      </div>
    </form>
  );
}

function ProofCard({ proof }: { proof: Proof }) {
  const [size, setSize] = useState("");
  const isImage = proof.mime?.startsWith("image/");
  const isPdf = proof.mime === "application/pdf";
  const isVideo = proof.mime?.startsWith("video/");
  return (
    <figure className="proof">
      <div className="crop">
        {proof.error ? (
          <p className="proof-error">Could not load {proof.name}. Reopen the request to try again.</p>
        ) : isImage && proof.url ? (
          <a href={proof.url} target="_blank" rel="noreferrer" aria-label={`Open ${proof.name} full size`}>
            <img
              src={proof.url}
              alt={proof.name}
              onLoad={(e) => setSize(`${e.currentTarget.naturalWidth} × ${e.currentTarget.naturalHeight}`)}
            />
          </a>
        ) : isVideo && proof.url ? (
          <video
            src={proof.url}
            controls
            playsInline
            preload="metadata"
            aria-label={proof.name}
            onLoadedMetadata={(e) =>
              setSize(`${e.currentTarget.videoWidth} × ${e.currentTarget.videoHeight} · ${Math.round(e.currentTarget.duration)} s`)
            }
          />
        ) : isPdf && proof.url ? (
          <object className="pdf" data={proof.url} type="application/pdf" aria-label={proof.name}>
            <p className="file">{proof.name}</p>
          </object>
        ) : (
          <p className="file">{proof.name}</p>
        )}
      </div>
      <figcaption>
        <span className="proof-size">{size || (isPdf ? "PDF" : "—")}</span>
        <span className="proof-name">{proof.name}</span>
        {proof.url && (
          <a className="download" href={proof.url} download={proof.name}>
            Download
          </a>
        )}
      </figcaption>
    </figure>
  );
}

type BrandSummary = {
  name: string;
  description: string;
  is_sample: boolean;
  colors: string[];
  fonts: string[];
  formats: { slug: string; width: number; height: number; label?: string }[];
};

type BrandDraft = Partial<BrandSummary> & { logos?: string[]; questions?: string; error?: string; problems?: string[] };

function BrandPanel({ onFromWebsite }: { onFromWebsite: (url: string) => void }) {
  const [brand, setBrand] = useState<BrandSummary | null>(null);
  const [draft, setDraft] = useState<BrandDraft | null>(null);
  const [site, setSite] = useState("");
  const [problems, setProblems] = useState<string[]>([]);
  const [status, setStatus] = useState("");
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    api.getBrand().then(setBrand).catch((e) => setStatus(e.message));
    api.getBrandDraft().then((d: BrandDraft) => setDraft(d && (d.name || d.error) ? d : null)).catch(() => {});
  }, []);
  async function decide(activate: boolean) {
    setBusy(true);
    setProblems([]);
    try {
      if (activate) {
        setBrand(await api.activateBrandDraft());
        setStatus("Brand activated. New requests use it.");
      } else {
        await api.discardBrandDraft();
        setStatus("Draft discarded. Your brand is unchanged.");
      }
      setDraft(null);
    } catch (e) {
      setStatus((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  async function upload(file: File) {
    setBusy(true);
    setStatus("Checking the pack…");
    setProblems([]);
    try {
      setBrand(await api.putBrand(file));
      setStatus("Brand updated. New requests use it.");
    } catch (e) {
      const msg = (e as Error).message;
      try {
        const detail = JSON.parse(msg);
        setProblems(detail.problems ?? []);
        setStatus(detail.error ?? "The pack was not accepted.");
      } catch {
        setStatus(msg);
      }
    } finally {
      setBusy(false);
    }
  }
  return (
    <div className="panel-body">
      <h2>Brand</h2>
      {draft && (
        <section className="draft" aria-label="Draft brand pack">
          <h3>Draft to review{draft.name ? `: ${draft.name}` : ""}</h3>
          {draft.error ? (
            <p>{draft.error}</p>
          ) : (
            <>
              <div className="swatches">
                {(draft.colors ?? []).map((c) => (
                  <div key={c} className="swatch">
                    <span style={{ background: c }} />
                    <code>{c}</code>
                  </div>
                ))}
              </div>
              <p>Typefaces: {(draft.fonts ?? []).join(", ") || "none"}</p>
              <p className="muted">{(draft.logos ?? []).length} logo file(s). The sample card is in the request that built it.</p>
              {draft.questions && (
                <div className="questions">
                  <Prose text={draft.questions} />
                </div>
              )}
            </>
          )}
          <div className="draft-actions">
            {!draft.error && (
              <button className="primary" disabled={busy} onClick={() => decide(true)}>
                Activate draft
              </button>
            )}
            <button className="link" disabled={busy} onClick={() => decide(false)}>
              Discard draft
            </button>
          </div>
        </section>
      )}
      {brand && (
        <>
          <p className="brand-name">
            {brand.name}
            {brand.is_sample && <span className="tag">Sample</span>}
          </p>
          <p className="muted">{brand.description}</p>
          <h3>Colours</h3>
          <div className="swatches">
            {brand.colors.map((c) => (
              <div key={c} className="swatch">
                <span style={{ background: c }} />
                <code>{c}</code>
              </div>
            ))}
          </div>
          <h3>Typefaces</h3>
          <p>{brand.fonts.join(", ")}</p>
          <h3>Formats</h3>
          <ul className="formats">
            {brand.formats.map((f) => (
              <li key={f.slug}>
                <span>{f.label || f.slug}</span>
                <span className="muted">
                  {f.width} × {f.height}
                </span>
              </li>
            ))}
          </ul>
        </>
      )}
      <form
        className="from-site"
        onSubmit={(e) => {
          e.preventDefault();
          if (site.trim()) onFromWebsite(site.trim());
        }}
      >
        <label htmlFor="brand-site">
          <strong>Build from your website</strong>
          <span className="muted">
            Design Content Studio reads your site's colours, type, logo and voice and drafts a pack for you to review here. It takes a few
            minutes; you'll get an email when it's ready.
          </span>
        </label>
        <div className="from-site-row">
          <input
            id="brand-site"
            type="url"
            required
            placeholder="https://yourcompany.com"
            value={site}
            onChange={(e) => setSite(e.target.value)}
          />
          <button className="primary" type="submit" disabled={busy}>
            Build draft
          </button>
        </div>
      </form>
      <label className={busy ? "dropzone busy" : "dropzone"}>
        <strong>{brand?.is_sample ? "Upload your brand pack" : "Replace the brand pack"}</strong>
        <span className="muted">A .zip with brand.toml, tokens.json, fonts.toml, voice.md, graphic-language.md, logos and fonts.</span>
        <input type="file" accept=".zip,application/zip" disabled={busy} onChange={(e) => e.target.files?.[0] && upload(e.target.files[0])} />
      </label>
      {status && <p role="status">{status}</p>}
      {problems.length > 0 && (
        <ul className="problems">
          {problems.map((p) => (
            <li key={p}>{p}</li>
          ))}
        </ul>
      )}
    </div>
  );
}

/** "about 1.4 credits": credits with one decimal, never below 0.1 when anything was used. */
function credits(n: number): string {
  const v = n > 0 && n < 0.1 ? 0.1 : Math.round(n * 10) / 10;
  return `${v} ${v === 1 ? "credit" : "credits"}`;
}

function planLabel(b: api.Billing): string {
  if (b.owner || b.balance === null) return "Pro";
  return `${b.plan === "pro" ? "Pro" : "Free"} · ${credits(Math.max(0, b.balance))}`;
}

const day = (iso: string) => new Date(iso).toLocaleDateString(undefined, { day: "numeric", month: "short" });

function PlanPanel({ billing, pricing, paid }: { billing: api.Billing | null; pricing: api.Pricing; paid: boolean }) {
  const [busy, setBusy] = useState<string | null>(null);
  const [err, setErr] = useState("");
  const go = async (what: api.BillingItem | "portal") => {
    setBusy(what);
    setErr("");
    try {
      window.location.href = what === "portal" ? await api.billingPortal() : await api.billingCheckout(what);
    } catch (e) {
      setErr((e as Error).message);
      setBusy(null);
    }
  };
  if (!billing)
    return (
      <div className="panel-body">
        <h2>Plan and credits</h2>
        <p className="muted">Loading…</p>
      </div>
    );
  const b = billing;
  const total = b.allowance + b.topup;
  const left = b.balance === null ? null : Math.max(0, b.balance);
  const until = b.subscription.access_until;
  const t = pricing.typical;
  return (
    <div className="panel-body plan">
      <h2>Plan and credits</h2>
      {paid && b.plan === "free" && <p className="note">Payment received. Your plan updates here within a minute.</p>}
      <p className="plan-name">
        {b.plan === "pro" ? "Pro" : "Free"}
        {b.owner && <span className="tag">Owner · unlimited</span>}
        {until && b.subscription.status === "cancelled" && <span className="tag">Ends {day(until)}</span>}
      </p>
      {left !== null && (
        <>
          <p className="figure">{credits(left)} left</p>
          <div className="meter" role="progressbar" aria-valuenow={left} aria-valuemin={0} aria-valuemax={total}>
            <span style={{ width: `${total ? Math.min(100, (left / total) * 100) : 0}%` }} />
          </div>
          <p className="muted">
            {b.plan === "free"
              ? `${b.allowance} free credits a month${b.period_end ? `, renewing ${day(b.period_end)}` : ""}.`
              : `${b.allowance} credits each billing period${b.topup ? `, plus ${credits(b.topup)} from top-ups that don't expire` : ""}.`}{" "}
            {pricing.credit_note}.
          </p>
        </>
      )}

      {!b.owner && b.plan === "free" && (
        <div className="offer">
          <h3>Pro</h3>
          <p>
            {pricing.pro.credits} credits a month and everything Design Content Studio makes: films with voiceover and music, motion,
            three-direction looks and background work.
          </p>
          <div className="offer-actions">
            <button className="primary" disabled={!!busy || !b.billing_ready} onClick={() => go("pro_monthly")}>
              {busy === "pro_monthly" ? "Opening checkout…" : `Upgrade · $${pricing.pro.monthly}/month`}
            </button>
            <button className="secondary" disabled={!!busy || !b.billing_ready} onClick={() => go("pro_annual")}>
              {busy === "pro_annual" ? "Opening checkout…" : `$${pricing.pro.annual}/year`}
            </button>
          </div>
          <p className="muted">Have a founding-member code? Enter it at checkout.</p>
        </div>
      )}
      {!b.owner && b.plan === "pro" && (
        <div className="offer-actions">
          <button className="primary" disabled={!!busy || !b.billing_ready} onClick={() => go("topup")}>
            {busy === "topup" ? "Opening checkout…" : `Buy ${pricing.topup.credits} credits · $${pricing.topup.price}`}
          </button>
          {b.can_manage && (
            <button className="secondary" disabled={!!busy} onClick={() => go("portal")}>
              {busy === "portal" ? "Opening…" : "Manage subscription"}
            </button>
          )}
        </div>
      )}
      {!b.billing_ready && !b.owner && <p className="muted">Payments open soon.</p>}
      {err && <p className="error">{err}</p>}

      <dl className="usage">
        <dt>A post, chart, PDF or diagram</dt>
        <dd>about {credits(t.post)}</dd>
        <dt>Brand from your website</dt>
        <dd>about {credits(t.brand)}</dd>
        <dt>Short motion piece</dt>
        <dd>about {credits(t.short_motion)}</dd>
        <dt>1-minute launch film</dt>
        <dd>about {credits(t.film_1min)}</dd>
        <dt>Three directions to pick from</dt>
        <dd>about {credits(t.looks)}</dd>
      </dl>
      <p className="note">Credits follow what each request actually takes, so a simple post can use less.</p>
    </div>
  );
}
