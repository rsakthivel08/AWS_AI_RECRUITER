import { useEffect, useLayoutEffect, useRef, useState } from "react";
import {
  fetchAuthSession,
  signIn,
  signOut,
} from "aws-amplify/auth";
import "./App.css";

/* ---------- shared types ---------- */
type Page = "dashboard" | "match" | "chat" | "candidates" | "jobs" | "search" | "compare" | "upload";
type Candidate = Record<string, any>;
type Job = Record<string, any>;
type Analysis = Record<string, any>;

const scoreBand = (pct: number): { label: string; chip: string; emoji: string } => {
  if (pct >= 75) return { label: "Strong", chip: "chip-good", emoji: "🟢" };
  if (pct >= 55) return { label: "Good", chip: "chip", emoji: "🔵" };
  if (pct >= 35) return { label: "Partial", chip: "chip-warn", emoji: "🟡" };
  return { label: "Weak", chip: "chip-bad", emoji: "🔴" };
};

/* ---------- API helper ---------- */
const API_BASE = (import.meta.env.VITE_API_URL ?? "").replace(/\/$/, "");

export const api = async <T,>(path: string, init?: RequestInit): Promise<{ data: T; status: number }> => {
  const session = await fetchAuthSession();
  const token = session.tokens?.idToken?.toString() ?? "";
  const res = await fetch(`${API_BASE}${path}`, {
    ...init,
    headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}`, ...(init?.headers ?? {}) },
  });
  if (res.status === 202) return { data: { accepted: true } as T, status: 202 };
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.message ?? `${res.status} ${res.statusText}`);
  }
  return { data: await res.json(), status: res.status };
};

const errText = (e: unknown) => (e instanceof Error ? e.message : String(e));

/* ---------- Gemini helpers (shared by quick match, Compare and Search) ---------- */
type QuickMatchResult = { score: number; explanation: string; model: string };

const GEMINI_API_KEY = import.meta.env.VITE_GEMINI_API_KEY ?? "";
const GEMINI_MODEL = import.meta.env.VITE_GEMINI_MODEL ?? "gemini-2.5-flash-lite";

/** Low-level Gemini text->JSON call. system+user blocks; JSON response mime. Throws with clear messages. */
const geminiJson = async (system: string, user: string, maxTokens = 256): Promise<string> => {
  if (!GEMINI_API_KEY) throw new Error("Gemini API key not configured — set VITE_GEMINI_API_KEY.");
  let res: Response;
  try {
    res = await fetch(
      `https://generativelanguage.googleapis.com/v1beta/models/${GEMINI_MODEL}:generateContent`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json", "x-goog-api-key": GEMINI_API_KEY },
        body: JSON.stringify({
          contents: [{ role: "user", parts: [{ text: `${system}\n\n${user}` }] }],
          generationConfig: { temperature: 0.2, maxOutputTokens: maxTokens, responseMimeType: "application/json" },
        }),
      },
    );
  } catch {
    throw new Error("Gemini API not reachable — check your network connection.");
  }
  if (res.status === 401 || res.status === 403) throw new Error("Gemini API key rejected — check VITE_GEMINI_API_KEY.");
  if (res.status === 404) throw new Error(`Gemini model ${GEMINI_MODEL} not found / not available with this key.`);
  if (!res.ok) throw new Error(`Gemini returned ${res.status} ${res.statusText}`);
  const body = await res.json();
  return body?.candidates?.[0]?.content?.parts?.[0]?.text ?? "";
};

/** Parse a JSON block out of a model reply, tolerating fences / leading prose. */
const parseModelJson = (text: string): any => {
  const raw = text.replace(/^```(?:json)?/m, "").replace(/```$/m, "").trim();
  const m = raw.match(/\{[\s\S]*\}|\[[\s\S]*\]/);
  let parsed: any;
  try {
    parsed = JSON.parse(m ? m[0] : raw);
  } catch {
    throw new Error(`Gemini returned non-JSON output: ${(m ? m[0] : raw).slice(0, 120) || "(empty)"}`);
  }
  return parsed;
};

/** Ask Gemini for a lightweight match score. Returns 0-100 + short explanation. */
const geminiMatch = async (candidate: Record<string, any>, job: Record<string, any>): Promise<QuickMatchResult> => {
  const reqs = job?.parsedRequirements ?? {};
  const profile = candidate?.profile ?? {};
  const jobSummary = [
    `Title: ${job.title}`,
    reqs.seniority && `Seniority: ${reqs.seniority}`,
    reqs.minYears && `Min years: ${reqs.minYears}`,
    reqs.requiredSkills?.length && `Required skills: ${(reqs.requiredSkills as string[]).join(", ")}`,
    reqs.mustHaves?.length && `Must-haves: ${(reqs.mustHaves as string[]).join(", ")}`,
    reqs.preferredSkills?.length && `Nice-to-have: ${(reqs.preferredSkills as string[]).join(", ")}`,
  ].filter(Boolean).join("\n");
  const candSummary = [
    profile.name && `Name: ${profile.name}`,
    profile.summary && `Summary: ${profile.summary}`,
    profile.yearsExperience != null && `Years experience: ${profile.yearsExperience}`,
    profile.skills?.length && `Skills: ${(profile.skills as string[]).join(", ")}`,
    profile.experience?.length && `Experience: ${(profile.experience as any[]).slice(0, 6).map((e: any) => `${e.title ?? e.jobTitle ?? "role"} @ ${e.company ?? "company"}`).join("; ")}`,
    profile.education?.length && `Education: ${(profile.education as any[]).slice(0, 3).map((e: any) => e.degree ?? e.field ?? "").filter(Boolean).join(", ")}`,
  ].filter(Boolean).join("\n");

  const system =
    "You are a recruiting assistant. Compare a candidate to a job and score the fit. " +
    "In top-reply JSON only: {\"score\": <0-100 integer>, \"explanation\": \"<2-3 sentences>\"}. " +
    "No markdown, no extra keys.";
  const user = `JOB:\n${jobSummary}\n\nCANDIDATE:\n${candSummary}\n\nScore the fit 0-100 and explain briefly in 2-3 sentences. Return JSON only.`;

  const text: string = await geminiJson(system, user);
  const parsed: any = parseModelJson(text);
  const score = Math.max(0, Math.min(100, Math.round(Number(parsed.score) || 0)));
  const explanation = String(parsed.explanation ?? "").trim() || "(no explanation returned)";
  return { score, explanation, model: GEMINI_MODEL };
};

/* ---------- status pill ---------- */
const Pill = ({ status }: { status: string }) => {
  const known: Record<string, string> = {
    PROCESSING: "processing", READY: "ready", PARSING: "parsing",
    MATCHING: "matching", MATCHED: "matched", ERROR: "error",
    DUPLICATE: "warn", FAILED: "error", UPLOADED: "neutral",
  };
  return <span className={`pill pill-${known[status] ?? "neutral"}`}>{status}</span>;
};

const Banner = ({ msg }: { msg: string }) =>
  msg ? <div className="banner"><span>⚠</span>{msg}</div> : null;

const Empty = ({ children }: { children: React.ReactNode }) =>
  <div className="empty">{children}</div>;

/* ============================================================ App root */
export default function App() {
  const [signedIn, setSignedIn] = useState(false);
  const [page, setPage] = useState<Page>("dashboard");
  const [dir, setDir] = useState<"fwd" | "back">("fwd");
  const navRefs = useRef<Partial<Record<Page, HTMLButtonElement | null>>>({});
  const [navInd, setNavInd] = useState<{ left: number; width: number } | null>(null);
  const [signingIn, setSigningIn] = useState(false);
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [authError, setAuthError] = useState("");

  const [activeCandidateId, setActiveCandidateId] = useState<string | null>(null);
  const [activeCandidate, setActiveCandidate] = useState<Candidate | null>(null);

  useEffect(() => {
    fetchAuthSession()
      .then((s: { tokens?: unknown }) => setSignedIn(Boolean(s.tokens)))
      .catch(() => setSignedIn(false));
  }, []);

  useEffect(() => {
    if (!activeCandidateId || !signedIn) {
      setActiveCandidate(null);
      return;
    }
    api<Candidate>(`/candidates/${activeCandidateId}`)
      .then(({ data }) => setActiveCandidate(data))
      .catch(() => {});
  }, [activeCandidateId, signedIn]);

  // Direction-aware page transition + sliding nav indicator
  const goTo = (next: Page) => {
    if (next === page) return;
    const order: Page[] = ["dashboard", "match", "chat", "candidates", "jobs", "search", "compare", "upload"];
    setDir(order.indexOf(next) > order.indexOf(page) ? "fwd" : "back");
    setPage(next);
  };

  useLayoutEffect(() => {
    if (!signedIn) { setNavInd(null); return; }
    const move = () => {
      const el = navRefs.current[page];
      if (el) setNavInd({ left: el.offsetLeft, width: el.offsetWidth });
    };
    move();
    window.addEventListener("resize", move);
    return () => window.removeEventListener("resize", move);
  }, [page, signedIn]);

  // Cursor-follow spotlight position for cards
  useEffect(() => {
    const h = (e: MouseEvent) => {
      const el = (e.target as HTMLElement | null)?.closest?.(".card") as HTMLElement | null;
      if (!el) return;
      const r = el.getBoundingClientRect();
      el.style.setProperty("--mx", `${e.clientX - r.left}px`);
      el.style.setProperty("--my", `${e.clientY - r.top}px`);
    };
    document.addEventListener("mousemove", h);
    return () => document.removeEventListener("mousemove", h);
  }, []);

  const doSignIn = async (e: React.FormEvent) => {
    e.preventDefault();
    setAuthError("");
    setSigningIn(true);
    try {
      await signIn({ username: email.trim(), password });
      // Re-fetch session to confirm tokens are present
      const s = await fetchAuthSession();
      setSignedIn(Boolean(s.tokens));
    } catch (err) {
      setAuthError(errText(err));
    }
    setSigningIn(false);
  };

  if (!signedIn) {
    return (
      <main className="app-shell">
        <div className="center">
          <form className="card signin-card" onSubmit={doSignIn} style={{ display: "flex", flexDirection: "column", gap: 12 }}>
            <h1 className="signin-title">AI Recruiter</h1>
            <p className="signin-sub">Score, gap-check and screen candidates against any job description.</p>
            {authError && <div className="banner"><span>⚠</span>{authError}</div>}
            <input
              className="input"
              type="email"
              placeholder="Email"
              value={email}
              autoComplete="username"
              required
              onChange={(e) => setEmail(e.target.value)}
            />
            <input
              className="input"
              type="password"
              placeholder="Password"
              value={password}
              autoComplete="current-password"
              required
              onChange={(e) => setPassword(e.target.value)}
            />
            <button className="btn btn-primary" type="submit" disabled={signingIn}>
              {signingIn ? <><span className="spinner" /> Signing in…</> : "Sign in"}
            </button>
          </form>
        </div>
      </main>
    );
  }


  const tabs: { key: Page; label: string }[] = [
    { key: "dashboard", label: "Dashboard" },
    { key: "match", label: "📄 Match" },
    { key: "chat", label: "💬 Ask Recruiter" },
    { key: "candidates", label: "Candidates" },
    { key: "jobs", label: "Jobs" },
    { key: "search", label: "Search" },
    { key: "compare", label: "⚖️ Compare" },
    { key: "upload", label: "Upload" },
  ];

  return (
    <div className="app-shell">
      <nav className="nav">
        <span className="nav-brand">AI Recruiter</span>
        {tabs.map(({ key, label }) => (
          <button
            key={key}
            ref={(el) => { navRefs.current[key] = el; }}
            className={`nav-tab${page === key ? " active" : ""}`}
            onClick={() => goTo(key)}
          >
            {label}
          </button>
        ))}
        <span className="nav-ind" style={navInd ? { left: navInd.left, width: navInd.width } : { display: "none" }} />
        <span className="nav-spacer" />
        {activeCandidate && (
          <div className="nav-active-cand">
            <span>👤 <b>{String(activeCandidate.profile?.name ?? activeCandidate.filename ?? activeCandidate.candidateId)}</b></span>
            <button
              className="btn btn-ghost small"
              style={{ padding: "1px 5px", minHeight: "auto", lineHeight: 1 }}
              title="Deselect active candidate"
              onClick={() => { setActiveCandidateId(null); setActiveCandidate(null); }}
            >
              ✕
            </button>
          </div>
        )}
        <button className="btn nav-signout" onClick={() => signOut().then(() => setSignedIn(false))}>
          Sign out
        </button>
      </nav>
      <main className={`page${dir === "back" ? " back" : ""}`} key={page}>
        {page === "dashboard" && (
          <Dashboard
            go={setPage}
            onSelectCandidate={(c) => {
              setActiveCandidateId(String(c.candidateId));
              setActiveCandidate(c);
            }}
          />
        )}
        {page === "match" && (
          <MatchScreen
            activeCandidate={activeCandidate}
            onSelectCandidate={(c) => {
              setActiveCandidateId(c ? String(c.candidateId) : null);
              setActiveCandidate(c);
            }}
            go={setPage}
          />
        )}
        {page === "chat" && (
          <AskRecruiter
            activeCandidate={activeCandidate}
            onSelectCandidate={(c) => {
              setActiveCandidateId(c ? String(c.candidateId) : null);
              setActiveCandidate(c);
            }}
            go={setPage}
          />
        )}
        {page === "candidates" && (
          <Candidates
            activeCandidateId={activeCandidateId}
            onSelectCandidate={(c) => {
              setActiveCandidateId(String(c.candidateId));
              setActiveCandidate(c);
            }}
            go={setPage}
          />
        )}
        {page === "jobs" && <Jobs />}
        {page === "search" && <Search />}
        {page === "compare" && <Compare />}
        {page === "upload" && <Upload />}
        <p className="disclaimer">AI-generated content — verify before use.</p>
      </main>
    </div>
  );
}

/* ============================================================ Dashboard */
const Dashboard = ({
  go,
  onSelectCandidate,
}: {
  go: (p: Page) => void;
  onSelectCandidate?: (c: Candidate) => void;
}) => {
  const [candidates, setCandidates] = useState<Candidate[]>([]);
  const [jobs, setJobs] = useState<Job[]>([]);
  const [error, setError] = useState("");

  const load = () => {
    setError("");
    api<{ candidates: Candidate[] }>("/candidates")
      .then(({ data }) => setCandidates(data.candidates ?? []))
      .catch((e) => setError(errText(e)));
    api<{ jobs: Job[] }>("/jobs")
      .then(({ data }) => setJobs(data.jobs ?? []))
      .catch((e) => setError(errText(e)));
  };
  useEffect(load, []);

  const ready = candidates.filter((c) => c.status === "READY").length;

  return (
    <>
      <h1>Dashboard</h1>
      <Banner msg={error} />
      <div className="stats" style={{ marginTop: 18 }}>
        <div className="card stat">
          <div className="stat-value">{candidates.length}</div>
          <div className="stat-label">Candidates</div>
        </div>
        <div className="card stat">
          <div className="stat-value">{ready}</div>
          <div className="stat-label">Processed & ready</div>
        </div>
        <div className="card stat">
          <div className="stat-value">{jobs.length}</div>
          <div className="stat-label">Jobs</div>
        </div>
      </div>
      <div className="card section" style={{ display: "flex", gap: 10, flexWrap: "wrap" }}>
        <button className="btn btn-primary" onClick={() => go("upload")}>+ Upload resumes</button>
        <button className="btn" onClick={() => go("match")}>📄 Match & Screen</button>
        <button className="btn" onClick={() => go("chat")}>💬 Ask Recruiter</button>
        <button className="btn" onClick={() => go("jobs")}>Jobs →</button>
        <button className="btn" onClick={load}>↻ Refresh</button>
      </div>
      {(candidates.length > 0 || jobs.length > 0) && (() => {
        const activity = [
          ...candidates.map((c) => ({ kind: "resume", label: String(c.filename ?? c.candidateId), status: String(c.status), ts: String(c.createdAt ?? ""), raw: c })),
          ...jobs.map((j) => ({ kind: "job", label: String(j.title ?? j.jobId), status: String(j.status), ts: String(j.createdAt ?? ""), raw: j })),
        ].sort((a, b) => b.ts.localeCompare(a.ts)).slice(0, 5);
        return (
          <div className="card" style={{ marginTop: 14 }}>
            <div className="section-head"><span className="section-title">Recent activity</span></div>
            {activity.map((a, i) => (
              <div key={i} className="row small" style={{ padding: "7px 0", borderBottom: "1px solid var(--border)" }}>
                <span className={`chip ${a.kind === "resume" ? "" : "chip-warn"}`} style={{ minWidth: 58, textAlign: "center" }}>{a.kind}</span>
                <span className="grow" style={{ color: "var(--text-hi)" }}>{a.label}</span>
                <Pill status={a.status} />
                {a.kind === "resume" && onSelectCandidate && (
                  <button
                    className="btn btn-ghost small"
                    style={{ padding: "2px 8px" }}
                    onClick={() => {
                      onSelectCandidate(a.raw);
                      go("match");
                    }}
                  >
                    Select 👤
                  </button>
                )}
              </div>
            ))}
          </div>
        );
      })()}
    </>
  );
};

/* ============================================================ Match & Screen */
const MatchScreen = ({
  activeCandidate,
  onSelectCandidate,
  go,
}: {
  activeCandidate: Candidate | null;
  onSelectCandidate: (c: Candidate | null) => void;
  go: (p: Page) => void;
}) => {
  const [candidates, setCandidates] = useState<Candidate[]>([]);
  const [jobs, setJobs] = useState<Job[]>([]);
  const [jobId, setJobId] = useState<string>("");
  const [showBrowser, setShowBrowser] = useState(false);
  const [error, setError] = useState("");
  const [topK, setTopK] = useState(3);
  const [matchBusy, setMatchBusy] = useState(false);
  const [matchResult, setMatchResult] = useState<{ totalScore: number; band: string; criteria: any[] } | null>(null);
  const [quickBusy, setQuickBusy] = useState(false);
  const [quickResult, setQuickResult] = useState<QuickMatchResult | null>(null);
  const [quickError, setQuickError] = useState("");
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const loadCandidates = () => {
    api<{ candidates: Candidate[] }>("/candidates")
      .then(({ data }) => setCandidates(data.candidates ?? []))
      .catch((e) => setError(errText(e)));
  };
  const loadJobs = () => {
    api<{ jobs: Job[] }>("/jobs")
      .then(({ data }) => setJobs(data.jobs ?? []))
      .catch((e) => setError(errText(e)));
  };

  useEffect(() => {
    loadCandidates();
    loadJobs();
    return () => {
      if (pollRef.current) clearInterval(pollRef.current);
    };
  }, []);

  const pollRanking = (jid: string, cid: string) => {
    if (pollRef.current) clearInterval(pollRef.current);
    let tries = 0;
    let errors = 0;
    const t: ReturnType<typeof setInterval> = setInterval(async () => {
      tries += 1;
      try {
        const { data } = await api<{ rankings: any[] }>(`/jobs/${jid}/rankings`);
        const row = (data.rankings ?? []).find((r: any) => String(r.candidateId) === cid);
        if (row) {
          // Rankings rows are summaries; the criterion breakdown lives in the analysis item.
          let criteria: any[] = [];
          try {
            const { data: a } = await api<Analysis>(`/jobs/${jid}/candidates/${cid}/analysis`);
            criteria = a?.criteria ?? [];
          } catch { /* breakdown is optional — score/band already received */ }
          clearInterval(t);
          setMatchBusy(false);
          setMatchResult({
            totalScore: row.totalScore ?? 0,
            band: row.band ?? "",
            criteria,
          });
        } else if (tries > 60) {
          clearInterval(t);
          setMatchBusy(false);
          setError("The match workflow is taking too long — check Rankings in a minute or run the match again.");
        }
      } catch {
        errors += 1;
        if (errors >= 4) {
          clearInterval(t);
          setMatchBusy(false);
        }
      }
    }, 4000);
    pollRef.current = t;
  };

  const runMatch = async () => {
    if (!activeCandidate) {
      setError("Please select a candidate to score.");
      return;
    }
    if (!jobId) {
      setError("Please pick a job to match against.");
      return;
    }
    const cid = String(activeCandidate.candidateId);
    setMatchBusy(true);
    setMatchResult(null);
    setError("");
    try {
      await api(`/jobs/${jobId}/match`, {
        method: "POST",
        body: JSON.stringify({ candidateIds: [cid] }),
      }); // 202 accepted — a match Map state runs in the workflow
      pollRanking(String(jobId), cid);
    } catch (e) {
      setError(errText(e));
      setMatchBusy(false);
    }
  };

  const roles: any[] = activeCandidate?.roles ?? activeCandidate?.roleSuggestions ?? [];
  const p = activeCandidate?.profile ?? {};
  const selectedJob = jobs.find((j) => String(j.jobId) === String(jobId));

  const runQuickMatch = async () => {
    if (!activeCandidate || !selectedJob) return;
    setQuickBusy(true);
    setQuickResult(null);
    setQuickError("");
    try {
      setQuickResult(await geminiMatch(activeCandidate, selectedJob));
    } catch (e) {
      setQuickError(errText(e));
    } finally {
      setQuickBusy(false);
    }
  };

  return (
    <>
      <div className="row">
        <h1 className="grow">Resume Matching</h1>
        <button className="btn" onClick={() => setShowBrowser(!showBrowser)}>
          👥 Candidate Browser ({candidates.length}) {showBrowser ? "▲" : "▼"}
        </button>
      </div>
      <Banner msg={error} />

      {/* Candidate Browser expander */}
      {showBrowser && (
        <div className="card" style={{ marginTop: 12, background: "var(--bg-2)" }}>
          <div className="section-head"><span className="section-title">Select an active candidate</span></div>
          {candidates.length === 0 ? (
            <Empty>No candidates in the database. Upload one in the Upload tab.</Empty>
          ) : (
            <table className="table">
              <thead><tr><th>ID</th><th>Name</th><th>File</th><th>Status</th><th /></tr></thead>
              <tbody>
                {candidates.map((c) => {
                  const isActive = activeCandidate?.candidateId === c.candidateId;
                  return (
                    <tr key={String(c.candidateId)}>
                      <td><span className="chip small">{String(c.candidateId).slice(0, 8)}</span></td>
                      <td style={{ fontWeight: 600, color: "var(--text-hi)" }}>{String(c.profile?.name ?? "(unextracted)")}</td>
                      <td className="small muted">{String(c.filename)}</td>
                      <td><Pill status={String(c.status)} /></td>
                      <td>
                        <button
                          className={`btn ${isActive ? "btn-primary" : "btn-ghost"} small`}
                          onClick={() => { onSelectCandidate(c); setShowBrowser(false); }}
                        >
                          {isActive ? "✅ Active" : "Select"}
                        </button>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          )}
        </div>
      )}

      {/* Active candidate card */}
      {!activeCandidate ? (
        <div className="card section" style={{ textAlign: "center", padding: "40px 20px" }}>
          <div style={{ fontSize: "2rem", marginBottom: 10 }}>👤</div>
          <h3>No active candidate selected</h3>
          <p className="muted">Select a candidate from the browser above or upload a new resume.</p>
          <div className="row" style={{ justifyContent: "center", marginTop: 16 }}>
            <button className="btn btn-primary" onClick={() => setShowBrowser(true)}>Open Candidate Browser</button>
            <button className="btn" onClick={() => go("upload")}>+ Upload new resume</button>
          </div>
        </div>
      ) : (
        <>
          <div className="card section">
            <div className="row">
              <span className="stat-value" style={{ fontSize: "1.3rem" }}>👤 {String(p.name ?? activeCandidate.filename)}</span>
              <Pill status={String(activeCandidate.status)} />
              <span className="muted small">ID: #{String(activeCandidate.candidateId)}</span>
              <span className="grow" />
              <button className="btn btn-ghost small" onClick={() => go("chat")}>💬 Ask Questions →</button>
              <button className="btn btn-ghost small" onClick={() => onSelectCandidate(null)}>Change</button>
            </div>

            {/* Profile tag badges */}
            <div className="stats" style={{ marginTop: 14 }}>
              <div className="card stat" style={{ padding: "12px 16px" }}>
                <div className="stat-label">Skills ({(p.skills ?? []).length})</div>
                <div style={{ marginTop: 6 }}>
                  {((p.skills as string[]) ?? []).length ? (
                    (p.skills as string[]).map((s) => <span key={s} className="chip tag-skill">{s}</span>)
                  ) : <span className="muted small">None</span>}
                </div>
              </div>
              <div className="card stat" style={{ padding: "12px 16px" }}>
                <div className="stat-label">Technologies ({(p.tools ?? []).length})</div>
                <div style={{ marginTop: 6 }}>
                  {((p.tools as string[]) ?? []).length ? (
                    (p.tools as string[]).map((t) => <span key={t} className="chip tag-tech">{t}</span>)
                  ) : <span className="muted small">None</span>}
                </div>
              </div>
              <div className="card stat" style={{ padding: "12px 16px" }}>
                <div className="stat-label">Languages ({(p.languages ?? []).length})</div>
                <div style={{ marginTop: 6 }}>
                  {((p.languages as string[]) ?? []).length ? (
                    (p.languages as string[]).map((l) => <span key={l} className="chip tag-lang">{l}</span>)
                  ) : <span className="muted small">None</span>}
                </div>
              </div>
            </div>
          </div>

          {/* Job matching: runs the real match workflow against a registered job */}
          <div className="card section" style={{ marginTop: 16 }}>
            <div className="section-head">
              <span className="section-title">📑 Match this candidate against a job</span>
              <span className="muted small" style={{ marginLeft: "auto" }}>⚠️ AI-generated</span>
            </div>

            {jobs.length === 0 ? (
              <Empty>
                No jobs yet —{" "}
                <button className="btn btn-ghost small" onClick={() => go("jobs")}>create one in the Jobs tab</button>
              </Empty>
            ) : (
              <>
                <div className="cmp-select cmp-job-select">
                  <label className="cmp-label">Job</label>
                  <select className="select grow" value={jobId} onChange={(e) => setJobId(e.target.value)}>
                    <option value="">Choose a job…</option>
                    {jobs.map((j) => (
                      <option key={String(j.jobId)} value={String(j.jobId)}>
                        {String(j.title ?? "(untitled)")}{j.status === "PARSING" ? " (parsing…)" : ""}
                      </option>
                    ))}
                  </select>
                </div>
                {selectedJob && (
                  <div className="row small muted" style={{ marginTop: 6, gap: 12, flexWrap: "wrap" }}>
                    <Pill status={String(selectedJob.status)} />
                    {selectedJob.parsedRequirements?.minYears != null &&
                      <span>{String(selectedJob.parsedRequirements.minYears)}+ yrs</span>}
                    {selectedJob.parsedRequirements?.seniority &&
                      <span>{String(selectedJob.parsedRequirements.seniority)}</span>}
                    {(selectedJob.parsedRequirements?.requiredSkills as string[] | undefined)?.map((s) =>
                      <span key={s} className="chip chip-good small">{s}</span>)}
                  </div>
                )}
                <div className="row" style={{ marginTop: 12 }}>
                  <button className="btn btn-primary" onClick={runMatch}
                          disabled={matchBusy || !jobId || !activeCandidate || selectedJob?.status === "PARSING"}>
                    {matchBusy ? <><span className="spinner" /> Scoring…</> : "Score this candidate"}
                  </button>
                  <button className="btn" onClick={runQuickMatch}
                          disabled={quickBusy || !jobId || !activeCandidate || selectedJob?.status === "PARSING"}
                          title={`Quick match via Gemini (${GEMINI_MODEL}) — instant, no backend workflow`}>
                    {quickBusy ? <><span className="spinner" /> Asking Gemini…</> : <>⚡ Quick match (Gemini)</>}
                  </button>
                  {!jobId && <span className="small muted">Select a job to match against.</span>}
                </div>

                {matchBusy && (
                  <div className="row small muted" style={{ marginTop: 10 }}>
                    <span className="spinner" /> The match workflow is scoring this candidate — a result appears here in ~30–60 s.
                  </div>
                )}

                {quickError && <div className="quote small" style={{ marginTop: 10, color: "var(--danger)" }}>⚠️ {quickError}</div>}
                {quickResult && (
                  <div className="card section" style={{ background: "var(--bg-2)", marginTop: 14 }}>
                    <div className="section-head">
                      <span className="section-title">⚡ Quick match — {quickResult.model}</span>
                      <span className="muted small" style={{ marginLeft: "auto" }}>via Gemini, no backend workflow</span>
                    </div>
                    <div className="row" style={{ alignItems: "baseline", gap: 14 }}>
                      <span className="stat-value" style={{ fontSize: "2.5rem" }}>{quickResult.score}%</span>
                      <span className={`chip ${(scoreBand(quickResult.score).chip)} small`}
                            style={{ fontSize: "1rem", padding: "4px 12px" }}>
                        {scoreBand(quickResult.score).emoji} {scoreBand(quickResult.score).label}
                      </span>
                    </div>
                    <div className="scorebar scorebar-track" style={{ marginTop: 8, height: 10 }}>
                      <div className="scorebar-fill"
                           style={{ width: `${quickResult.score}%` }} />
                    </div>
                    <p className="quote" style={{ marginTop: 10 }}>{quickResult.explanation}</p>
                  </div>
                )}

                {matchResult && (
                  <div className="card section" style={{ background: "var(--bg-2)", marginTop: 14 }}>
                    <div className="section-head"><span className="section-title">Match result</span></div>
                    <div className="row" style={{ alignItems: "baseline", gap: 14 }}>
                      <span className="stat-value" style={{ fontSize: "2.5rem" }}>{Math.round(Number(matchResult.totalScore) || 0)}%</span>
                      <span className="chip" style={{ fontSize: "1rem", padding: "4px 12px" }}>{String(matchResult.band ?? "")}</span>
                    </div>
                    {matchResult.criteria?.length ? (
                      <div style={{ marginTop: 10 }}>
                        <div className="section-title" style={{ marginBottom: 6 }}>Breakdown</div>
                        {matchResult.criteria.map((c, i) => {
                          const s = Math.round(Number(c.score) || 0);
                          return (
                            <div key={i} style={{ margin: "8px 0" }}>
                              <div className="row small">
                                <span style={{ fontWeight: 600, minWidth: 170 }}>{String(c.name ?? c.criterion)}</span>
                                <span className={`chip ${s >= 75 ? "chip-good" : s >= 50 ? "chip-warn" : "chip-bad"} small`}>{s}</span>
                              </div>
                              <div className="scorebar scorebar-track" style={{ marginTop: 3 }}>
                                <div className="scorebar-fill" style={{ width: `${s}%` }} />
                              </div>
                              {c.rationale && <p className="quote small" style={{ marginTop: 4 }}>{String(c.rationale)}</p>}
                              {Array.isArray(c.evidence) && c.evidence.slice(0, 2).map((ev: any, k: number) => (
                                <div className="quote small" key={k}>“{String(ev?.quote ?? ev)}”</div>
                              ))}
                            </div>
                          );
                        })}
                      </div>
                    ) : null}
                  </div>
                )}
              </>
            )}
          </div>

          {/* Suggested Roles — kept alongside */}
          <div className="card" style={{ marginTop: 16 }}>
            <div className="section-head">
              <span className="section-title">🎯 Suggested Roles</span>
              <span className="muted small" style={{ marginLeft: "auto" }}>⚠️ AI-generated</span>
            </div>
            <div style={{ margin: "8px 0 14px" }}>
              <div className="row small" style={{ marginBottom: 4 }}>
                <span className="muted">How many roles?</span>
                <span className="score-num">{topK}</span>
              </div>
              <input
                type="range"
                min={1}
                max={10}
                value={topK}
                style={{ width: "100%", accentColor: "var(--accent)" }}
                onChange={(e) => setTopK(Number(e.target.value))}
              />
            </div>

            {roles.length === 0 ? (
              <Empty>No role suggestions stored yet for this candidate.</Empty>
            ) : (
              roles.slice(0, topK).map((r: any, i: number) => {
                const s = Math.round(Number(r.score) || 0);
                const b = scoreBand(s);
                return (
                  <div key={i} style={{ margin: "10px 0" }}>
                    <div className="row small">
                      <span style={{ fontWeight: 700, color: "var(--text-hi)" }}>{String(r.role)}</span>
                      <span className={`chip ${b.chip} small`} style={{ marginLeft: "auto" }}>{b.emoji} {b.label} ({s}%)</span>
                    </div>
                    <div className="scorebar" style={{ marginTop: 4 }}>
                      <div className="scorebar-track" style={{ flex: 1 }}>
                        <div className="scorebar-fill" style={{ width: `${s}%` }} />
                      </div>
                    </div>
                    {r.rationale && <p className="quote small" style={{ marginTop: 4 }}>{String(r.rationale)}</p>}
                  </div>
                );
              })
            )}
          </div>
        </>
      )}
    </>
  );
};

/* ============================================================ Ask Recruiter */
const AskRecruiter = ({
  activeCandidate,
  onSelectCandidate,
  go,
}: {
  activeCandidate: Candidate | null;
  onSelectCandidate: (c: Candidate | null) => void;
  go: (p: Page) => void;
}) => {
  const [candidates, setCandidates] = useState<Candidate[]>([]);
  const [messages, setMessages] = useState<{ role: "user" | "assistant"; text: string; citations?: any[] }[]>([
    {
      role: "assistant",
      text: "Hello! I am your AI Recruiter assistant. Ask me anything about this candidate's fit, experience, technical skills, or project work.",
    },
  ]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    api<{ candidates: Candidate[] }>("/candidates")
      .then(({ data }) => setCandidates(data.candidates ?? []))
      .catch(() => {});
  }, []);

  const send = async () => {
    const q = input.trim();
    if (!q || busy) return;
    if (!activeCandidate) {
      setError("Please select a candidate first.");
      return;
    }
    setBusy(true);
    setInput("");
    setError("");
    const next = [...messages, { role: "user" as const, text: q }];
    setMessages(next);

    try {
      const { data } = await api<{ answer: string; citations?: any[]; missingInfo?: boolean }>(
        `/candidates/${activeCandidate.candidateId}/ask`,
        { method: "POST", body: JSON.stringify({ question: q }) }
      );
      setMessages([...next, { role: "assistant", text: data.answer, citations: data.citations }]);
    } catch (e) {
      setMessages([...next, { role: "assistant", text: `⚠️ Error: ${errText(e)}` }]);
    }
    setBusy(false);
  };

  return (
    <>
      <div className="row">
        <h1 className="grow">Ask the Recruiter Q&A</h1>
        {messages.length > 1 && (
          <button className="btn btn-ghost small" onClick={() => setMessages([messages[0]])}>
            🗑 Clear chat
          </button>
        )}
      </div>
      <p className="muted">
        Ask grounded questions about candidate capabilities, verified against processed resume chunks.
      </p>
      <Banner msg={error} />

      {/* Candidate selection bar */}
      <div className="card" style={{ marginTop: 12, padding: "10px 16px" }}>
        <div className="row">
          <span style={{ fontWeight: 600 }}>Target candidate:</span>
          {candidates.length > 0 ? (
            <select
              className="select grow"
              style={{ maxWidth: 360 }}
              value={activeCandidate?.candidateId ?? ""}
              onChange={(e) => {
                const found = candidates.find((c) => String(c.candidateId) === e.target.value);
                onSelectCandidate(found ?? null);
              }}
            >
              <option value="">-- Choose a candidate --</option>
              {candidates.map((c) => (
                <option key={String(c.candidateId)} value={String(c.candidateId)}>
                  {String(c.profile?.name ?? c.filename)} (#{String(c.candidateId).slice(0, 8)})
                </option>
              ))}
            </select>
          ) : (
            <span className="muted small">No candidates available. Upload one in the Upload tab.</span>
          )}
          {activeCandidate && (
            <button className="btn btn-ghost small" onClick={() => go("match")}>View profile →</button>
          )}
        </div>
      </div>

      {/* Chat conversation thread */}
      <div className="chat-container">
        {messages.map((m, i) => (
          <div key={i} className={`chat-msg chat-msg-${m.role}`}>
            <div className="chat-bubble">
              {m.text}
              {m.citations && m.citations.length > 0 && (
                <details style={{ marginTop: 8, fontSize: "0.8rem", opacity: 0.85 }}>
                  <summary style={{ cursor: "pointer", fontWeight: 600 }}>📄 {m.citations.length} resume context chunk{m.citations.length !== 1 ? "s" : ""} cited</summary>
                  <ul style={{ margin: "4px 0 0", paddingLeft: 16 }}>
                    {m.citations.map((c: any, j: number) => (
                      <li key={j} style={{ margin: "4px 0" }}>“{String(c.quote ?? c.text ?? "")}”</li>
                    ))}
                  </ul>
                </details>
              )}
            </div>
            {m.role === "assistant" && (
              <span className="small muted" style={{ fontSize: "0.7rem", marginTop: 2, paddingLeft: 4 }}>
                ⚠️ AI-generated — verify before use.
              </span>
            )}
          </div>
        ))}
        {busy && (
          <div className="chat-msg chat-msg-assistant">
            <div className="chat-bubble"><span className="spinner" /> Thinking…</div>
          </div>
        )}
      </div>

      {/* Input row */}
      <div className="row" style={{ marginTop: 12 }}>
        <input
          className="input grow"
          placeholder={activeCandidate ? `Ask about ${activeCandidate.profile?.name ?? activeCandidate.filename}…` : "Select a candidate above to ask questions…"}
          value={input}
          disabled={!activeCandidate || busy}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && send()}
        />
        <button className="btn btn-primary" onClick={send} disabled={!activeCandidate || busy || !input.trim()}>
          {busy ? <span className="spinner" /> : "Send"}
        </button>
      </div>
    </>
  );
};


/* ============================================================ Upload */
const Upload = () => {
  const [results, setResults] = useState<{ name: string; status: string; candidateId?: string }[]>([]);
  const [busy, setBusy] = useState(false);
  const [dragging, setDragging] = useState(false);

  const updateStatus = (name: string, status: string, candidateId?: string) =>
    setResults((r) => r.map((x) => (x.name === name ? { ...x, status, ...(candidateId ? { candidateId } : {}) } : x)));

  // Poll until the newly-uploaded file appears as READY or FAILED
  const pollForCandidate = (filename: string) => {
    let attempts = 0;
    const t = setInterval(async () => {
      attempts += 1;
      try {
        const { data } = await api<{ candidates: Candidate[] }>("/candidates");
        const match = (data.candidates ?? []).find(
          (c) => String(c.filename ?? "").toLowerCase() === filename.toLowerCase()
        );
        if (match) {
          const s = String(match.status);
          if (s === "READY" || s === "FAILED" || s === "DUPLICATE" || s === "ERROR") {
            updateStatus(
              filename,
              s === "READY"
                ? "✅ ready"
                : s === "DUPLICATE"
                ? `⚠ duplicate of ${match.duplicateOf ?? "existing"}`
                : `⚠ ${s.toLowerCase()}${match.failureReason ? `: ${match.failureReason}` : ""}`,
              String(match.candidateId)
            );
            clearInterval(t);
          } else {
            updateStatus(filename, `processing · ${s.toLowerCase()}…`, String(match.candidateId));
          }
        }
      } catch { /* keep polling */ }
      if (attempts > 30) { clearInterval(t); } // give up after ~2 min
    }, 4000);
  };

  const onFiles = async (files: FileList | null) => {
    if (!files?.length) return;
    setBusy(true);
    for (const file of Array.from(files)) {
      setResults((r) => [...r, { name: file.name, status: "checking & hashing…" }]);
      try {
        let sha256 = "";
        try {
          const buffer = await file.arrayBuffer();
          const hashBuffer = await crypto.subtle.digest("SHA-256", buffer);
          sha256 = Array.from(new Uint8Array(hashBuffer))
            .map((b) => b.toString(16).padStart(2, "0"))
            .join("");
        } catch {
          // fallback if crypto.subtle unavailable
        }

        const { data } = await api<{
          resourceId?: string;
          uploadUrl?: string;
          already_exists?: boolean;
          candidateId?: string;
          message?: string;
        }>("/uploads/presign", {
          method: "POST",
          body: JSON.stringify({ kind: "resume", filename: file.name, sizeBytes: file.size, sha256 }),
        });

        if (data.already_exists) {
          updateStatus(
            file.name,
            "⚠️ duplicate detected — already exists (no duplicate created)",
            data.candidateId
          );
          continue;
        }

        if (data.uploadUrl) {
          updateStatus(file.name, "uploading file…", data.resourceId);
          await fetch(data.uploadUrl, { method: "PUT", body: file, headers: { "Content-Type": file.type } });
          updateStatus(file.name, "uploaded · waiting for processing…", data.resourceId);
          pollForCandidate(file.name);
        }
      } catch (e) {
        updateStatus(file.name, `❌ failed: ${errText(e)}`);
      }
    }
    setBusy(false);
  };

  const onDrop = (e: React.DragEvent) => {
    e.preventDefault();
    setDragging(false);
    onFiles(e.dataTransfer.files);
  };

  return (
    <>
      <h1>Upload resumes</h1>
      <p className="muted">PDF or DOCX. Extraction runs automatically ~10–20 s after upload.</p>
      <label
        className="file-label"
        style={{ border: `2px dashed ${dragging ? "var(--accent)" : "var(--border-strong)"}`,
                 background: dragging ? "var(--accent-soft)" : undefined, transition: "all 0.2s" }}
        onDragOver={(e) => { e.preventDefault(); setDragging(true); }}
        onDragLeave={() => setDragging(false)}
        onDrop={onDrop}
      >
        <span className={busy ? "spinner" : ""} />
        {busy ? "Uploading…" : dragging ? "Drop files here" : "Choose files or drag & drop"}
        <input className="file-input" type="file" multiple accept=".pdf,.docx" disabled={busy}
               onChange={(e) => onFiles(e.target.files)} />
      </label>
      <ul style={{ listStyle: "none", padding: 0 }}>
        {results.map((r, i) => (
          <li key={i} className="row" style={{ marginTop: 8, animation: "page-in 320ms var(--ease-out)" }}>
            <span>{r.name}</span>
            <span className="muted small">— {r.status}</span>
            {r.candidateId && <span className="chip chip-good small">id {r.candidateId.slice(0, 8)}</span>}
          </li>
        ))}
      </ul>
    </>
  );
};


/* ============================================================ Candidates */
const Candidates = ({
  activeCandidateId,
  onSelectCandidate,
  go,
}: {
  activeCandidateId?: string | null;
  onSelectCandidate?: (c: Candidate) => void;
  go?: (p: Page) => void;
}) => {
  const [items, setItems] = useState<Candidate[]>([]);
  const [error, setError] = useState("");
  const [open, setOpen] = useState<Candidate | null>(null);

  const load = () => {
    setError("");
    api<{ candidates: Candidate[] }>("/candidates")
      .then(({ data }) => setItems(data.candidates ?? []))
      .catch((e) => setError(errText(e)));
  };
  useEffect(load, []);

  const del = async (c: Candidate) => {
    if (!confirm(`Delete candidate ${c.filename}? This removes S3 objects, vectors and all records.`)) return;
    try {
      await api(`/candidates/${c.candidateId}`, { method: "DELETE" });
      setItems((list) => list.filter((x) => x.candidateId !== c.candidateId));
      if (open?.candidateId === c.candidateId) setOpen(null);
    } catch (e) { setError(errText(e)); }
  };

  return (
    <>
      <div className="row">
        <h1 className="grow">Candidates</h1>
        <button className="btn" onClick={() => go?.("upload")}>+ Upload new</button>
        <button className="btn" onClick={load}>↻ Refresh</button>
      </div>
      <Banner msg={error} />
      <div className="card" style={{ marginTop: 14 }}>
        {items.length === 0 ? (
          <Empty>No candidates yet — upload a resume in the Upload tab.</Empty>
        ) : (
          <table className="table">
            <thead>
              <tr><th>Name</th><th>File</th><th>Status</th><th>Top roles</th><th>Active</th><th /></tr>
            </thead>
            <tbody>
              {items.map((c, i) => {
                const isActive = activeCandidateId === c.candidateId;
                return (
                  <tr key={String(c.candidateId)} style={{ animationDelay: `${i * 40}ms` }}>
                    <td
                      style={{ color: "var(--text-hi)", fontWeight: 600, cursor: "pointer" }}
                      onClick={() => setOpen(c)}
                    >
                      {String(c.profile?.name ?? "(unextracted)")}
                    </td>
                    <td className="small muted">{String(c.filename)}</td>
                    <td><Pill status={String(c.status)} /></td>
                    <td className="small muted">
                      {c.status === "DUPLICATE" && c.duplicateOf
                        ? `duplicate of ${String(c.duplicateOf)}`
                        : c.status === "FAILED" && c.failureReason
                          ? String(c.failureReason)
                          : Array.isArray(c.roleSuggestions) && c.roleSuggestions.length
                            ? c.roleSuggestions.slice(0, 3).map((r: any) => r.role).join(", ")
                            : Array.isArray(c.roles) && c.roles.length
                              ? c.roles.slice(0, 3).map((r: any) => r.role).join(", ")
                              : "—"}
                    </td>
                    <td>
                      <button
                        className={`btn ${isActive ? "btn-primary" : "btn-ghost"} small`}
                        style={{ padding: "3px 10px" }}
                        onClick={() => onSelectCandidate?.(c)}
                      >
                        {isActive ? "✅ Active" : "Select 👤"}
                      </button>
                    </td>
                    <td>
                      <div className="row" style={{ gap: 6 }}>
                        <button
                          className="btn btn-ghost small"
                          title="Open full profile"
                          onClick={() => setOpen(c)}
                        >
                          Profile →
                        </button>
                        <button
                          className="btn btn-ghost small"
                          title="Ask questions about this candidate"
                          onClick={() => {
                            onSelectCandidate?.(c);
                            go?.("chat");
                          }}
                        >
                          Chat 💬
                        </button>
                        <button
                          className="btn btn-danger small"
                          title="Delete candidate"
                          onClick={() => del(c)}
                        >
                          ✕
                        </button>
                      </div>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </div>
      {open && (
        <CandidateDrawer
          candidate={open}
          onClose={() => setOpen(null)}
          onSelectActive={() => onSelectCandidate?.(open)}
        />
      )}
    </>
  );
};

/* ============================================================ Candidate drawer (detail + Q&A) */
const CandidateDrawer = ({
  candidate,
  onClose,
  onSelectActive,
}: {
  candidate: Candidate;
  onClose: () => void;
  onSelectActive?: () => void;
}) => {
  const [detail, setDetail] = useState<Candidate | null>(null);
  const [error, setError] = useState("");
  const [question, setQuestion] = useState("");
  const [chat, setChat] = useState<{ q: string; a: string; citations: { quote: string; chunkIndex: number }[]; missing?: boolean }[]>([]);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    api<Candidate>(`/candidates/${candidate.candidateId}`)
      .then(({ data }) => setDetail(data))
      .catch((e) => setError(errText(e)));
  }, [candidate.candidateId]);

  const [resumeText, setResumeText] = useState<{ text: string; available: boolean } | null>(null);
  const [showJson, setShowJson] = useState(false);
  useEffect(() => {
    api<{ text: string; available: boolean }>(`/candidates/${candidate.candidateId}/text`)
      .then(({ data }) => setResumeText({ text: data.text ?? "", available: data.available }))
      .catch(() => setResumeText({ text: "", available: false }));
  }, [candidate.candidateId]);

  const ask = async () => {
    const q = question.trim();
    if (!q || busy) return;
    setBusy(true);
    setQuestion("");
    try {
      const { data } = await api<{ answer: string; citations: any[]; missingInfo?: boolean }>(
        `/candidates/${candidate.candidateId}/ask`, { method: "POST", body: JSON.stringify({ question: q }) });
      setChat((c) => [...c, { q, a: data.answer, citations: data.citations ?? [], missing: data.missingInfo }]);
    } catch (e) {
      setChat((c) => [...c, { q, a: `Error: ${errText(e)}`, citations: [] }]);
    }
    setBusy(false);
  };

  const p = detail?.profile ?? {};
  const roles: any[] = detail?.roles ?? [];

  return (
    <div style={{ position: "fixed", inset: 0, zIndex: 50, background: "rgba(4,6,10,0.6)", backdropFilter: "blur(6px)" }}
         onClick={onClose}>
      <div className="card page" style={{ position: "relative", margin: "48px auto", maxWidth: 720, maxHeight: "85vh", overflowY: "auto" }}
           onClick={(e) => e.stopPropagation()}>
        <div className="row">
          <h2 className="grow">{String(p.name ?? detail?.filename ?? candidate.filename)}</h2>
          <Pill status={String(detail?.status ?? candidate.status)} />
          {onSelectActive && (
            <button className="btn btn-primary small" onClick={onSelectActive}>Set as Active 👤</button>
          )}
          <button className="btn btn-ghost" onClick={onClose}>✕</button>
        </div>
        <Banner msg={error} />
        {detail?.status === "UPLOADED" && (
          <div className="banner" style={{ background: "rgba(255, 204, 102, 0.15)", color: "var(--warn)", borderColor: "rgba(255, 204, 102, 0.3)" }}>
            <span>ℹ</span> Resume is currently being processed by the ingestion pipeline. Profile will populate shortly.
          </div>
        )}

        {detail?.status === "DUPLICATE" && (
          <div className="banner">
            <span>⚠</span> Duplicate of candidate {String(detail?.duplicateOf ?? "?")} — the same file was uploaded before.
          </div>
        )}
        {detail?.status === "FAILED" && (
          <div className="banner">
            <span>⚠</span> Processing failed: {String(detail?.failureReason ?? "unknown reason")}
          </div>
        )}

        {detail && (
          <>
            {roles.length > 0 && (
              <div className="section">
                <div className="section-head"><span className="section-title">Best-fit roles</span></div>
                {roles.slice(0, 5).map((r: any, i: number) => (
                  <div key={i} className="row" style={{ margin: "6px 0" }}>
                    <span style={{ fontWeight: 700, minWidth: 190 }}>{String(r.role)}</span>
                    <div className="scorebar scorebar-track" style={{ flex: 1 }}>
                      <div className="scorebar-fill" style={{ width: `${Math.round(Number(r.score) || 0)}%` }} />
                    </div>
                    <span className="score-num">{Math.round(Number(r.score) || 0)}</span>
                  </div>
                ))}
                {roles.some((r: any) => r.rationale) && (
                  <p className="quote small">{String(roles.find((r: any) => r.rationale)?.rationale)}</p>
                )}
              </div>
            )}

            <div className="section">
              <div className="section-head"><span className="section-title">Profile</span></div>
              <div className="row" style={{ gap: 14 }}>
                <div className="grow">
                  <div className="section-title" style={{ marginBottom: 4 }}>Skills</div>
                  {((p.skills as string[]) ?? []).length
                    ? ((p.skills as string[]).map((s) => <span className="chip" key={s}>{s}</span>))
                    : <span className="muted">—</span>}
                </div>
              </div>
              <div className="row" style={{ gap: 14, marginTop: 10 }}>
                <div className="grow">
                  <div className="section-title" style={{ marginBottom: 4 }}>Tools</div>
                  {((p.tools as string[]) ?? []).length
                    ? ((p.tools as string[]).map((s) => <span className="chip" key={s}>{s}</span>))
                    : <span className="muted">—</span>}
                </div>
                <div className="grow">
                  <div className="section-title" style={{ marginBottom: 4 }}>Languages</div>
                  {((p.languages as string[]) ?? []).length
                    ? ((p.languages as string[]).map((s) => <span className="chip" key={s}>{s}</span>))
                    : <span className="muted">—</span>}
                </div>
              </div>
              {p.summary ? <p className="quote" style={{ marginTop: 12 }}>{String(p.summary)}</p> : null}

              <ProfileSections profile={p} />

              <div className="row" style={{ marginTop: 14 }}>
                <button className="btn" onClick={() => setShowJson((v) => !v)}>
                  {showJson ? "Hide raw JSON" : "View raw JSON"}
                </button>
              </div>
              {showJson && (
                <pre className="quote small" style={{
                  whiteSpace: "pre-wrap", maxHeight: 320, overflowY: "auto",
                  marginTop: 8, fontFamily: "ui-monospace, monospace",
                }}>{JSON.stringify(detail, null, 2)}</pre>
              )}
            </div>

            <div className="section">
              <div className="section-head">
                <span className="section-title">Extracted resume text</span>
                {resumeText?.available && <span className="muted small">{resumeText.text.length} chars</span>}
              </div>
              {resumeText == null
                ? <div className="muted small">Loading…</div>
                : resumeText.available && resumeText.text
                  ? <pre className="quote small" style={{
                      whiteSpace: "pre-wrap", maxHeight: 260, overflowY: "auto",
                      fontFamily: "ui-monospace, monospace",
                    }}>{resumeText.text}</pre>
                  : <div className="muted small">No extracted text available (not processed yet, or extraction failed).</div>}
            </div>

            <div className="section">
              <div className="section-head"><span className="section-title">Ask this candidate</span></div>
              {chat.map((m, i) => (
                <div key={i} className="qa-msg" style={{ margin: "10px 0" }}>
                  <div className="row"><span style={{ fontWeight: 700 }}>You:</span><span>{m.q}</span></div>
                  <div className="row" style={{ alignItems: "flex-start" }}>
                    <span style={{ fontWeight: 700 }}>AI:</span>
                    <span className="grow qa-answer">{m.a}</span>
                  </div>
                  {m.citations.map((ct, j) => (
                    <div className="quote small" key={j}>“{ct.quote}”</div>
                  ))}
                  {m.missing && <div className="muted small">No matching resume content found for that question.</div>}
                </div>
              ))}
              <div className="row" style={{ marginTop: 8 }}>
                <input className="input grow" placeholder="e.g. Did they lead any migrations?" value={question}
                       onChange={(e) => setQuestion(e.target.value)} onKeyDown={(e) => e.key === "Enter" && ask()} />
                <button className="btn btn-primary" onClick={ask} disabled={busy || !question.trim()}>
                  {busy ? <span className="spinner" /> : "Ask"}
                </button>
              </div>
            </div>
          </>
        )}
        {!detail && !error && <div className="row"><span className="spinner" /> Loading…</div>}
      </div>
    </div>
  );
};

/* Profile sub-sections the drawer was missing: education, experience, projects, */
/* certifications, claims, location/contact. Empty-tolerant: NULLs render as —. */
const asArr = (v: unknown): any[] => (Array.isArray(v) ? v : []);

const ProfileSections = ({ profile }: { profile: Record<string, any> }) => {
  const education = asArr(profile.education);
  const experience = asArr(profile.experience).filter(
    (e: any) => e && typeof e === "object" && (e.title || e.company || e.summary));
  const projects = asArr(profile.projects);
  const certifications = asArr(profile.certifications);
  const claims = asArr(profile.claims);
  const contact = [profile.email, profile.phone, profile.location]
    .filter((x) => x && typeof x === "string");

  return (
    <>
      {contact.length > 0 && (
        <div className="row small muted" style={{ marginTop: 10, gap: 12 }}>
          {contact.map((c, i) => <span key={i}>{String(c)}</span>)}
        </div>
      )}
      {education.length > 0 && (
        <div style={{ marginTop: 12 }}>
          <div className="section-title" style={{ marginBottom: 4 }}>Education</div>
          {education.map((e: any, i: number) => (
            <div className="row small" key={i}>
              <b>{String(e.degree ?? "—")}</b>
              <span className="muted">{String(e.institution ?? "")}{e.year ? ` · ${e.year}` : ""}</span>
            </div>
          ))}
        </div>
      )}
      {experience.length > 0 && (
        <div style={{ marginTop: 12 }}>
          <div className="section-title" style={{ marginBottom: 4 }}>Experience</div>
          {experience.map((e: any, i: number) => (
            <div key={i} style={{ margin: "6px 0" }}>
              <div className="row small">
                <b style={{ minWidth: 160 }}>{String(e.title ?? "—")}</b>
                <span className="muted">
                  {String(e.company ?? "")}
                  {e.start || e.end ? ` · ${String(e.start ?? "?")} – ${String(e.end ?? "present")}` : ""}
                </span>
              </div>
              {e.summary && <div className="small muted" style={{ marginTop: 2 }}>{String(e.summary)}</div>}
            </div>
          ))}
        </div>
      )}
      {projects.length > 0 && (
        <div style={{ marginTop: 12 }}>
          <div className="section-title" style={{ marginBottom: 4 }}>Projects</div>
          {projects.map((pr: any, i: number) => (
            <div className="small" key={i} style={{ margin: "4px 0" }}>
              {typeof pr === "string"
                ? pr
                : <><b>{String(pr.name ?? "—")}</b>{pr.description ? ` — ${String(pr.description)}` : ""}{pr.stack ? ` (${String(pr.stack)})` : ""}</>}
            </div>
          ))}
        </div>
      )}
      {certifications.length > 0 && (
        <div style={{ marginTop: 12 }}>
          <div className="section-title" style={{ marginBottom: 4 }}>Certifications</div>
          {certifications.map((c: any, i: number) => (
            <div className="small" key={i} style={{ margin: "4px 0" }}>
              {typeof c === "string"
                ? c
                : <><b>{String(c.name ?? "—")}</b>{c.issuer ? ` · ${String(c.issuer)}` : ""}{c.year ? ` (${String(c.year)})` : ""}</>}
            </div>
          ))}
        </div>
      )}
      {claims.length > 0 && (
        <div style={{ marginTop: 12 }}>
          <div className="section-title" style={{ marginBottom: 4 }}>Claimed highlights</div>
          {claims.map((c: unknown, i: number) => (
            <div className="quote small" key={i}>“{typeof c === "string" ? c : JSON.stringify(c)}”</div>
          ))}
        </div>
      )}
    </>
  );
};

/* ============================================================ Jobs */
const Jobs = () => {
  const [jobs, setJobs] = useState<Job[]>([]);
  const [jdText, setJdText] = useState("");
  const [error, setError] = useState("");
  const [open, setOpen] = useState<Job | null>(null);

  const load = () => {
    setError("");
    api<{ jobs: Job[] }>("/jobs")
      .then(({ data }) => setJobs(data.jobs ?? []))
      .catch((e) => setError(errText(e)));
  };
  useEffect(load, []);
  useEffect(() => {
    const parsing = jobs.some((j) => j.status === "PARSING");
    if (!parsing) return;
    const t = setInterval(load, 5000);
    return () => clearInterval(t);
  }, [jobs]);

  const createJob = async () => {
    setError("");
    try {
      await api("/jobs", { method: "POST", body: JSON.stringify({ jdText }) });
      setJdText("");
      load();
    } catch (e) { setError(errText(e)); }
  };

  const deleteJob = async (j: Job, e: React.MouseEvent) => {
    e.stopPropagation();
    if (!confirm(`Delete job "${String(j.title)}"? This cannot be undone.`)) return;
    setError("");
    try {
      await api(`/jobs/${j.jobId}`, { method: "DELETE" });
      setJobs((list) => list.filter((x) => x.jobId !== j.jobId));
      if (open?.jobId === j.jobId) setOpen(null);
    } catch (e) { setError(errText(e)); }
  };

  return (
    <>
      <h1>Jobs</h1>
      <Banner msg={error} />
      <div className="card" style={{ marginTop: 12 }}>
        <h3>Paste a job description</h3>
        <textarea className="textarea" rows={6} placeholder="Paste the full JD text — skills, years, seniority…"
                  value={jdText} onChange={(e) => setJdText(e.target.value)} />
        <div className="row" style={{ marginTop: 10 }}>
          <button className="btn btn-primary" onClick={createJob} disabled={!jdText.trim()}>Create job</button>
          <button className="btn" onClick={load}>↻ Refresh</button>
        </div>
      </div>
      <div className="card">
        {jobs.length === 0 ? (
          <Empty>No jobs yet.</Empty>
        ) : (
          <table className="table">
            <thead><tr><th>Title</th><th>Status</th><th>Parsed</th><th>Actions</th></tr></thead>
            <tbody>
              {jobs.map((j, i) => (
                <tr key={String(j.jobId)} style={{ animationDelay: `${i * 40}ms` }} className="clickable"
                    onClick={() => setOpen(j)}>
                  <td style={{ color: "var(--text-hi)", fontWeight: 600 }}>{String(j.title)}</td>
                  <td><Pill status={String(j.status)} /></td>
                  <td className="small muted">
                    {j.parsedRequirements?.requiredSkills?.length
                      ? `${j.parsedRequirements.requiredSkills.length} required skills`
                      : "—"}
                  </td>
                  <td>
                    <div className="row" style={{ gap: 6 }}>
                      <button className="btn btn-ghost small" onClick={(e) => { e.stopPropagation(); setOpen(j); }}>Open</button>
                      <button className="btn btn-danger small" title="Delete job" onClick={(e) => deleteJob(j, e)}>✕ Delete</button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
      {open && <JobDetail job={open} onClose={() => { setOpen(null); load(); }} />}
    </>
  );
};

/* ============================================================ Job detail (read-only: raw JD + parsed tech stack) */
const JobDetail = ({ job, onClose }: { job: Job; onClose: () => void }) => {
  const [detail, setDetail] = useState<Job | null>(job);
  const [error, setError] = useState("");

  useEffect(() => {
    api<Job>(`/jobs/${job.jobId}`)
      .then(({ data }) => setDetail(data))
      .catch((e) => setError(errText(e)));
  }, [job.jobId]);

  const req = detail?.parsedRequirements;
  const jd = typeof detail?.jdText === "string" ? detail.jdText : "";

  return (
    <div style={{ position: "fixed", inset: 0, zIndex: 50, background: "rgba(4,6,10,0.6)", backdropFilter: "blur(6px)" }}
         onClick={onClose}>
      <div className="card page" style={{ position: "relative", margin: "48px auto", maxWidth: 780, maxHeight: "85vh", overflowY: "auto" }}
           onClick={(e) => e.stopPropagation()}>
        <div className="row">
          <h2 className="grow">{String(detail?.title ?? "(job)")}</h2>
          <Pill status={String(detail?.status ?? "")} />
          <button className="btn btn-ghost" onClick={onClose}>✕</button>
        </div>
        <Banner msg={error} />

        {detail?.status === "PARSING" && (
          <div className="banner">
            <span>ℹ</span> The AI is still parsing this job description — the parsed requirements below will appear shortly.
          </div>
        )}

        {jd ? (
          <div className="section">
            <div className="section-head">
              <span className="section-title">Job description</span>
              <span className="muted small">{jd.length} chars</span>
            </div>
            <pre className="quote small" style={{
              whiteSpace: "pre-wrap", maxHeight: 260, overflowY: "auto",
              fontFamily: "ui-monospace, monospace",
            }}>{jd}</pre>
          </div>
        ) : (
          <p className="muted small">Raw JD text isn&apos;t stored for this job (it was created before raw JD persistence). Parsed requirements below still apply.</p>
        )}

        {req && (
          <div className="section">
            <div className="section-head"><span className="section-title">Parsed requirements</span></div>
            <div className="row small muted" style={{ gap: 14 }}>
              {req.minYears != null && <span>{String(req.minYears)}+ yrs</span>}
              {req.seniority && <span>{String(req.seniority)}</span>}
              {req.education && <span>{String(req.education)}</span>}
              {req.domain && <span>{String(req.domain)}</span>}
            </div>
            <div style={{ marginTop: 8 }}>
              {(req.requiredSkills as string[] ?? []).map((s) => <span className="chip chip-good" key={s}>{s}</span>)}
              {(req.preferredSkills as string[] ?? []).map((s) => <span className="chip" key={s}>{s}</span>)}
              {(req.mustHaves as string[] ?? []).map((s) => <span className="chip chip-warn" key={s}>★ {s}</span>)}
            </div>
          </div>
        )}
      </div>
    </div>
  );
};

/* ============================================================ Analysis panel (opened from rankings) */
const AnalysisPanel = ({ jobId, onLoad }: { jobId: string; onLoad: () => void }) => {
  const [cid, setCid] = useState("");
  const [analysis, setAnalysis] = useState<Analysis | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    const h = (e: Event) => {
      const d = (e as CustomEvent).detail;
      setCid(d.candidateId);
      setAnalysis(null);
      setError("");
      api<Analysis>(`/jobs/${d.jobId}/candidates/${d.candidateId}/analysis`)
        .then(({ data }) => setAnalysis(data))
        .catch((e) => setError(errText(e)));
    };
    window.addEventListener("open-analysis", h);
    return () => window.removeEventListener("open-analysis", h);
  }, [jobId]);

  if (!cid) return null;
  const band = (n: number) => (n >= 75 ? "good" : n >= 50 ? "warn" : "bad");
  const verdictChip: Record<string, string> = { Supported: "good", Weak: "warn", Unsupported: "bad" };
  const sevChip: Record<string, string> = { Low: "good", Medium: "warn", High: "bad" };

  return (
    <div className="card section">
      <div className="row">
        <h3 className="grow">Analysis</h3>
        {analysis && <span className="stat-value" style={{ fontSize: "2rem" }}>{analysis.totalScore}</span>}
        <button className="btn btn-ghost" onClick={() => { setCid(""); setAnalysis(null); }}>✕</button>
      </div>
      <Banner msg={error} />
      {analysis && (
        <>
          <div className="section">
            <div className="section-head"><span className="section-title">Criteria</span></div>
            {(analysis.criteria as any[]).map((c: any, i: number) => (
              <div key={i} style={{ margin: "8px 0" }}>
                <div className="row small">
                  <span style={{ fontWeight: 700, minWidth: 170 }}>{String(c.name)}</span>
                  <span className="muted">weight {String(c.weight)}</span>
                </div>
                <div className="scorebar">
                  <div className="scorebar-track" style={{ flex: 1 }}>
                    <div className={`scorebar-fill ${band(Number(c.score)) === "bad" ? "chip-bad" : ""}`}
                         style={{
                           width: `${Math.round(Number(c.score) || 0)}%`,
                           background: band(Number(c.score)) === "bad" ? "linear-gradient(90deg,#ff7a8a,#ffcc66)"
                             : band(Number(c.score)) === "warn" ? "linear-gradient(90deg,#ffcc66,#4ade9b)" : undefined,
                         }} />
                  </div>
                  <span className="score-num">{Math.round(Number(c.score) || 0)}</span>
                </div>
                {(c.evidence as any[] ?? []).map((ev: any, j: number) => (
                  <div className="quote small" key={j}>
                    <b>{String(ev.requirement)}:</b> “{String(ev.quote)}”
                  </div>
                ))}
                {(!(c.evidence as any[])?.length) && <div className="muted small">No evidence found for this criterion.</div>}
              </div>
            ))}
          </div>

          <div className="section">
            <div className="section-head"><span className="section-title">Gaps</span></div>
            {((analysis.gaps as any[]) ?? []).length === 0 ? <span className="muted small">No gaps.</span> : null}
            {((analysis.gaps as any[]) ?? []).map((g: any, i: number) => (
              <div className="row small" key={i} style={{ margin: "4px 0" }}>
                <span style={{ minWidth: 220 }}>{String(g.requirement)}</span>
                <span className={`chip ${(g.status === "Met" ? "good" : g.status === "Partial" ? "warn" : "bad")}`}>{String(g.status)}</span>
                <span className={`chip chip-${sevChip[String(g.severity)] ?? "neutral"}`}>{String(g.severity)}</span>
                {g.transferable && <span className="muted">↔ {String(g.transferable)}</span>}
              </div>
            ))}
          </div>

          <div className="section">
            <div className="section-head"><span className="section-title">Claim checks</span></div>
            {((analysis.claimChecks as any[]) ?? []).map((cc: any, i: number) => (
              <div className="row small" key={i} style={{ margin: "4px 0" }}>
                <span className={`chip chip-${verdictChip[String(cc.verdict)] ?? "neutral"}`}>{String(cc.verdict)}</span>
                <span className="grow">{String(cc.claim)}</span>
                {cc.support && <span className="muted small">{String(cc.support)}</span>}
              </div>
            ))}
          </div>

          <div className="section">
            <div className="section-head"><span className="section-title">Flags</span></div>
            {((analysis.flags as any[]) ?? []).map((f: any, i: number) => (
              <div className="row small" key={i}>
                <span className="chip chip-warn">{String(f.type)}</span>
                <span>{String(f.detail)}</span>
              </div>
            ))}
            {!((analysis.flags as any[]) ?? []).length && <span className="muted small">No flags.</span>}
          </div>

          <div className="section">
            <div className="section-head"><span className="section-title">Screening questions</span></div>
            {((analysis.questions as any[]) ?? []).map((q: any, i: number) => (
              <div className="card" style={{ margin: "8px 0", background: "var(--bg-2)" }} key={i}>
                <div className="row">
                  <span className="chip chip-bad">{String(q.category)}</span>
                  <b className="grow">{String(q.question)}</b>
                  <button
                    className="btn btn-ghost small"
                    title="Copy question to clipboard"
                    onClick={() => navigator.clipboard.writeText(
                      `Q: ${String(q.question)}\nWhy: ${String(q.why)}\nGood answer: ${String(q.goodAnswer)}`
                    )}
                  >⎘ Copy</button>
                </div>
                <div className="small muted" style={{ marginTop: 6 }}>Why: {String(q.why)}</div>
                <div className="quote small">Good answer: {String(q.goodAnswer)}</div>
              </div>
            ))}
          </div>
        </>
      )}
    </div>
  );
};

/* ============================================================ Search */

type SearchResult = {
  name: string;
  candidateId: string;
  matchScore: number;
  matchReason: string;
  matchedSkills: string[];
};

const Search = () => {
  const [q, setQ] = useState("");
  const [results, setResults] = useState<SearchResult[]>([]);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  const run = async () => {
    const query = q.trim();
    if (!query || busy) return;
    setBusy(true);
    setError("");
    setResults([]);
    try {
      // 1. Fetch all READY candidates from the backend
      const { data: candData } = await api<{ candidates: Candidate[] }>("/candidates");
      const readyCandidates = (candData.candidates ?? []).filter((c) => c.status === "READY");

      if (readyCandidates.length === 0) {
        setError("No READY candidates found. Upload and process resumes first.");
        setBusy(false);
        return;
      }

      // 2. Build a compact profile summary for each candidate
      const candidateSummaries = readyCandidates.map((c) => {
        const p = c.profile ?? {};
        const skills = [...(p.skills ?? []), ...(p.tools ?? []), ...(p.languages ?? [])].join(", ");
        const roles = (c.roles ?? c.roleSuggestions ?? []).slice(0, 3).map((r: any) => r.role).join(", ");
        return {
          candidateId: String(c.candidateId),
          name: String(p.name ?? c.filename ?? c.candidateId),
          summary: `Name: ${String(p.name ?? c.filename)}. Skills/Tools/Languages: ${skills || "N/A"}. Top roles: ${roles || "N/A"}. Summary: ${String(p.summary ?? "")}.`,
          matchedSkills: [...(p.skills ?? []), ...(p.tools ?? [])].filter(
            (s: string) => query.toLowerCase().includes(s.toLowerCase())
          ),
        };
      });

      if (!GEMINI_API_KEY) {
        // Fallback: simple keyword-based ranking when no Gemini key
        const scored = candidateSummaries.map((c) => ({
          ...c,
          matchScore: Math.min(100, c.matchedSkills.length * 20 + 10),
          matchReason: c.matchedSkills.length > 0
            ? `Matched skills: ${c.matchedSkills.join(", ")}.`
            : "No direct skill overlap detected from keyword matching.",
        }));
        scored.sort((a, b) => b.matchScore - a.matchScore);
        setResults(scored.slice(0, 3));
        setBusy(false);
        return;
      }

      // 3. Call the Gemini API to rank and explain the top 3 (swapped from the
      //    exhausted Groq free tier; same JSON contract as before).
      const geminiUser = [
        `Search query: "${query}"`,
        `Candidates:`,
        ...candidateSummaries.map((c, i) => `${i + 1}. ID: ${c.candidateId}\n   ${c.summary}`),
      ].join("\n\n");
      const raw = await geminiJson(
        `You are a recruitment AI. Given a search query and candidate profiles, identify the TOP 3 best-matching candidates and explain why each matches. Respond ONLY with valid JSON in this exact format (no markdown, no extra text):
[{"candidateId":"...","matchScore":85,"matchReason":"Clear 2-3 sentence explanation of why this candidate matches the query."}]`,
        geminiUser,
        900,
      );
      const rankings = parseModelJson(raw) as { candidateId: string; matchScore: number; matchReason: string }[];
      if (!Array.isArray(rankings) || rankings.length === 0) throw new Error("Search model returned no rankings — try again.");

      const top3: SearchResult[] = rankings.slice(0, 3).map((rank) => {
        const cand = candidateSummaries.find((c) => c.candidateId === rank.candidateId);
        return {
          candidateId: rank.candidateId,
          name: cand?.name ?? rank.candidateId,
          matchScore: rank.matchScore,
          matchReason: rank.matchReason,
          matchedSkills: cand?.matchedSkills ?? [],
        };
      });

      setResults(top3);
    } catch (e) {
      setError(errText(e));
    }
    setBusy(false);
  };

  const scoreBandLocal = (s: number) => {
    if (s >= 75) return { chip: "chip-good", emoji: "🟢" };
    if (s >= 55) return { chip: "chip", emoji: "🔵" };
    if (s >= 35) return { chip: "chip-warn", emoji: "🟡" };
    return { chip: "chip-bad", emoji: "🔴" };
  };

  return (
    <>
      <h1>Search</h1>
      <p className="muted">Natural language search — returns the top 3 best-matching candidates with AI-powered explanations.</p>
      <Banner msg={error} />
      <div className="row" style={{ marginTop: 10 }}>
        <input
          className="input grow"
          placeholder='e.g. "Kotlin developers with 3+ years who shipped payment systems"'
          value={q}
          onChange={(e) => setQ(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && run()}
        />
        <button className="btn btn-primary" onClick={run} disabled={busy || !q.trim()}>
          {busy ? <><span className="spinner" /> Searching…</> : "Search"}
        </button>
      </div>

      <div className="card" style={{ marginTop: 14 }}>
        {results.length === 0 && !busy && (
          <Empty>{q ? "No candidates matched — every matched result needs a READY, processed candidate." : "Run a search to see the top 3 best-matching candidates."}</Empty>
        )}
        {busy && (
          <div className="row" style={{ justifyContent: "center", padding: "20px 0" }}>
            <span className="spinner" />
            <span className="muted" style={{ marginLeft: 10 }}>Ranking candidates with AI…</span>
          </div>
        )}
        {results.map((r, i) => {
          const band = scoreBandLocal(r.matchScore);
          return (
            <div
              className="card"
              style={{ margin: "10px 0", background: "var(--bg-2)", borderLeft: "3px solid var(--accent)" }}
              key={i}
            >
              <div className="row">
                <span style={{
                  background: "var(--accent)", color: "#000", borderRadius: "50%",
                  width: 28, height: 28, display: "flex", alignItems: "center",
                  justifyContent: "center", fontWeight: 700, fontSize: "0.85rem", flexShrink: 0,
                }}>
                  #{i + 1}
                </span>
                <b className="grow" style={{ color: "var(--text-hi)", fontSize: "1.05rem" }}>{r.name}</b>
                <span className={`chip ${band.chip}`} style={{ fontSize: "0.9rem", padding: "3px 10px" }}>
                  {band.emoji} {r.matchScore}% match
                </span>
              </div>
              {r.matchedSkills.length > 0 && (
                <div className="row" style={{ marginTop: 8, flexWrap: "wrap" }}>
                  {r.matchedSkills.map((s) => <span className="chip chip-good small" key={s}>{s}</span>)}
                </div>
              )}
              <div style={{
                marginTop: 10, padding: "10px 14px",
                background: "rgba(122,162,255,0.07)", borderRadius: 8,
                borderLeft: "3px solid rgba(122,162,255,0.4)",
              }}>
                <span className="small muted" style={{ fontWeight: 600, display: "block", marginBottom: 4 }}>Why this candidate matches:</span>
                <span className="small" style={{ color: "var(--text-hi)", lineHeight: 1.6 }}>{r.matchReason}</span>
              </div>
            </div>
          );
        })}
      </div>
    </>
  );
};

/* ============================================================ Compare Candidates */
type CompareAdvantage = "A" | "B" | "tie";
type CompareRow = { dimension: string; a: string; b: string; advantage: CompareAdvantage };
type CompareResultData = {
  table: CompareRow[];
  strengths: { a: string[]; b: string[] };
  weaknesses: { a: string[]; b: string[] };
  verdict: { winner: "A" | "B" | "tie"; confidence: "high" | "medium" | "low"; explanation: string };
  source: "ai" | "heuristic";
};

const cmpNorm = (s: string) => s.toLowerCase().trim();
const cmpName = (c: Candidate | undefined, fallback: string) =>
  c ? String((c.profile ?? {}).name ?? c.filename ?? c.candidateId) : fallback;
const cmpSkillList = (c: Candidate | undefined): string[] => {
  if (!c) return [];
  const p: any = c.profile ?? {};
  return [...(p.skills ?? []), ...(p.tools ?? []), ...(p.languages ?? [])]
    .map((x: unknown) => String(x).trim()).filter(Boolean);
};
const cmpAsArray = (v: unknown): string[] =>
  Array.isArray(v) ? v.map((x) => String(x)).filter(Boolean) : typeof v === "string" && v ? [v] : [];
const cmpAsAdvantage = (v: unknown): CompareAdvantage => {
  const s = String(v ?? "").trim().toUpperCase();
  if (s === "A") return "A";
  if (s === "B") return "B";
  return "tie"; // covers "TIE", missing and invalid values
};
const cmpClip = (arr: string[], n: number) => arr.slice(0, n);
const cmpAdvBy = (x: number, y: number): CompareAdvantage => (x > y ? "A" : y > x ? "B" : "tie");

const CmpLoading = ({ stage }: { stage: number }) => (
  <div className="card section">
    <div className="row"><span className="spinner" /><b className="grow">Building comparison…</b></div>
    <div>
      <span className={`cmp-stage${stage <= 1 ? " active" : " done"}`}>
        <span className="cmp-dot" />{stage <= 1 ? "Gathering profiles & job" : "✓ Profiles & job gathered"}
      </span>
      <span className={`cmp-stage${stage === 2 ? " active" : ""}`}>
        <span className="cmp-dot" />{stage === 2 ? "AI judge weighing both candidates" : "AI judge"}
      </span>
    </div>
    <div className="prog-track"><div className="prog-fill" style={{ width: stage <= 1 ? "35%" : "80%" }} /></div>
    <div className="row" style={{ marginTop: 16, gap: 14 }}>
      <div className="skel" style={{ width: 200, height: 56 }} />
      <div className="skel" style={{ width: 200, height: 56 }} />
      <div className="skel grow" style={{ height: 56 }} />
    </div>
    <div className="skel" style={{ height: 90, marginTop: 12 }} />
  </div>
);

const CmpAdvBadge = ({ adv }: { adv: CompareAdvantage }) =>
  adv === "A" ? <span className="cmp-adv cmp-adv-a">▲ A better</span>
  : adv === "B" ? <span className="cmp-adv cmp-adv-b">▲ B better</span>
  : <span className="cmp-adv cmp-adv-tie">— tie</span>;

const heuristicCompare = (
  ca: Candidate, cb: Candidate, job: Job,
  reqSkills: string[], mustHaves: string[],
): CompareResultData => {
  const sA = cmpSkillList(ca).map(cmpNorm);
  const sB = cmpSkillList(cb).map(cmpNorm);
  const hits = (skills: string[], reqs: string[]) =>
    reqs.filter((r0) => skills.some((s) => s.includes(cmpNorm(r0)) || cmpNorm(r0).includes(s)));
  const hA = hits(sA, reqSkills), hB = hits(sB, reqSkills);
  const mA = hits(sA, mustHaves), mB = hits(sB, mustHaves);
  const shared = sA.filter((s) => sB.includes(s)).filter((s, i, arr) => arr.indexOf(s) === i);
  const onlyA = sA.filter((s) => !sB.includes(s)).filter((s, i, arr) => arr.indexOf(s) === i);
  const onlyB = sB.filter((s) => !sA.includes(s)).filter((s, i, arr) => arr.indexOf(s) === i);
  const rolesOf = (c: Candidate) =>
    (((c.roles ?? c.roleSuggestions ?? []) as any[]) ?? []).slice(0, 3).map((r) => String(r.role)).join(", ") || "—";
  const table: CompareRow[] = [];
  if (reqSkills.length) table.push({
    dimension: "Required-skills coverage",
    a: `${hA.length}/${reqSkills.length}: ${hA.join(", ") || "none seen"}`,
    b: `${hB.length}/${reqSkills.length}: ${hB.join(", ") || "none seen"}`,
    advantage: cmpAdvBy(hA.length, hB.length),
  });
  if (mustHaves.length) table.push({
    dimension: "Must-haves coverage",
    a: `${mA.length}/${mustHaves.length}: ${mA.join(", ") || "none seen"}`,
    b: `${mB.length}/${mustHaves.length}: ${mB.join(", ") || "none seen"}`,
    advantage: cmpAdvBy(mA.length, mB.length),
  });
  table.push({
    dimension: "Skills breadth",
    a: `${sA.length} skills listed`,
    b: `${sB.length} skills listed`,
    advantage: cmpAdvBy(sA.length, sB.length),
  });
  table.push({
    dimension: "Unique skills",
    a: onlyA.slice(0, 8).join(", ") || "None unique",
    b: onlyB.slice(0, 8).join(", ") || "None unique",
    advantage: cmpAdvBy(onlyA.length, onlyB.length),
  });
  table.push({ dimension: "Top roles", a: rolesOf(ca), b: rolesOf(cb), advantage: "tie" });
  if (shared.length) {
    table.push({ dimension: "Shared skills", a: shared.slice(0, 8).join(", "), b: shared.slice(0, 8).join(", "), advantage: "tie" });
  }
  // Fix 2: strengths/weaknesses derive from the PROFILES themselves (skills,
  // years, roles, education), not only required-skill coverage — that coverage
  // is empty when the job has no parsed requirements (the old "0-of-0" bug).
  let winner: "A" | "B" | "tie" = "tie";
  const lead = (hA.length + mA.length) - (hB.length + mB.length);
  if (lead !== 0) winner = lead > 0 ? "A" : "B";
  else if (onlyA.length !== onlyB.length) winner = onlyA.length > onlyB.length ? "A" : "B";
  const quickWins = (ownSkills: string[], otherSkills: string[]) =>
    [...new Set([
      ...hits(ownSkills, reqSkills).slice(0, 4),
      ...hits(ownSkills, mustHaves).slice(0, 3),
      ...ownSkills.filter((s) => !otherSkills.includes(s)).slice(0, 4),
    ])].slice(0, 6);
  const strengthsA = quickWins(sA, sB);
  const strengthsB = quickWins(sB, sA);
  const gaps = (ownSkills: string[]) =>
    [...new Set([...reqSkills, ...mustHaves])].filter((r0) =>
      !ownSkills.some((s) => s.includes(cmpNorm(r0)) || cmpNorm(r0).includes(s))).slice(0, 5);
  const weaknessesA = gaps(sA);
  const weaknessesB = gaps(sB);
  const emptyMsg = (c: Candidate) =>
    !cmpSkillList(c).length && !((c.profile ?? {}) as any).summary
      ? ["(profile has no extracted data — re-upload this resume)"]
      : [];
    const wA = strengthsA.length ? strengthsA : emptyMsg(ca);
    const wB = strengthsB.length ? strengthsB : emptyMsg(cb);
    const dA = weaknessesA.length ? weaknessesA : ["no obvious skill gaps against this job"];
    const dB = weaknessesB.length ? weaknessesB : ["no obvious skill gaps against this job"];
    const winnerName2 = winner === "A" ? cmpName(ca, "Candidate A") : winner === "B" ? cmpName(cb, "Candidate B") : "Neither candidate";
    const leadDesc = reqSkills.length
      ? `${winnerName2} ${winner === "tie" ? "~" : "leads"} on ${winner === "A" ? hA.length : winner === "B" ? hB.length : 0}-of-${reqSkills.length} required skills coverage`
      : `${winnerName2} ${winner === "tie" ? "~" : "leads"} on profile substance (${winner === "A" ? onlyA.length : winner === "B" ? onlyB.length : Math.max(onlyA.length, onlyB.length)} distinctive skills listed)`;
  return {
    table,
    strengths: { a: wA, b: wB },
    weaknesses: { a: dA, b: dB },
    verdict: {
      winner,
      confidence: "low",
      explanation: `Heuristic fallback (AI judge call failed) — deterministic comparison from profile-keyword overlap. ${leadDesc}.`,
    },
    source: "heuristic",
  };
};

const Compare = () => {
  const [cands, setCands] = useState<Candidate[]>([]);
  const [jobs, setJobs] = useState<Job[]>([]);
  const [cidA, setCidA] = useState("");
  const [cidB, setCidB] = useState("");
  const [jobId, setJobId] = useState("");
  const [rankings, setRankings] = useState<Record<string, { totalScore: number; band: string }>>({});
  // Quick (Gemini) scores per candidateId for the selected job — computed when Compare runs.
  const [quickScores, setQuickScores] = useState<Record<string, { score: number; error?: string }>>({});
  const [stage, setStage] = useState(1);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [result, setResult] = useState<CompareResultData | null>(null);

  useEffect(() => {
    api<{ candidates: Candidate[] }>("/candidates")
      .then(({ data }) => setCands(data.candidates ?? []))
      .catch((e) => setError(errText(e)));
    api<{ jobs: Job[] }>("/jobs")
      .then(({ data }) => setJobs(data.jobs ?? []))
      .catch((e) => setError(errText(e)));
  }, []);

  // Per-job match score chips (context only — the verdict itself is judged by the model)
  useEffect(() => {
    if (!jobId) { setRankings({}); return; }
    api<{ rankings: any[] }>(`/jobs/${jobId}/rankings`)
      .then(({ data }) => {
        const map: Record<string, { totalScore: number; band: string }> = {};
        (data.rankings ?? []).forEach((r) => {
          const cid = String(r.candidateId ?? "");
          const sc = Number(r.totalScore);
          if (cid && Number.isFinite(sc)) map[cid] = { totalScore: sc, band: String(r.band ?? "") };
        });
        setRankings(map);
      })
      .catch(() => setRankings({}));
  }, [jobId]);

  const ready = cands.filter((c) => c.status === "READY");
  const ca = cands.find((c) => String(c.candidateId) === cidA);
  const cb = cands.find((c) => String(c.candidateId) === cidB);
  const job = jobs.find((j) => String(j.jobId) === jobId);
  const canRun = Boolean(cidA) && Boolean(cidB) && cidA !== cidB && Boolean(jobId);
  const skiA = cmpSkillList(ca);
  const skiB = cmpSkillList(cb);
  const skiBSet = new Set(skiB.map(cmpNorm));
  const skiASet = new Set(skiA.map(cmpNorm));
  const shared = skiA.filter((s) => skiBSet.has(cmpNorm(s))).filter((s, i, arr) => arr.findIndex((x) => cmpNorm(x) === cmpNorm(s)) === i).slice(0, 12);
  const onlyA = skiA.filter((s) => !skiBSet.has(cmpNorm(s))).filter((s, i, arr) => arr.findIndex((x) => cmpNorm(x) === cmpNorm(s)) === i).slice(0, 12);
  const onlyB = skiB.filter((s) => !skiASet.has(cmpNorm(s))).filter((s, i, arr) => arr.findIndex((x) => cmpNorm(x) === cmpNorm(s)) === i).slice(0, 12);

  const run = async () => {
    if (!ca || !cb || !job) return;
    setBusy(true); setStage(1); setError(""); setResult(null);
    setQuickScores({});
    try {
      // Fix 1: run the same Gemini quick match used on the Match tab for both
      // candidates in parallel, so the score chips show real numbers instead of
      // "Not scored for this job yet" (backend rankings only exist after the
      // full scoring workflow ran).
      setQuickScores(() => ({}));
      geminiMatch(ca, job).then((r) => setQuickScores((prev) => ({ ...prev, [String(ca.candidateId)]: { score: r.score } })))
        .catch((e) => setQuickScores((prev) => ({ ...prev, [String(ca.candidateId)]: { score: 0, error: errText(e) } })));
      geminiMatch(cb, job).then((r) => setQuickScores((prev) => ({ ...prev, [String(cb.candidateId)]: { score: r.score } })))
        .catch((e) => setQuickScores((prev) => ({ ...prev, [String(cb.candidateId)]: { score: 0, error: errText(e) } })));

      const req = (job.parsedRequirements ?? {}) as Record<string, unknown>;
      const reqSkills = cmpAsArray(req.requiredSkills);
      const prefSkills = cmpAsArray(req.preferredSkills);
      const mustHaves = cmpAsArray(req.mustHaves);
      const jobBits = [
        `Job title: ${String(job.title ?? "(untitled)")}`,
        req.minYears != null ? `Minimum years: ${String(req.minYears)}` : "",
        req.seniority ? `Seniority: ${String(req.seniority)}` : "",
        req.education ? `Education: ${String(req.education)}` : "",
        req.domain ? `Domain: ${String(req.domain)}` : "",
        reqSkills.length ? `Required skills: ${reqSkills.join(", ")}` : "",
        prefSkills.length ? `Preferred skills: ${prefSkills.join(", ")}` : "",
        mustHaves.length ? `Must-haves: ${mustHaves.join(", ")}` : "",
      ].filter(Boolean).join("\n");
      const profileBlock = (c: Candidate) => {
        const p: any = c.profile ?? {};
        const skills = cmpSkillList(c).join(", ") || "N/A";
        const roles = ((c.roles ?? c.roleSuggestions ?? []) as any[]).slice(0, 3).map((r) => r.role).join(", ") || "N/A";
        return `Name: ${String(p.name ?? c.filename ?? c.candidateId)}. Skills/Tools/Languages: ${skills}. Top roles: ${roles}. Summary: ${String(p.summary ?? "")}.`;
      };

      setStage(2);
      let out: CompareResultData;
      if (!GEMINI_API_KEY) {
        // No key → heuristic fallback (still populated with real profile data).
        out = heuristicCompare(ca, cb, job, reqSkills, mustHaves);
      } else {
        const system = `You are a senior technical recruiter. Compare two candidates head-to-head for one specific job and reply with STRICT JSON only (no markdown, no extra text) in exactly this shape:
{"table":[{"dimension":"short dimension name","a":"candidate A's standing (1-2 sentences)","b":"candidate B's standing","advantage":"A"|"B"|"tie"}],"strengths":{"a":["..."],"b":["..."]},"weaknesses":{"a":["..."],"b":["..."]},"verdict":{"winner":"A"|"B"|"tie","confidence":"high"|"medium"|"low","explanation":"3-5 sentences justifying the decision against the specific job requirements"}}
Rules: table must contain 5-7 rows covering skills depth, experience relevance, seniority, education, domain fit and any red flags; advantage and verdict.winner must ONLY be A, B, or tie; keep each cell under 40 words; weaknesses must be candid and job-relevant; the explanation must reference the strongest differentiators by name.`;
        const user = `JOB\n${jobBits}\n\nCANDIDATE A\n${profileBlock(ca)}\n\nCANDIDATE B\n${profileBlock(cb)}`;
        const raw = await geminiJson(system, user, 2600);
        const parsed = parseModelJson(raw) as Record<string, unknown>;
        const rowsRaw = Array.isArray(parsed.table) ? (parsed.table as Record<string, unknown>[]) : [];
        const strengths = (parsed.strengths ?? {}) as Record<string, unknown>;
        const weaknesses = (parsed.weaknesses ?? {}) as Record<string, unknown>;
        const verdictRaw = (parsed.verdict ?? {}) as Record<string, unknown>;
        const conf = String(verdictRaw.confidence ?? "medium").toLowerCase();
        out = {
          table: rowsRaw.map((r0) => ({
            dimension: String(r0.dimension ?? "Dimension"),
            a: String(r0.a ?? ""),
            b: String(r0.b ?? ""),
            advantage: cmpAsAdvantage(r0.advantage),
          })),
          strengths: { a: cmpClip(cmpAsArray(strengths.a), 6), b: cmpClip(cmpAsArray(strengths.b), 6) },
          weaknesses: { a: cmpClip(cmpAsArray(weaknesses.a), 6), b: cmpClip(cmpAsArray(weaknesses.b), 6) },
          verdict: {
            winner: cmpAsAdvantage(verdictRaw.winner),
            confidence: conf === "high" || conf === "low" ? (conf as "high" | "low") : "medium",
            explanation: String(verdictRaw.explanation ?? "(No explanation returned by the model.)"),
          },
          source: "ai",
        };
        if (out.table.length === 0) throw new Error("The AI judge returned an empty comparison table — try again.");
      }
      setResult(out);
    } catch (e) {
      setError(errText(e));
    }
    setBusy(false);
  };

  const nameA = cmpName(ca, "Candidate A");
  const nameB = cmpName(cb, "Candidate B");
  const winnerName = result
    ? result.verdict.winner === "A" ? nameA : result.verdict.winner === "B" ? nameB : ""
    : "";

  const SideCard = ({ side, name, cid }: { side: "a" | "b"; name: string; cid: string }) => {
    const rk = rankings[cid];
    const qs = quickScores[cid];
    const band = qs && !qs.error ? scoreBand(qs.score) : null;
    return (
      <div className={`cmp-side${side === "b" ? " cmp-side-b" : ""}` + (side === "a" ? " cmp-side-a" : "")}>
        <div className="row">
          <span className="cmp-badge">{side.toUpperCase()}</span>
          <b className="grow">{name}</b>
        </div>
        <div className="row" style={{ marginTop: 8 }}>
          {rk ? (
            <span className="cmp-score-chip">Job score: {rk.totalScore}% · {rk.band}</span>
          ) : qs?.error ? (
            <span className="cmp-score-chip chip-bad" title={qs.error}>⚠ Quick score failed</span>
          ) : qs ? (
            <span className={`cmp-score-chip ${band?.chip ?? ""}`}>
              {band?.emoji} Quick score: {qs.score}% · {band?.label} fit
            </span>
          ) : (
            <span className="cmp-score-chip">Quick score…</span>
          )}
        </div>
      </div>
    );
  };

  return (
    <>
      <h1>Compare Candidates</h1>
      <p className="muted">
        Head-to-head comparison of two candidates for one job — judged by AI (the same model as Search),
        with skill overlap computed from the profiles in code.
      </p>
      <Banner msg={error} />

      <div className="card" style={{ marginTop: 14 }}>
        <div className="section-head"><span className="section-title">Pick the head-to-head pair</span></div>
        {ready.length < 2 && <Empty>Need at least two READY candidates — upload and process more resumes first.</Empty>}
        {ready.length >= 2 && jobs.length === 0 && <Empty>Need at least one job — create one in the Jobs tab first.</Empty>}
        {ready.length >= 2 && jobs.length > 0 && (
          <>
            <div className="cmp-selectors">
              <div className="cmp-select">
                <label className="cmp-label" style={{ "--side": "var(--cand-a)" } as React.CSSProperties}>Candidate A</label>
                <select className="select" value={cidA} onChange={(e) => setCidA(e.target.value)}>
                  <option value="">Choose candidate A…</option>
                  {ready.filter((c) => String(c.candidateId) !== cidB).map((c) => (
                    <option key={String(c.candidateId)} value={String(c.candidateId)}>{cmpName(c, String(c.candidateId))}</option>
                  ))}
                </select>
              </div>
              <button
                className="btn btn-ghost small cmp-swap"
                title="Swap candidates A and B"
                onClick={() => { setCidA(cidB); setCidB(cidA); }}
              >⇄</button>
              <div className="cmp-select">
                <label className="cmp-label" style={{ "--side": "var(--cand-b)" } as React.CSSProperties}>Candidate B</label>
                <select className="select" value={cidB} onChange={(e) => setCidB(e.target.value)}>
                  <option value="">Choose candidate B…</option>
                  {ready.filter((c) => String(c.candidateId) !== cidA).map((c) => (
                    <option key={String(c.candidateId)} value={String(c.candidateId)}>{cmpName(c, String(c.candidateId))}</option>
                  ))}
                </select>
              </div>
            </div>
            <div className="cmp-select cmp-job-select">
              <label className="cmp-label">Job</label>
              <select className="select" value={jobId} onChange={(e) => setJobId(e.target.value)}>
                <option value="">Choose a job…</option>
                {jobs.map((j) => (
                  <option key={String(j.jobId)} value={String(j.jobId)}>{String(j.title ?? "(untitled)")}</option>
                ))}
              </select>
            </div>
            <div className="row" style={{ marginTop: 14 }}>
              <button className="btn btn-primary" onClick={run} disabled={!canRun || busy}>
                {busy ? <><span className="spinner" /> Comparing…</> : "⚖️ Compare candidates"}
              </button>
              {cidA && cidA === cidB && <span className="chip chip-warn">Pick two different candidates</span>}
              {!jobId && <span className="small muted">Select a job to compare against.</span>}
            </div>
          </>
        )}
      </div>

      {busy && <CmpLoading stage={stage} />}

      {result && (
        <div className="card section" style={{ marginTop: 14 }}>
          <div className="cmp-h2h" style={{ marginBottom: 12 }}>
            <SideCard side="a" name={nameA} cid={cidA} />
            <div className="cmp-vs">VS</div>
            <SideCard side="b" name={nameB} cid={cidB} />
          </div>

          <div className="section-head"><span className="section-title">Skill overlap (computed from profiles)</span></div>
          <div className="row">
            <span className="small muted" style={{ minWidth: 62 }}>Shared</span>
            {shared.length ? shared.map((s) => <span className="chip chip-good" key={s}>{s}</span>) : <span className="small muted">none detected</span>}
          </div>
          <div className="row" style={{ marginTop: 6 }}>
            <span className="small muted" style={{ minWidth: 62 }}>Only A</span>
            {onlyA.length ? onlyA.map((s) => <span className="chip" style={{ borderColor: "var(--cand-a)", color: "var(--cand-a)" }} key={s}>{s}</span>) : <span className="small muted">none</span>}
          </div>
          <div className="row" style={{ marginTop: 6 }}>
            <span className="small muted" style={{ minWidth: 62 }}>Only B</span>
            {onlyB.length ? onlyB.map((s) => <span className="chip" style={{ borderColor: "var(--cand-b)", color: "var(--cand-b)" }} key={s}>{s}</span>) : <span className="small muted">none</span>}
          </div>

          <div style={{ marginTop: 16 }}>
            <div className="section-head"><span className="section-title">Dimension by dimension</span></div>
            <table className="table cmp-table">
              <thead>
                <tr><th style={{ width: "16%" }}>Dimension</th><th>{nameA}</th><th>{nameB}</th><th style={{ width: "9%" }}>Better?</th></tr>
              </thead>
              <tbody>
                {result.table.map((row, i) => (
                  <tr key={i} style={{ animationDelay: `${i * 55}ms` }}>
                    <td style={{ fontWeight: 600, color: "var(--text-hi)" }}>{row.dimension}</td>
                    <td className={row.advantage === "A" ? "cmp-win-a" : ""}>{row.a || "—"}</td>
                    <td className={row.advantage === "B" ? "cmp-win-b" : ""}>{row.b || "—"}</td>
                    <td><CmpAdvBadge adv={row.advantage} /></td>
                  </tr>
                ))}
              </tbody>
            </table>
            <div className="small muted" style={{ marginTop: 8 }}>
              {result.source === "heuristic"
                ? "⚠ AI judge unavailable — heuristic keyword comparison shown."
                : `Judged by Gemini (${GEMINI_MODEL}) — AI-generated, verify before use.`}
            </div>
          </div>

          <div className="cmp-twin">
            <div className="card">
              <div className="row"><span className="cmp-badge">A</span><h3 className="grow" style={{ margin: 0 }}>{nameA}</h3></div>
              <div className="section-head" style={{ marginTop: 10 }}><span className="section-title">Strengths</span></div>
              <ul className="cmp-list">
                {(result.strengths.a.length ? result.strengths.a : ["(none reported)"]).map((s, i) => (
                  <li key={i} style={{ animationDelay: `${i * 60}ms` }}>{s}</li>
                ))}
              </ul>
              <div className="section-head"><span className="section-title">Weaknesses</span></div>
              <ul className="cmp-list cmp-wk">
                {(result.weaknesses.a.length ? result.weaknesses.a : ["(none reported)"]).map((s, i) => (
                  <li key={i} style={{ animationDelay: `${i * 60}ms` }}>{s}</li>
                ))}
              </ul>
            </div>
            <div className="card cmp-side-b">
              <div className="row"><span className="cmp-badge">B</span><h3 className="grow" style={{ margin: 0 }}>{nameB}</h3></div>
              <div className="section-head" style={{ marginTop: 10 }}><span className="section-title">Strengths</span></div>
              <ul className="cmp-list">
                {(result.strengths.b.length ? result.strengths.b : ["(none reported)"]).map((s, i) => (
                  <li key={i} style={{ animationDelay: `${60 + i * 60}ms` }}>{s}</li>
                ))}
              </ul>
              <div className="section-head"><span className="section-title">Weaknesses</span></div>
              <ul className="cmp-list cmp-wk">
                {(result.weaknesses.b.length ? result.weaknesses.b : ["(none reported)"]).map((s, i) => (
                  <li key={i} style={{ animationDelay: `${60 + i * 60}ms` }}>{s}</li>
                ))}
              </ul>
            </div>
          </div>

          <div className="card cmp-verdict" style={{ marginTop: 14 }}>
            <div className="section-head"><span className="section-title">Final verdict</span></div>
            {result.verdict.winner === "tie" ? (
              <div className="cmp-winner" style={{ "--m-side": "var(--warn)" } as React.CSSProperties}>
                <div className="cmp-winner-badge">⚖️</div>
                <div>
                  <div className="cmp-winner-title">Too close to call</div>
                  <div className="small muted">Both candidates are viable — use the table above to break the tie.</div>
                </div>
              </div>
            ) : (
              <div
                className="cmp-winner"
                style={{ "--m-side": result.verdict.winner === "A" ? "var(--cand-a)" : "var(--cand-b)" } as React.CSSProperties}
              >
                <div className="cmp-winner-badge">🏆</div>
                <div className="grow">
                  <div className="cmp-winner-title">{winnerName} is the better fit</div>
                  <div className="row small muted" style={{ marginTop: 2 }}>
                    <span>Confidence:</span>
                    <div className="scorebar" style={{ maxWidth: 220 }}>
                      <div className="scorebar-track">
                        <div className="scorebar-fill" style={{
                          width: result.verdict.confidence === "high" ? "95%" : result.verdict.confidence === "medium" ? "65%" : "35%",
                        }} />
                      </div>
                      <span className="score-num small">{result.verdict.confidence}</span>
                    </div>
                  </div>
                </div>
              </div>
            )}
            <p style={{ marginTop: 12, color: "var(--text-hi)", lineHeight: 1.65 }}>{result.verdict.explanation}</p>
          </div>
        </div>
      )}
    </>
  );
};
