import { useEffect, useMemo, useRef, useState } from 'react';
import { Chat } from './Chat';
import { AnimatePresence, MotionConfig, motion } from 'motion/react';
import type { AuditImport, AuditRequirement, Availability, Course, Prereqs, Program, SchedulePreferenceProfile, Section, SuggestResponse, Suggestion, Term } from './types';
import { COLS, DEGREE_CREDITS, GEOM, bandH, nextTerm, pos } from './plan';
import './App.css';

const load = <T,>(f: string, fallback: T): Promise<T> => fetch(`/data/${f}`).then(r => (r.ok ? r.json() : fallback)).catch(() => fallback);
const store = <T,>(k: string, v?: T): T | undefined => {
  try { if (v !== undefined) localStorage.setItem(k, JSON.stringify(v)); else return JSON.parse(localStorage.getItem(k) ?? 'null') ?? undefined; } catch { /* ignore */ }
};

// --- motion tokens (MASTER.md) — tween only, one easing ---
const ease = [0.2, 0, 0, 1] as const;
const T = { fast: { duration: 0.12, ease }, base: { duration: 0.2, ease }, slow: { duration: 0.32, ease } };

// --- component recipes (MASTER.md) ---
const ring = 'outline-none focus-visible:ring-2 focus-visible:ring-accent/40';
const btn = `inline-flex h-7 shrink-0 items-center gap-1 rounded-md px-2 text-[13px] font-medium text-ink transition hover:bg-surface-hover active:bg-line disabled:opacity-40 disabled:hover:bg-transparent ${ring}`;
const primary = `inline-flex h-7 items-center rounded-md bg-accent px-2.5 text-[13px] font-medium text-white transition hover:bg-accent-hover active:bg-accent-hover disabled:opacity-40 disabled:hover:bg-accent ${ring}`;
const icon = `grid h-7 w-7 shrink-0 place-items-center rounded-md text-ink-2 transition hover:bg-surface-hover hover:text-ink active:bg-line disabled:opacity-40 disabled:hover:bg-transparent ${ring}`;
const field = `h-7 rounded-md border border-line bg-canvas px-2 text-[13px] text-ink transition placeholder:text-ink-3 hover:border-line-strong focus:border-accent ${ring} focus-visible:ring-accent/25`;
const tag = 'rounded-sm bg-accent-soft px-1 text-[11px] font-medium text-accent';

// --- schedules: times are minutes past midnight, so overlap is one comparison ---
const DAYS = [['Mo', 'M'], ['Tu', 'T'], ['We', 'W'], ['Th', 'Th'], ['Fr', 'F']] as const;
const NO_AVAIL: Availability = { busy: [], earliest: 0, latest: 24 * 60 };
/** 820 -> "1:40PM" */
const clock = (m: number) => `${((m / 60 | 0) - 1) % 12 + 1}:${`${m % 60}`.padStart(2, '0')}${m < 720 ? 'AM' : 'PM'}`;
const hhmm = (m: number) => `${`${m / 60 | 0}`.padStart(2, '0')}:${`${m % 60}`.padStart(2, '0')}`;   // for <input type="time">
const mins = (v: string) => { const [h, m] = v.split(':').map(Number); return h * 60 + m; };
/** Show the complete registration when a lecture has a linked lab/recitation. */
const oneMeet = (s: Section) => (s.start === null ? (s.raw || 'time TBA') : `${s.days} ${clock(s.start)}–${clock(s.end!)}`);
const meets = (s: Section) => s.components?.length ? s.components.map(oneMeet).join(' + ') : oneMeet(s);

type CourseLocation =
  | { kind: 'term'; index: number }
  | { kind: 'proposal' }
  | { kind: 'queue' };

type DraggedCourse = {
  id: string;
  from: CourseLocation;
};

type SubjectPreferences = { preferredSubjects: string[]; avoidedSubjects: string[] };
const NO_PREFERENCES: SubjectPreferences = { preferredSubjects: [], avoidedSubjects: [] };
const WEIGHT_LABELS: Record<string, string> = { campus_days: 'Fewer campus days', gap_minutes: 'Shorter gaps', early_minutes: 'Avoid early classes', late_minutes: 'Avoid late classes', campus_span_minutes: 'Shorter campus days' };
const hours = (m: number | undefined) => `${Math.round(((m ?? 0) / 60) * 10) / 10}h`;

const PanelIcon = ({ side }: { side: 'left' | 'right' }) => (
  <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" aria-hidden="true">
    <rect x="1.75" y="2.75" width="12.5" height="10.5" rx="2" />
    <path d={side === 'left' ? 'M6 2.75v10.5' : 'M10 2.75v10.5'} />
  </svg>
);

export default function App() {
  const [programs, setPrograms] = useState<Program[]>([]);
  const [courses, setCourses] = useState<Map<string, Course>>(new Map());
  const [prereqs, setPrereqs] = useState<Prereqs>({});
  const [source, setSource] = useState<Record<string, string>>({});   // courseId -> where its prereqs came from (verified)
  const [coreqs, setCoreqs] = useState<Set<string>>(new Set());       // may be taken the same term as a prereq
  const [notice, setNotice] = useState('');                           // transient warning (dupe add, etc.)
  const [dismissedWarn, setDismissedWarn] = useState('');
  const [gened, setGened] = useState<{ labels: Record<string, string>; courses: Record<string, string[]> }>({ labels: {}, courses: {} });
  const [filter, setFilter] = useState({ subject: '', pathway: '', eligible: false });   // add-course box
  const [pid, setPid] = useState<string>(() => store('program') ?? 'CSCI-BS');
  const [breaks, setBreaks] = useState(false);
  const [avail, setAvail] = useState<Availability>(() => store<Availability>('avail') ?? NO_AVAIL);
  const [preferences, setPreferences] = useState<SubjectPreferences>(() => store<SubjectPreferences>('preferences') ?? NO_PREFERENCES);
  const [schedulePrompt, setSchedulePrompt] = useState<string>(() => store<string>('schedulePrompt') ?? '');
  const [scheduleProfile, setScheduleProfile] = useState<SchedulePreferenceProfile | null>(() => store<SchedulePreferenceProfile>('scheduleProfile') ?? null);
  const [scheduleIndex, setScheduleIndex] = useState(0);
  const [preferenceLoading, setPreferenceLoading] = useState(false);
  const [feedbackOpen, setFeedbackOpen] = useState(false);
  const [scheduleFeedback, setScheduleFeedback] = useState('');
  const [terms, setTerms] = useState<Term[]>(() => store<Term[]>(`terms:${store('program') ?? 'CSCI-BS'}`) ?? []);
  const [proposal, setProposal] = useState<Suggestion[]>([]);
  // pins: courses locked into the current proposal (survive Generate with Gemini). queue: wanted later — promoted to a pin the first term they're eligible.
  const [pins, setPins] = useState<string[]>(() => store(`pins:${store('program') ?? 'CSCI-BS'}`) ?? []);
  const [queue, setQueue] = useState<string[]>(() => store(`queue:${store('program') ?? 'CSCI-BS'}`) ?? []);
  const [auditRequirements, setAuditRequirements] = useState<AuditRequirement[]>(() => store(`auditReqs:${store('program') ?? 'CSCI-BS'}`) ?? []);
  // ignored: courses whose prerequisite warning the student chose to ignore (the card stops being red, Approve is unblocked)
  const [ignored, setIgnored] = useState<string[]>(() => store(`ignored:${store('program') ?? 'CSCI-BS'}`) ?? []);
  const [resp, setResp] = useState<SuggestResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [focus, setFocus] = useState<string | null>(null);
  const [adding, setAdding] = useState('');
  const [importing, setImporting] = useState(false);
  const [left, setLeft] = useState<boolean>(() => store('ui:left') ?? true);
  const [right, setRight] = useState<boolean>(() => store('ui:right') ?? true);
  const [chat, setChat] = useState(false);                          // advisor chatbot (Gemini) open
  const [view, setView] = useState({ x: 0, y: 0, s: 1 });          // diagram pan/zoom
  const auditInput = useRef<HTMLInputElement>(null);
  const canvas = useRef<HTMLDivElement>(null);
  const drag = useRef<{ px: number; py: number; x: number; y: number } | null>(null);
  const draggedCourse = useRef<DraggedCourse | null>(null);
  const suppressCourseClick = useRef(false);
  const [dropTarget, setDropTarget] = useState<number | null>(null);
  const latest = useRef({ pins, queue, courses });
  useEffect(() => { latest.current = { pins, queue, courses }; });
  const leaveTimer = useRef(0);
  const [more, setMore] = useState(false);   // "+N more" ghost dropdown open; keeps the hover focus alive
  const moreRef = useRef(false);
  useEffect(() => { moreRef.current = more; }, [more]);

  // hover focus is sticky for a beat so the pointer can travel from a card to its ghost suggestions
  const enter = (id: string) => { window.clearTimeout(leaveTimer.current); setFocus(f => { if (f !== id) setMore(false); return id; }); };
  const leave = () => { window.clearTimeout(leaveTimer.current); leaveTimer.current = window.setTimeout(() => { if (!moreRef.current) setFocus(null); }, 250); };

  // wheel = pan, ctrl/cmd+wheel (or trackpad pinch) = zoom about the cursor
  useEffect(() => {
    const el = canvas.current!;
    const onWheel = (e: WheelEvent) => {
      e.preventDefault();
      const r = el.getBoundingClientRect(), mx = e.clientX - r.left, my = e.clientY - r.top;
      setView(v => {
        if (!(e.ctrlKey || e.metaKey)) return { ...v, x: v.x - e.deltaX, y: v.y - e.deltaY };
        const s = Math.min(3, Math.max(0.25, v.s * Math.exp(-e.deltaY * 0.01)));
        return { s, x: mx - (mx - v.x) * s / v.s, y: my - (my - v.y) * s / v.s };
      });
    };
    el.addEventListener('wheel', onWheel, { passive: false });
    return () => el.removeEventListener('wheel', onWheel);
  }, []);
  const zoomBy = (f: number) => setView(v => {
    const el = canvas.current!, cx = el.clientWidth / 2, cy = el.clientHeight / 2, s = Math.min(3, Math.max(0.25, v.s * f));
    return { s, x: cx - (cx - v.x) * s / v.s, y: cy - (cy - v.y) * s / v.s };
  });

  useEffect(() => {
    load<Program[]>('programs.json', []).then(setPrograms);
    load<Course[]>('courses.json', []).then(cs => setCourses(new Map(cs.map(c => [c.id, c]))));
    load<Prereqs>('prereqs.json', {}).then(setPrereqs);
    load<Record<string, string>>('prereq_source.json', {}).then(setSource);
    load<string[]>('coreqs.json', []).then(l => setCoreqs(new Set(l)));
    fetch('/api/gened').then(r => r.json()).then(setGened).catch(() => { /* filter just shows no Pathways options */ });
  }, []);
  useEffect(() => {
    if (store('program') !== pid) {
      store('program', pid);
      setTerms(store<Term[]>(`terms:${pid}`) ?? []);
      setPins(store(`pins:${pid}`) ?? []);
      setQueue(store(`queue:${pid}`) ?? []);
      setIgnored(store(`ignored:${pid}`) ?? []);
      setAuditRequirements(store<AuditRequirement[]>(`auditReqs:${pid}`) ?? []);
    }
  }, [pid]);
  useEffect(() => { store(`pins:${pid}`, pins); store(`queue:${pid}`, queue); store(`ignored:${pid}`, ignored); }, [pid, pins, queue, ignored]);
  useEffect(() => { store('ui:left', left); store('ui:right', right); }, [left, right]);
  useEffect(() => { store('schedulePrompt', schedulePrompt); store('scheduleProfile', scheduleProfile); }, [schedulePrompt, scheduleProfile]);
  useEffect(() => {
    if (!notice.startsWith('Imported ')) return;
    const timer = window.setTimeout(() => setNotice(''), 8000);
    return () => window.clearTimeout(timer);
  }, [notice]);

  // Ctrl/Cmd + [  -> left panel,  Ctrl/Cmd + ]  -> right panel
  useEffect(() => {
    const isMac = /Mac|iPhone|iPad/.test(navigator.userAgent);
    const onKey = (e: KeyboardEvent) => {
      if (!(isMac ? e.metaKey : e.ctrlKey)) return;
      if (e.key === '[') { e.preventDefault(); setLeft(v => !v); }
      if (e.key === ']') { e.preventDefault(); setRight(v => !v); }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, []);

  const program = programs.find(p => p.id === pid);
  const taken = useMemo(() => terms.flatMap(t => t.courses), [terms]);
  const current = useMemo(() => nextTerm(terms, breaks), [terms, breaks]);
  const credits = (ids: string[]) => ids.reduce((s, id) => s + (courses.get(id)?.credits ?? 0), 0);
  // done = what DegreeWorks calls complete: credit total AND every major rule AND every Pathways slot
  const extra = terms.reduce((s, t) => s + (t.extra ?? 0), 0);
  const done = !!resp && credits(taken) + extra >= DEGREE_CREDITS
    && resp.progress.major.every(m => m.have >= m.need) && resp.progress.pathways.every(s => s.course);
  /** Has the student actually narrowed anything? If not we send no `avail` at all, so the filter is inert. */
  const availNarrowed = avail.busy.length > 0 || avail.earliest > 0 || avail.latest < 24 * 60;
  /** True once we are past the term the registrar has published: times are then a season pattern, not a booking. */
  const pattern = resp?.schedule?.basis === 'pattern';
  const scheduleOptions = resp?.optimizer?.schedules ?? [];
  const activeSchedule = scheduleOptions[scheduleIndex] ?? null;

  // --- prerequisite status of a proposed course, mirroring server.py (placement, coreq, verified) ---
  const num = (id: string) => Number((courses.get(id)?.code.split(' ')[1] ?? '').match(/\d+/)?.[0] ?? 0);
  const level = (id: string) => { const n = num(id); return n >= 1000 ? Math.floor(n / 10) : n; };
  const placement = (g: string[]) => g.every(p => courses.get(p)?.subject === 'MATH' && (num(p) < 120 || num(p) === 122));
  const verified = (id: string) => id in source || !!prereqs[id]?.length || level(id) < 200;
  /** ok: every prereq group met by an approved term; partial: some met (yellow); missing: none met (red). */
  const status = (id: string, same: string[]): 'ok' | 'partial' | 'missing' => {
    const groups = prereqs[id] ?? [];
    const met = groups.filter(g => g.some(p => taken.includes(p)) || placement(g) || (coreqs.has(id) && g.some(p => same.includes(p))));
    return met.length === groups.length ? 'ok' : met.length ? 'partial' : 'missing';
  };
  const proposalIds = proposal.map(p => p.id);
  const blocked = proposal.filter(p => !ignored.includes(p.id) && status(p.id, proposalIds) !== 'ok');
  const ignore = (id: string) => { if (!ignored.includes(id)) setIgnored([...ignored, id]); };

  const proposalForSchedule = (r: SuggestResponse, index: number) => {
    const schedule = r.optimizer?.schedules?.[index];
    if (!schedule) return r.suggested;
    const selected = new Map(schedule.sections.map(item => [item.course_id, item.section]));
    const base = new Map<string, Suggestion>();
    r.candidates.forEach(c => base.set(c.id, c));
    r.suggested.forEach(c => base.set(c.id, c));
    return r.candidates.filter(c => selected.has(c.id)).map(c => ({ ...(base.get(c.id) ?? c), sections: [selected.get(c.id)!] }));
  };

  /** Promote newly eligible queued courses to pins, and show optimizer schedule #1. */
  const merge = (r: SuggestResponse) => {
    const { pins, queue } = latest.current;
    const eligible = new Set(r.candidates.map(c => c.id));
    setPins([...new Set([...pins, ...queue])].filter(id => eligible.has(id)));
    setQueue(queue.filter(id => !eligible.has(id)));
    setScheduleIndex(0); setFeedbackOpen(false);
    return proposalForSchedule(r, 0);
  };

  const selectSchedule = (index: number) => {
    if (!resp || index < 0 || index >= scheduleOptions.length) return;
    setScheduleIndex(index); setProposal(proposalForSchedule(resp, index)); setFeedbackOpen(false); setScheduleFeedback('');
  };

  const interpretSchedulePreferences = async (feedback = '') => {
    if (!schedulePrompt.trim() && !feedback.trim()) return setNotice('Tell Gemini what matters to you first.');
    setPreferenceLoading(true); setError(''); setNotice('');
    try {
      const response = await fetch('/api/schedule-preferences', {
        method: 'POST', headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ prompt: schedulePrompt, profile: scheduleProfile, feedback: feedback || undefined, scheduleMetrics: activeSchedule?.metrics }),
      });
      const data = await response.json();
      if (!response.ok || data.error) throw new Error(data.error || `${response.status}`);
      setScheduleProfile(data as SchedulePreferenceProfile); setScheduleIndex(0); setFeedbackOpen(false); setScheduleFeedback('');
      setNotice(feedback ? 'Gemini updated the schedule priorities from your feedback.' : 'Gemini translated your schedule preferences.');
    } catch (e) {
      setError(`Schedule preference update failed (${e instanceof Error ? e.message : 'unknown error'}).`);
    } finally { setPreferenceLoading(false); }
  };

  // ask the backend for the next semester whenever the approved terms change (pins/queue ride along). The automatic request is
  // rule-based (ai: false, instant, no Gemini credits); only "Generate with Gemini" sends ai + fresh (bypasses the server cache).
  const fresh = useRef(false);
  const ai = useRef(false);
  useEffect(() => {
    if (!program || done) return;
    const ctl = new AbortController();
    const { pins, queue } = latest.current;
    const hasPreferences = preferences.preferredSubjects.length > 0 || preferences.avoidedSubjects.length > 0;
    const body = { program: pid, terms: terms.map(t => t.courses), term: current.kind, pins, queue, auditRequirements,
                   fresh: fresh.current, ai: ai.current, avail: availNarrowed ? avail : null, preferences: hasPreferences ? preferences : undefined,
                   scheduleProfile: scheduleProfile ?? undefined };
    fresh.current = false; ai.current = false;
    setLoading(true); setError('');
    fetch('/api/suggest', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(body), signal: ctl.signal })
      .then(r => { if (!r.ok) throw new Error(`${r.status}`); return r.json() as Promise<SuggestResponse>; })
      .then(r => { setResp(r); setProposal(merge(r)); setLoading(false); })
      .catch(e => { if (e.name !== 'AbortError') { setError(`Backend not reachable (${e.message}). Run: python backend/server.py`); setLoading(false); } });
    return () => ctl.abort();
  }, [pid, taken, current.kind, !!program, avail, preferences, auditRequirements, scheduleProfile]);   // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => { store('avail', avail); }, [avail]);
  useEffect(() => { store('preferences', preferences); }, [preferences]);

  const approve = () => { if (blocked.length) return; const t = [...terms, { ...current, courses: proposal.map(p => p.id) }]; setTerms(t); store(`terms:${pid}`, t); setPins([]); };
  const reset = () => { setTerms([]); store(`terms:${pid}`, []); setResp(null); setPins([]); setQueue([]); setIgnored([]); setAuditRequirements([]); store(`auditReqs:${pid}`, []); };
  const undo = () => { const t = terms.slice(0, -1); setTerms(t); store(`terms:${pid}`, t); setPins([]); };
  const generate = () => { fresh.current = true; ai.current = true; setTerms([...terms]); };   // re-triggers the effect with Gemini, bypassing the server cache; pins survive
  const remove = (id: string) => { setProposal(proposal.filter(p => p.id !== id)); setPins(pins.filter(p => p !== id)); };
  const togglePin = (id: string) => setPins(pins.includes(id) ? pins.filter(p => p !== id) : [...pins, id]);
  const eligibleNow = (id: string) => resp?.candidates.find(c => c.id === id);
  /** One place only: put `id` in the proposal (pinned) if eligible this term, else in the queue. */
  const want = (id: string) => {
    if (taken.includes(id)) return;
    const c = eligibleNow(id);
    if (c) { if (!proposal.some(p => p.id === id)) setProposal([...proposal, c]); if (!pins.includes(id)) setPins([...pins, id]); setQueue(queue.filter(q => q !== id)); }
    else if (!queue.includes(id)) setQueue([...queue, id]);
  };
  const unqueue = (id: string) => setQueue(queue.filter(q => q !== id));
  /** Ghosts are unlocked by `src` in the proposed term: pin `src` (so Generate with Gemini keeps the prerequisite) and queue the ghost for the next eligible term. */
  const enqueue = (id: string, src: string) => {
    if (!pins.includes(src)) setPins([...pins, src]);
    if (!taken.includes(id) && !queue.includes(id)) setQueue([...queue, id]);
  };
  const label = (c: Course) => `${c.code} — ${c.name}`;
  const subjects = useMemo(() => [...new Set([...courses.values()].map(c => c.subject))].sort(), [courses]);
  const preferenceCount = preferences.preferredSubjects.length + preferences.avoidedSubjects.length;
  const addPreference = (kind: keyof SubjectPreferences, subject: string) => {
    if (!subject) return;
    const other: keyof SubjectPreferences = kind === 'preferredSubjects' ? 'avoidedSubjects' : 'preferredSubjects';
    setPreferences(p => ({ ...p, [kind]: [...new Set([...p[kind], subject])], [other]: p[other].filter(s => s !== subject) }));
  };
  const removePreference = (kind: keyof SubjectPreferences, subject: string) =>
    setPreferences(p => ({ ...p, [kind]: p[kind].filter(s => s !== subject) }));
  const addable = [...courses.values()].filter(c => (!filter.subject || c.subject === filter.subject)
    && (!filter.pathway || gened.courses[c.id]?.includes(filter.pathway)) && (!filter.eligible || eligibleNow(c.id)));
  /** Any catalog course can be added; eligibility is shown on the card (red/yellow) and gates Approve. Duplicates warn instead. */
  const add = (text: string) => {
    const c = [...courses.values()].find(c => label(c) === text);
    if (!c) return;
    setAdding('');
    if (proposal.some(p => p.id === c.id)) return setNotice(`${c.code} is already in ${current.name}.`);
    if (taken.includes(c.id)) return setNotice(`${c.code} is already in an approved term.`);
    setNotice('');
    setProposal([...proposal, resp?.candidates.find(x => x.id === c.id) ?? { id: c.id, reason: 'Added by you', unlocks: [], verified: verified(c.id), source: source[c.id] ?? null, sections: null }]);
  };
  const importAudit = async (file: File | null) => {
    if (!file) return;
    setImporting(true); setError(''); setNotice('');
    try {
      const body = new FormData();
      body.append('audit', file);
      const r = await fetch('/api/audit', { method: 'POST', body });
      const audit = await r.json() as AuditImport;
      if (!r.ok || audit.error) throw new Error(audit.error || `${r.status}`);
      if (!audit.program) throw new Error(`Could not match "${audit.major ?? 'audit major'}" to a planner major.`);
      audit.terms = audit.terms.map(t => ({ ...t, imported: true }));   // the registrar already accepted these — never flag prereqs
      store('program', audit.program);
      store(`terms:${audit.program}`, audit.terms);
      store(`ignored:${audit.program}`, []);
      store(`pins:${audit.program}`, []);
      store(`queue:${audit.program}`, []);
      store(`auditReqs:${audit.program}`, audit.completedRequirements ?? []);
      setPid(audit.program);
      setTerms(audit.terms);
      setPins([]);
      setQueue([]);
      setIgnored([]);
      setAuditRequirements(audit.completedRequirements ?? []);
      setProposal([]);
      setResp(null);
      setNotice(`Imported ${audit.courses.length} completed courses and ${(audit.completedRequirements ?? []).length} checked requirements from ${audit.major ?? 'DegreeWorks'}.`);
    } catch (e) {
      setError(`DegreeWorks import failed (${e instanceof Error ? e.message : 'unknown error'}).`);
    } finally {
      setImporting(false);
      if (auditInput.current) auditInput.current.value = '';
    }
  };

  // --- DAG geometry: one band per approved term (top to bottom), then the proposal band, then the queue band ---
  const levels: { name: string; kind: string; ids: string[]; proposed?: boolean; queued?: boolean; transfers?: Record<string, string>; cr?: number }[] = [
    ...terms.map(t => ({ name: t.name, kind: t.kind, ids: [...new Set(t.courses)], transfers: t.transfers, cr: credits(t.courses) + (t.extra ?? 0) })),   // one card per course; credits count every row
    ...(done ? [] : [{ name: current.name, kind: current.kind, ids: proposal.map(p => p.id), proposed: true }]),
    ...(queue.length ? [{ name: 'Queued', kind: 'queue', ids: queue, queued: true }] : []),
  ];
  const moveCourse = (id: string, target: CourseLocation) => {
    const dragged = draggedCourse.current;
    if (!dragged || dragged.id !== id) return;

    const sameLocation =
      dragged.from.kind === target.kind &&
      (dragged.from.kind !== 'term' ||
        (target.kind === 'term' && dragged.from.index === target.index));

    if (sameLocation) return;

    const originalSuggestion = proposal.find(p => p.id === id);

    // Remove the course from every possible location first.
    const updatedTerms = terms.map(term => ({
      ...term,
      courses: term.courses.filter(courseId => courseId !== id),
    }));

    let updatedProposal = proposal.filter(p => p.id !== id);
    let updatedQueue = queue.filter(courseId => courseId !== id);

    // Add it to the selected destination.
    if (target.kind === 'term') {
      updatedTerms[target.index] = {
        ...updatedTerms[target.index],
        courses: [...updatedTerms[target.index].courses, id],
      };
    } else if (target.kind === 'proposal') {
      const suggestion =
        originalSuggestion ??
        resp?.candidates.find(candidate => candidate.id === id) ?? {
          id,
          reason: 'Moved by you',
          unlocks: [],
          verified: verified(id),
          source: source[id] ?? null,
          sections: null,
        };

      updatedProposal = [...updatedProposal, suggestion];
    } else {
      updatedQueue = [...updatedQueue, id];
    }

    setTerms(updatedTerms);
    store(`terms:${pid}`, updatedTerms);
    setProposal(updatedProposal);
    setQueue(updatedQueue);

    // A course moved out of the proposal should no longer remain pinned there.
    if (target.kind !== 'proposal') {
      setPins(currentPins => currentPins.filter(pin => pin !== id));
    }

    setNotice('');
  };
  const place = new Map<string, { level: number; i: number }>();
  levels.forEach((l, level) => l.ids.forEach((id, i) => place.set(id, { level, i })));
  const edges: { from: string; to: string }[] = [];
  for (const [id, p] of place) for (const g of prereqs[id] ?? []) for (const q of g) if (place.has(q) && place.get(q)!.level < p.level) edges.push({ from: q, to: id });
  const widest = Math.min(COLS, Math.max(1, ...levels.map(l => l.ids.length)));
  const W = GEOM.PAD * 2 + widest * (GEOM.CARD_W + GEOM.COL_GAP) + GEOM.LABEL_W;
  const tops: number[] = [];
  levels.reduce((y, l) => { tops.push(y); return y + bandH(l.ids.length) + GEOM.ROW_GAP; }, GEOM.PAD);
  const H = levels.length ? tops[levels.length - 1] + bandH(levels[levels.length - 1].ids.length) + GEOM.PAD : GEOM.PAD * 2 + bandH(0);
  const at = (id: string) => { const p = place.get(id)!; return pos(tops[p.level], p.i, levels[p.level].ids.length, W); };
  const proposalCredits = credits(proposal.map(p => p.id));
  const courseCodes = (ids: string[]) => ids.map(id => courses.get(id)?.code ?? id).join(' or ');
  const auditReqText = (a: AuditRequirement) => courseCodes(a.courses);
  const hot = focus ? new Set([focus, ...(prereqs[focus] ?? []).flat(), ...(resp?.candidates.find(c => c.id === focus)?.unlocks ?? [])]) : null;
  // audit-imported terms are the registrar's record, not a plan: never flag their prerequisites. Ignored courses are the student's call.
  const importedIds = new Set(terms.filter(t => t.imported).flatMap(t => t.courses));
  const violations = new Map((resp?.violations ?? []).filter(v => !importedIds.has(v.id) && !ignored.includes(v.id)).map(v => [v.id, v]));
  const unverified = new Set([...place.keys()].filter(id => courses.has(id) && !verified(id)));
  const rawWarn = error || notice || (blocked.length ? `Can't approve ${current.name}: ${blocked.map(p => {
    const c = courses.get(p.id), g = (prereqs[p.id] ?? []).filter(g => !g.some(q => taken.includes(q)) && !placement(g));
    return `${c?.code} needs ${g.map(x => x.map(q => courses.get(q)?.code).join(' or ')).join(' and ')} in an earlier term`;
  }).join(' · ')}.` : '')
    || (violations.size ? `Prerequisite problems in approved terms: ${[...violations.values()].map(v => `${courses.get(v.id)?.code} needs ${v.missing.map(m => courses.get(m)?.code).join(' or ')} first`).join(' · ')}. Use "Undo" or "Start over".` : '');
  const warn = rawWarn === dismissedWarn ? '' : rawWarn;
  /** Every red/yellow card on the board: blocked proposals, approved-term violations, and unverified-prereq courses. */
  const flagged = [...new Set([...blocked.map(p => p.id), ...violations.keys(), ...[...unverified].filter(id => !ignored.includes(id))])];
  const ignoreAll = () => setIgnored([...new Set([...ignored, ...flagged])]);
  const dismissWarn = () => {
    if (rawWarn === notice) setNotice('');
    else if (rawWarn === error) setError('');
    else setDismissedWarn(rawWarn);
  };
  const totalCredits = credits(taken) + extra;   // counted here, not server-side: the server dedupes `taken`, the audit does not

  // --- ghosts: up to 3 not-yet-planned courses the hovered one unlocks, fanned below it in a half-wheel ---
  const unlockedBy = useMemo(() => {
    const m = new Map<string, string[]>();
    for (const [id, groups] of Object.entries(prereqs)) for (const q of groups.flat()) m.set(q, [...(m.get(q) ?? []), id]);
    return m;
  }, [prereqs]);
  // Only the proposal band (the term awaiting approval) grows ghosts. Slots 1-2 are courses; slot 3 is a "+N more" dropdown when the pool is bigger than 3.
  const { ghosts, rest } = (() => {
    if (!focus || !place.has(focus) || !proposal.some(p => p.id === focus)) return { ghosts: [], rest: [] as string[] };
    const fromServer = resp?.candidates.find(c => c.id === focus)?.unlocks;
    const pool = (fromServer?.length ? fromServer : (unlockedBy.get(focus) ?? []).filter(id => courses.has(id))).filter(id => !place.has(id) && !taken.includes(id));
    const ids = pool.slice(0, 3), rest = pool.length > 3 ? pool.slice(2) : [];
    const { x, y } = at(focus), cx = x + GEOM.CARD_W / 2, cy = y + GEOM.CARD_H, R = 100;
    return {
      rest, ghosts: ids.map((id, i) => {
        const a = ids.length === 1 ? 0 : (i / (ids.length - 1) - 0.5) * (Math.PI * 0.6);   // fan across ±54°
        return { id, src: focus, more: i === 2 && rest.length > 0, x: cx + R * Math.sin(a) - GEOM.CARD_W / 2, y: cy + R * Math.cos(a) - GEOM.CARD_H / 2, cx, cy };
      })
    };
  })();

  return (
    <MotionConfig reducedMotion="user">
      <div className="flex h-screen flex-col">
        <header className="flex h-11 shrink-0 items-center gap-2 border-b border-line bg-canvas px-2">
          <button className={`${icon} ${left ? 'text-ink' : ''}`} onClick={() => setLeft(v => !v)} aria-pressed={left} aria-label="Toggle plan panel" title="Toggle plan panel (Ctrl+[)"><PanelIcon side="left" /></button>
          <span className="px-1 text-[13px] font-medium">Degree Planner</span>
          <span className="text-ink-3" aria-hidden="true">/</span>
          <input list="programs" placeholder="Search a major…" defaultValue={program ? `${program.name} (${program.degree})` : ''} className={`${field} w-full max-w-sm`} aria-label="Major"
            onChange={e => { const p = programs.find(p => `${p.name} (${p.degree})` === e.target.value); if (p) setPid(p.id); }} />
          <datalist id="programs">{programs.map(p => <option key={p.id} value={`${p.name} (${p.degree})`} />)}</datalist>
          <input ref={auditInput} type="file" accept="application/pdf,.pdf" className="hidden" onChange={e => importAudit(e.target.files?.[0] ?? null)} />
          <button className={btn} onClick={() => auditInput.current?.click()} disabled={importing} title="Import completed courses from a DegreeWorks PDF">
            {importing ? 'Importing…' : 'Import audit'}
          </button>
          <label className={`${btn} cursor-pointer font-normal text-ink-2`}><input type="checkbox" className="accent-accent" checked={breaks} onChange={e => setBreaks(e.target.checked)} /> Summer / Winter</label>
          <div className="ml-auto flex items-center gap-3 text-[13px]">
            <span className="text-ink-2 tabular-nums">{terms.length} {terms.length === 1 ? 'term' : 'terms'}</span>
            <div className="h-1 w-28 overflow-hidden rounded-full bg-line" role="progressbar" aria-valuenow={totalCredits} aria-valuemax={DEGREE_CREDITS} aria-label="Credits">
              <motion.div className="h-full origin-left rounded-full bg-accent" animate={{ scaleX: Math.min(1, totalCredits / DEGREE_CREDITS) }} transition={T.slow} />
            </div>
            <span className="font-medium tabular-nums">{totalCredits} <span className="text-ink-3">/ {DEGREE_CREDITS}</span></span>
          </div>
          <button className={`${btn} ${chat ? 'bg-surface-hover' : ''}`} onClick={() => setChat(v => !v)} aria-pressed={chat} title="Ask the Gemini advisor about your plan">Advisor</button>
          <button className={`${icon} ${right ? 'text-ink' : ''}`} onClick={() => setRight(v => !v)} aria-pressed={right} aria-label="Toggle requirements panel" title="Toggle requirements panel (Ctrl+])"><PanelIcon side="right" /></button>
        </header>

        <AnimatePresence>
          {warn && (
            <motion.div key="warn" role="alert" initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }} transition={T.base}
              className="flex shrink-0 items-center gap-3 border-b border-line bg-warning-soft px-4 py-2 text-[13px] text-warning">
              <p className="min-w-0 flex-1">{warn}</p>
              {warn !== error && warn !== notice && flagged.length > 0 && (
                <button type="button" className={`h-6 shrink-0 rounded-md border border-warning/30 px-2 text-[12px] font-semibold text-warning transition hover:bg-warning/10 ${ring}`}
                  onClick={ignoreAll} title="Clear every red and yellow prerequisite flag and allow approval">
                  Ignore all ({flagged.length})
                </button>
              )}
              <button type="button" className={`grid h-6 w-6 shrink-0 place-items-center rounded-md border border-warning/30 text-[12px] font-semibold leading-none text-warning transition hover:bg-warning/10 ${ring}`}
                onClick={dismissWarn} aria-label="Dismiss message" title="Dismiss message">
                X
              </button>
            </motion.div>
          )}
        </AnimatePresence>

        <main className="relative flex min-h-0 flex-1">
          {/* ---------- LEFT: approval ---------- */}
          <AnimatePresence initial={false} mode="popLayout">
            {left && (
              <motion.aside key="left" initial={{ x: -16, opacity: 0 }} animate={{ x: 0, opacity: 1 }} exit={{ x: -16, opacity: 0 }} transition={T.base}
                className="flex w-80 shrink-0 flex-col overflow-auto border-r border-line bg-surface" aria-label="Plan">
                {!done && resp && (
                  <section className="p-4">
                    <div className="mb-3 flex items-baseline justify-between">
                      <h2 className="text-[14px] font-semibold">{current.name}</h2>
                      <span className="text-[12px] text-ink-2 tabular-nums">{proposalCredits} cr · {resp.optimizer?.profile?.source === 'gemini' && resp.optimizer.applied ? 'Gemini-ranked' : resp.source === 'gemini' ? 'Gemini' : 'rule-based'}</span>
                    </div>
                    <ul className="mb-3 -mx-2">
                      <AnimatePresence initial={false}>
                        {proposal.map(p => {
                          const c = courses.get(p.id), pinned = pins.includes(p.id);
                          return <motion.li key={p.id} layout initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }} transition={T.fast}
                            className="group flex items-start gap-1 rounded-md px-2 py-1.5 transition hover:bg-surface-hover" onMouseEnter={() => enter(p.id)} onMouseLeave={leave}>
                            <button className="min-w-0 flex-1 cursor-pointer text-left" onClick={() => togglePin(p.id)} title={pinned ? 'Pinned — click to unpin' : 'Click to pin for this term'}>
                              <div className="flex items-baseline gap-1.5"><b className="font-semibold">{c?.code}</b><span className="truncate text-ink-2">{c?.name}</span>{pinned && <span className={tag}>pinned</span>}</div>
                              <p className="text-[12px] text-ink-2">{p.reason}</p>
                              {p.sections
                                ? <p className={`text-[12px] tabular-nums ${pattern ? 'text-ink-3' : p.sections.some(s => s.status === 'Open') ? 'text-ink-2' : 'text-warning'}`}>
                                    {pattern && 'usually '}{meets(p.sections[0])}
                                    {!pattern && p.sections[0].instr && ` · ${p.sections[0].instr}`}
                                    {p.sections.length > 1 && ` · +${p.sections.length - 1} more`}
                                    {!pattern && !p.sections.some(s => s.status === 'Open') && ' · all sections full'}
                                  </p>
                                : <p className="text-[12px] text-ink-3">no published schedule</p>}
                              {p.unlocks.length > 0 && <p className="text-[12px] text-ink-3">→ unlocks {p.unlocks.slice(0, 5).map(u => courses.get(u)?.code).join(', ')}</p>}
                            </button>
                            <button className={`${icon} h-6 w-6 opacity-0 group-hover:opacity-100 focus-visible:opacity-100`} onClick={() => remove(p.id)} aria-label={`Remove ${c?.code}`} title="Remove">×</button>
                          </motion.li>;
                        })}
                      </AnimatePresence>
                    </ul>
                    {resp.optimizer?.applied && scheduleOptions.length > 0 && activeSchedule && (
                      <div className="mb-3 rounded-md border border-line bg-canvas p-2">
                        <div className="mb-1 flex items-center justify-between gap-2">
                          <div><b className="text-[12px] font-semibold">Ranked schedule {scheduleIndex + 1} of {scheduleOptions.length}</b>
                            <p className="text-[11px] text-ink-3">{resp.optimizer.ranking === 'weighted-explored-pool' ? 'ordered by your schedule priorities' : 'conflict-free alternatives'}</p></div>
                          <div className="flex gap-0.5">
                            <button className={`${icon} h-6 w-6`} onClick={() => selectSchedule(scheduleIndex - 1)} disabled={scheduleIndex === 0} aria-label="Previous schedule">‹</button>
                            <button className={`${icon} h-6 w-6`} onClick={() => selectSchedule(scheduleIndex + 1)} disabled={scheduleIndex >= scheduleOptions.length - 1} aria-label="Next schedule">›</button>
                          </div>
                        </div>
                        <p className="text-[11px] text-ink-2 tabular-nums">{activeSchedule.metrics.campus_days ?? 0} campus days · {hours(activeSchedule.metrics.gap_minutes)} gaps · longest day {hours(activeSchedule.metrics.max_daily_span_minutes)}{activeSchedule.cost !== undefined && ` · score ${activeSchedule.cost.toFixed(2)}`}</p>
                        <button className={`${btn} mt-1 h-6 px-0 text-[12px] text-ink-2`} onClick={() => setFeedbackOpen(v => !v)}>Not for me</button>
                        {feedbackOpen && <div className="mt-1.5">
                          <textarea className={`min-h-16 w-full resize-y rounded-md border border-line bg-canvas p-2 text-[12px] text-ink ${ring}`} value={scheduleFeedback} onChange={e => setScheduleFeedback(e.target.value)} placeholder="Why not? e.g. Too much waiting between classes, or I really don't want to come in on Friday." />
                          <button className={`${primary} mt-1`} disabled={preferenceLoading || !scheduleFeedback.trim()} onClick={() => interpretSchedulePreferences(scheduleFeedback)}>{preferenceLoading ? 'Updating…' : 'Update priorities with Gemini'}</button>
                        </div>}
                      </div>
                    )}
                    <div className="mb-1.5 flex gap-1">
                      <select className={`${field} min-w-0 flex-1`} aria-label="Filter by subject" value={filter.subject} onChange={e => setFilter({ ...filter, subject: e.target.value })}>
                        <option value="">All subjects</option>{subjects.map(s => <option key={s} value={s}>{s}</option>)}
                      </select>
                      <select className={`${field} min-w-0 flex-1`} aria-label="Filter by Pathways" value={filter.pathway} onChange={e => setFilter({ ...filter, pathway: e.target.value })}>
                        <option value="">Any Pathways</option>{Object.entries(gened.labels).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
                      </select>
                    </div>
                    <label className={`${btn} mb-1.5 cursor-pointer font-normal text-ink-2`}><input type="checkbox" className="accent-accent" checked={filter.eligible} onChange={e => setFilter({ ...filter, eligible: e.target.checked })} /> Eligible now only</label>
                    <details className="mb-1.5">
                      <summary className={`${btn} w-full cursor-pointer justify-between font-normal text-ink-2`}>AI schedule priorities{scheduleProfile && <span className={tag}>Gemini</span>}</summary>
                      <div className="mt-1.5 rounded-md border border-line p-2">
                        <p className="mb-1.5 text-[12px] text-ink-3">Describe your real life. Gemini translates it into visible weights and hard unavailable times; Python still decides whether a schedule is valid.</p>
                        <textarea className={`min-h-20 w-full resize-y rounded-md border border-line bg-canvas p-2 text-[12px] text-ink ${ring}`} value={schedulePrompt} onChange={e => setSchedulePrompt(e.target.value)} placeholder="Example: I work Tue/Thu 9–1, commute 50 minutes, hate long gaps, and don't want to be on campus more than 6 hours." />
                        <div className="mt-1 flex flex-wrap gap-1">
                          <button className={primary} disabled={preferenceLoading || !schedulePrompt.trim()} onClick={() => interpretSchedulePreferences()}>{preferenceLoading ? 'Asking Gemini…' : scheduleProfile ? 'Re-interpret' : 'Prioritize schedules'}</button>
                          {scheduleProfile && <button className={btn} onClick={() => { setScheduleProfile(null); setSchedulePrompt(''); setScheduleFeedback(''); setFeedbackOpen(false); }}>Clear AI priorities</button>}
                        </div>
                        {scheduleProfile && <div className="mt-2 border-t border-line pt-2">
                          <p className="mb-1 text-[12px] text-ink-2">{scheduleProfile.summary}</p>
                          <div className="flex flex-wrap gap-1">
                            {Object.entries(scheduleProfile.weights).filter(([, value]) => value > 0).map(([name, value]) => <span key={name} className={tag}>{WEIGHT_LABELS[name] ?? name}: {Math.round(value * 100)}%</span>)}
                            {scheduleProfile.commuteMinutes !== null && <span className={tag}>Commute: {scheduleProfile.commuteMinutes} min</span>}
                            {scheduleProfile.maxCampusSpanMinutes !== null && <span className={tag}>Preferred max day: {hours(scheduleProfile.maxCampusSpanMinutes)}</span>}
                          </div>
                          {scheduleProfile.availability && <div className="mt-1 text-[11px] text-ink-3">
                            {(scheduleProfile.availability.earliest > 0 || scheduleProfile.availability.latest < 24 * 60) && <p>AI hard window: {clock(scheduleProfile.availability.earliest)}–{clock(scheduleProfile.availability.latest)}</p>}
                            {scheduleProfile.availability.busy.length > 0 && <p>AI unavailable: {scheduleProfile.availability.busy.map(([day, start, end]) => `${day} ${clock(start)}–${clock(end)}`).join(' · ')}</p>}
                          </div>}
                        </div>}
                      </div>
                    </details>
                    {/* Availability — filters candidates to courses with a section the student can actually attend. */}
                    <details className="mb-1.5">
                      <summary className={`${btn} w-full cursor-pointer justify-between font-normal text-ink-2`}>
                        Availability{availNarrowed && <span className={tag}>on</span>}
                      </summary>
                      <div className="mt-1.5 rounded-md border border-line p-2">
                        <div className="mb-2 flex flex-wrap items-center gap-1 text-[12px] text-ink-2">
                          <span>No class before</span>
                          <input type="time" className={`${field} w-26`} aria-label="Earliest class start" value={hhmm(avail.earliest)}
                            onChange={e => setAvail({ ...avail, earliest: mins(e.target.value) })} />
                          <span>or after</span>
                          <input type="time" className={`${field} w-26`} aria-label="Latest class end" value={hhmm(avail.latest)}
                            onChange={e => setAvail({ ...avail, latest: mins(e.target.value) })} />
                        </div>
                        <div className="flex gap-1">
                          {DAYS.map(([code, lbl]) => {
                            const off = avail.busy.some(b => b[0] === code);
                            return <button key={code} aria-pressed={!off} title={off ? `${code}: unavailable` : `${code}: available`}
                              className={`h-7 flex-1 rounded-md border text-[12px] font-medium transition ${ring} ${off ? 'border-line bg-surface-hover text-ink-3 line-through' : 'border-line-strong text-ink hover:bg-surface-hover'}`}
                              onClick={() => setAvail({ ...avail, busy: off ? avail.busy.filter(b => b[0] !== code) : [...avail.busy, [code, 0, 24 * 60]] })}>{lbl}</button>;
                          })}
                        </div>
                        <p className="mt-1.5 text-[12px] text-ink-3">Struck-through days are days you can’t attend. Courses with no section that fits are not suggested.</p>
                        {availNarrowed && <button className={`${btn} mt-1 px-0 text-ink-2`} onClick={() => setAvail(NO_AVAIL)}>Clear availability</button>}
                      </div>
                    </details>
                    <details className="mb-1.5">
                      <summary className={`${btn} w-full cursor-pointer justify-between font-normal text-ink-2`}>
                        Elective preferences{preferenceCount > 0 && <span className={tag}>{preferenceCount}</span>}
                      </summary>
                      <div className="mt-1.5 rounded-md border border-line p-2">
                        <p className="mb-2 text-[12px] text-ink-3">Soft preferences for Pathways, Writing Intensive, and free electives only. Major requirements are unaffected.</p>
                        {([
                          ['preferredSubjects', 'Prefer', 'Preferred subject'],
                          ['avoidedSubjects', 'Avoid if possible', 'Subject to avoid'],
                        ] as const).map(([kind, label, aria]) => (
                          <div key={kind} className="mb-2 last:mb-0">
                            <label className="mb-1 block text-[12px] font-medium text-ink-2">{label}</label>
                            <select className={`${field} w-full`} aria-label={aria} value="" onChange={e => addPreference(kind, e.target.value)}>
                              <option value="">Choose a subject…</option>
                              {subjects.filter(s => !preferences[kind].includes(s)).map(s => <option key={s} value={s}>{s}</option>)}
                            </select>
                            {preferences[kind].length > 0 && <div className="mt-1.5 flex flex-wrap gap-1">
                              {preferences[kind].map(subject => <button key={subject} type="button" className={`${tag} inline-flex items-center gap-1 py-0.5 transition hover:bg-line`}
                                onClick={() => removePreference(kind, subject)} aria-label={`Remove ${subject} ${label.toLowerCase()} preference`} title="Remove">
                                {subject}<span aria-hidden="true">×</span>
                              </button>)}
                            </div>}
                          </div>
                        ))}
                        {preferenceCount > 0 && <button className={`${btn} mt-1 px-0 text-ink-2`} onClick={() => setPreferences(NO_PREFERENCES)}>Clear preferences</button>}
                      </div>
                    </details>
                    <input list="cands" placeholder={`+ Add a course (${addable.length})`} value={adding} className={`${field} mb-3 w-full`} aria-label="Add course" onChange={e => { setAdding(e.target.value); add(e.target.value); }} />
                    <datalist id="cands">{addable.map(c => <option key={c.id} value={label(c)}>{resp.candidates.find(x => x.id === c.id)?.reason ?? ''}</option>)}</datalist>
                    <div className="flex flex-wrap gap-1">
                      <button className={primary} onClick={approve} disabled={loading || !proposal.length || blocked.length > 0}
                        title={blocked.length ? `${blocked.map(p => courses.get(p.id)?.code).join(', ')} missing prerequisites` : undefined}>Approve {current.name}</button>
                      <button className={btn} onClick={generate} disabled={loading} title={`Ask Gemini to order this term (uses API credits)${pins.length ? `; keeps ${pins.length} pinned` : ''}`}>
                        {resp.source === 'gemini' ? 'Regenerate with Gemini' : 'Generate with Gemini'}</button>
                      <button className={btn} onClick={undo} disabled={!terms.length}>Undo</button>
                    </div>
                    <AnimatePresence>{loading && <motion.p key="l" initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }} transition={T.fast} className="mt-2 text-[12px] text-ink-3">Thinking…</motion.p>}</AnimatePresence>
                  </section>
                )}
                {queue.length > 0 && (
                  <section className="border-t border-line p-4">
                    <h3 className="eyebrow mb-1">Queued for later</h3>
                    <p className="mb-2 text-[12px] text-ink-3">Each joins the first term it becomes eligible.</p>
                    <ul className="-mx-2">
                      <AnimatePresence initial={false}>
                        {queue.map(id => {
                          const c = courses.get(id), now = !!eligibleNow(id);
                          return <motion.li key={id} layout initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }} transition={T.fast}
                            className="group flex items-center gap-1 rounded-md px-2 py-1.5 transition hover:bg-surface-hover" onMouseEnter={() => enter(id)} onMouseLeave={leave}>
                            <div className="min-w-0 flex-1"><b className="font-semibold">{c?.code}</b> <span className="truncate text-ink-2">{c?.name}</span></div>
                            <button className={`${btn} h-6 px-1.5 text-[12px]`} onClick={() => want(id)} disabled={!now} title={now ? `Add to ${current.name}` : 'Prerequisites not planned yet'}>Add now</button>
                            <button className={`${icon} h-6 w-6`} onClick={() => unqueue(id)} aria-label={`Remove ${c?.code} from queue`} title="Remove">×</button>
                          </motion.li>;
                        })}
                      </AnimatePresence>
                    </ul>
                  </section>
                )}
                {!resp && !error && <p className="p-4 text-ink-3">Loading plan…</p>}
                {done && <section className="m-4 rounded-lg bg-success-soft p-3 text-[13px] font-medium text-success">🎓 {DEGREE_CREDITS} credits planned</section>}
                <div className="mt-auto border-t border-line p-2">
                  <button className={`${btn} w-full justify-start text-ink-2`} onClick={reset} disabled={!terms.length && !queue.length}>Start over</button>
                </div>
              </motion.aside>
            )}
          </AnimatePresence>

          {/* ---------- CENTER: diagram ---------- */}
          <div ref={canvas} className="relative min-w-0 flex-1 overflow-hidden bg-canvas cursor-grab active:cursor-grabbing select-none" style={{ touchAction: 'none' }}
            onPointerDown={e => { if ((e.target as HTMLElement).closest('button,[role=button],[data-more],[draggable=true]')) return; setMore(false); setFocus(null); drag.current = { px: e.clientX, py: e.clientY, x: view.x, y: view.y }; e.currentTarget.setPointerCapture(e.pointerId); }}
            onPointerMove={e => { const d = drag.current; if (d) setView(v => ({ ...v, x: d.x + e.clientX - d.px, y: d.y + e.clientY - d.py })); }}
            onPointerUp={() => { drag.current = null; }} onPointerCancel={() => { drag.current = null; }}>
            <div className="absolute right-3 bottom-3 z-10 flex items-center gap-0.5 rounded-md border border-line bg-canvas p-0.5" role="group" aria-label="Zoom">
              <button className={`${icon} h-6 w-6`} onClick={() => zoomBy(1 / 1.25)} aria-label="Zoom out">−</button>
              <button className={`${btn} h-6 px-1.5 text-[12px] text-ink-2 tabular-nums`} onClick={() => setView({ x: 0, y: 0, s: 1 })} title="Reset view">{Math.round(view.s * 100)}%</button>
              <button className={`${icon} h-6 w-6`} onClick={() => zoomBy(1.25)} aria-label="Zoom in">+</button>
            </div>
            {/* no `layout` props inside this transformed layer: motion's layout projection fights the pan/zoom transform */}
            <div className="relative origin-top-left" style={{ width: W, height: H, transform: `translate(${view.x}px, ${view.y}px) scale(${view.s})` }}>
              {levels.map((l, i) => (
                <motion.div
                  key={l.name}
                  initial={{ opacity: 0 }}
                  animate={{ opacity: 1 }}
                  transition={T.slow}
                  onDragOver={e => {
                    e.preventDefault();
                    e.dataTransfer.dropEffect = 'move';
                    setDropTarget(i);
                  }}
                  onDragLeave={e => {
                    if (!e.currentTarget.contains(e.relatedTarget as Node)) {
                      setDropTarget(null);
                    }
                  }}
                  onDrop={e => {
                    e.preventDefault();

                    const id =
                      e.dataTransfer.getData('text/plain') ||
                      draggedCourse.current?.id;

                    if (!id) return;

                    const target: CourseLocation = l.proposed
                      ? { kind: 'proposal' }
                      : l.queued
                        ? { kind: 'queue' }
                        : { kind: 'term', index: i };

                    moveCourse(id, target);
                    draggedCourse.current = null;
                    setDropTarget(null);
                  }}
                  className={`absolute rounded-lg transition-shadow ${dropTarget === i ? 'ring-2 ring-accent/50' : ''
                    } ${l.queued
                      ? 'border border-dashed border-line bg-canvas'
                      : l.proposed
                        ? 'border border-dashed border-line-strong bg-accent-soft/60'
                        : /Summer|Winter/.test(l.kind)
                          ? 'bg-success-soft'
                          : 'bg-surface'
                    }`}
                  style={{
                    left: GEOM.PAD,
                    top: tops[i],
                    width: W - GEOM.PAD * 2,
                    height: bandH(l.ids.length),
                  }}>
                  <div className="absolute left-4 top-1/2 -translate-y-1/2 leading-tight">
                    <b className={`block text-[13px] font-semibold ${l.queued ? 'text-ink-2' : ''}`}>{l.name}</b>
                    <small className="text-[12px] text-ink-2 tabular-nums">{l.queued ? 'when eligible' : `${l.cr ?? credits(l.ids)} cr${l.proposed ? ' · proposed' : ''}`}</small>
                  </div>
                </motion.div>
              ))}

              <svg width={W} height={H} className="pointer-events-none absolute inset-0" aria-hidden="true">
                <defs>
                  <marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" className="fill-line-strong" /></marker>
                  <marker id="arrow-hot" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" className="fill-accent" /></marker>
                </defs>
                <AnimatePresence>
                  {edges.map(({ from, to }) => {
                    const pa = at(from), pb = at(to);
                    const x1 = pa.x + GEOM.CARD_W / 2, y1 = pa.y + GEOM.CARD_H, x2 = pb.x + GEOM.CARD_W / 2, y2 = pb.y - 5, my = (y1 + y2) / 2;
                    const isHot = !!(hot?.has(from) && hot?.has(to)), dim = !!hot && !isHot;
                    return <motion.path key={from + to} d={`M${x1},${y1} C${x1},${my} ${x2},${my} ${x2},${y2}`} fill="none"
                      className={`transition-colors ${isHot ? 'stroke-accent' : 'stroke-line-strong'}`} strokeWidth={isHot ? 2 : 1.25}
                      initial={{ pathLength: 0, opacity: 0 }} exit={{ opacity: 0 }} animate={{ pathLength: 1, opacity: dim ? 0.2 : 1 }}
                      transition={{ pathLength: T.slow, default: T.fast }} markerEnd={isHot ? 'url(#arrow-hot)' : 'url(#arrow)'} />;
                  })}
                </AnimatePresence>
              </svg>

              <AnimatePresence>
                {levels.map((col, levelIndex) => col.ids.map(id => {
                  const c = courses.get(id); const { x, y } = at(id);
                  const s = proposal.find(p => p.id === id);
                  const v = violations.get(id);
                  const st = col.proposed && !ignored.includes(id) ? status(id, proposalIds) : 'ok';       // red: no prereq met; yellow: some met (almost eligible)
                  const shrug = ignored.includes(id);
                  const tone = v || st === 'missing' ? 'danger' : st === 'partial' || (unverified.has(id) && !shrug) ? 'warning' : null;
                  const pinned = col.proposed && pins.includes(id);
                  const dim = !!hot && !hot.has(id);
                  const clickable = !!col.proposed;
                  const location: CourseLocation = col.proposed
                    ? { kind: 'proposal' }
                    : col.queued
                      ? { kind: 'queue' }
                      : { kind: 'term', index: levelIndex };
                  return <motion.div key={`${col.name}:${id}`} initial={{ opacity: 0, x, y }} exit={{ opacity: 0 }}
                    animate={{ opacity: dim ? 0.35 : col.queued ? 0.7 : 1, x, y }} transition={T.slow} draggable
                    onDragStartCapture={e => {
                      draggedCourse.current = { id, from: location };
                      suppressCourseClick.current = true;
                      e.dataTransfer.effectAllowed = 'move';
                      e.dataTransfer.setData('text/plain', id);
                      setMore(false);
                      setFocus(null);
                    }}
                    onDragEndCapture={() => {
                      draggedCourse.current = null;
                      setDropTarget(null);

                      window.setTimeout(() => {
                        suppressCourseClick.current = false;
                      }, 0);
                    }}
                    role={clickable ? 'button' : undefined} tabIndex={clickable ? 0 : undefined} aria-pressed={clickable ? pinned : undefined}
                    onClick={
                      clickable
                        ? () => {
                          if (!suppressCourseClick.current) togglePin(id);
                        }
                        : undefined
                    } onKeyDown={clickable ? e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); togglePin(id); } } : undefined}
                    className={`group absolute top-0 left-0 flex select-none items-center gap-2 rounded-md border bg-canvas px-3 transition-colors ${clickable ? `cursor-pointer ${ring}` : 'cursor-default'}
                    ${tone === 'danger' ? 'border-danger bg-danger-soft' : tone === 'warning' ? 'border-warning bg-warning-soft' : hot?.has(id) ? 'border-accent' : pinned ? 'border-accent bg-accent-soft/40' : col.proposed || col.queued ? 'border-dashed border-line-strong hover:border-ink-3' : 'border-line hover:border-line-strong'}`}
                    style={{ width: GEOM.CARD_W, height: GEOM.CARD_H }}
                    title={c ? [
                      `${c.name}\n${c.credits} cr`,
                      col.proposed ? (pinned ? 'Pinned for this term — click to unpin' : 'Click to pin for this term') : col.queued ? 'Queued — joins the first eligible term' : '',
                      v ? `Needs ${v.missing.map(m => courses.get(m)?.code).join(' or ')} in an earlier term` : '',
                      st === 'missing' ? 'Prerequisites not met by an earlier term — remove, or ignore (top-left) if you have permission' : st === 'partial' ? 'Almost eligible: some prerequisites are still missing from earlier terms' : '',
                      ignored.includes(id) ? 'Prerequisite warning ignored' : '',
                      s ? `Why: ${s.reason}` : '',
                      s?.unlocks.length ? `Unlocks: ${s.unlocks.map(u => courses.get(u)?.code).join(', ')}` : '',
                      (prereqs[id] ?? []).length ? `Prereqs: ${prereqs[id].map(g => g.map(q => courses.get(q)?.code).join(' or ')).join(' and ')}${s?.source ? ` (source: ${s.source})` : ''}`
                        : unverified.has(id) ? 'Prereqs: none found in the catalog — confirm with an advisor' : '',
                      col.transfers?.[id] ? `Transfer Credit from ${col.transfers[id]}` : c.description,
                    ].filter(Boolean).join('\n\n') : id}
                    onMouseEnter={() => enter(id)} onMouseLeave={leave}>
                    <div className={`h-1.5 w-1.5 shrink-0 rounded-full ${tone === 'danger' ? 'bg-danger' : tone === 'warning' ? 'bg-warning' : pinned ? 'bg-accent' : col.proposed || col.queued ? 'bg-line-strong' : 'bg-success'}`} />
                    <div className="min-w-0 flex-1">
                      <b className="flex items-center gap-1.5 text-[13px] font-semibold">{c?.code ?? id}
                        {unverified.has(id) && !shrug && <i className="grid h-3.5 w-3.5 place-items-center rounded-full bg-warning text-[9px] font-bold not-italic text-white" title="No prerequisite data found — confirm with an advisor">!</i>}</b>
                      <span className="card-name">{c?.name}</span>
                    </div>
                    <em className="text-[12px] not-italic text-ink-3 tabular-nums">{c?.credits}</em>
                    {(col.proposed || col.queued) && <button title="Remove" aria-label={`Remove ${c?.code}`} onClick={e => { e.stopPropagation(); (col.queued ? unqueue : remove)(id); }}
                      className={`${icon} absolute -top-2.5 -right-2.5 h-5 w-5 border border-line bg-canvas text-[12px] opacity-0 group-hover:opacity-100 focus-visible:opacity-100 hover:text-danger`}>×</button>}
                    {tone && <button title="Ignore prerequisite warning" aria-label={`Ignore prerequisite warning for ${c?.code}`} onClick={e => { e.stopPropagation(); ignore(id); }}
                      className={`${icon} absolute -top-2.5 -left-2.5 h-5 w-5 border border-line bg-canvas text-[10px] font-semibold opacity-0 group-hover:opacity-100 focus-visible:opacity-100 hover:text-ink`}>ok</button>}
                  </motion.div>;
                }))}
              </AnimatePresence>

              {/* ghost suggestions: what the hovered proposed course unlocks next (informational; slot 3 opens a "+N more" list) */}
              <svg className="pointer-events-none absolute inset-0 overflow-visible" width={W} height={H} aria-hidden="true">
                <AnimatePresence>
                  {ghosts.map(g => <motion.path key={`ge:${g.id}`} d={`M${g.cx},${g.cy} L${g.x + GEOM.CARD_W / 2},${g.y}`} fill="none" className="stroke-line-strong" strokeWidth={1.25} strokeDasharray="3 4"
                    initial={{ opacity: 0, pathLength: 0 }} animate={{ opacity: 1, pathLength: 1 }} exit={{ opacity: 0 }} transition={T.base} />)}
                </AnimatePresence>
              </svg>
              <AnimatePresence>
                {ghosts.map(g => {
                  const c = courses.get(g.id);
                  const cls = 'absolute top-0 left-0 z-20 flex items-center gap-2 rounded-md border border-dashed border-line-strong bg-canvas px-3 text-left text-ink-2';
                  const anim = { initial: { opacity: 0, x: g.cx - GEOM.CARD_W / 2, y: g.cy - GEOM.CARD_H / 2 }, animate: { opacity: 0.7, x: g.x, y: g.y }, exit: { opacity: 0 }, transition: T.base };
                  if (g.more) return <motion.div key="ghost:more" data-more className={`${cls} cursor-default`} style={{ width: GEOM.CARD_W, height: GEOM.CARD_H }} {...anim} onMouseEnter={() => enter(g.src)} onMouseLeave={leave}>
                    <button type="button" className={`${btn} -mx-2 w-full justify-between text-ink-2`} aria-expanded={more} onClick={() => setMore(m => !m)}>
                      +{rest.length} more <span className={`text-ink-3 transition ${more ? 'rotate-180' : ''}`} aria-hidden="true">⌄</span>
                    </button>
                    <AnimatePresence>
                      {more && <motion.ul key="list" initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }} transition={T.fast}
                        className="absolute top-full left-0 z-30 mt-1 max-h-56 w-64 overflow-auto rounded-md border border-line bg-canvas py-1 shadow-2 text-ink" role="list">
                        {rest.map(id => {
                          const r = courses.get(id); return <li key={id}><button type="button" onClick={() => enqueue(id, g.src)} title={`Pin ${courses.get(g.src)?.code} and queue for the next eligible term`}
                            className={`flex w-full cursor-pointer items-baseline gap-1.5 px-3 py-1 text-left text-[13px] transition hover:bg-surface-hover ${ring}`}>
                            <b className="font-semibold">{r?.code ?? id}</b><span className="truncate text-ink-2">{r?.name}</span><em className="ml-auto pl-2 text-[12px] not-italic text-ink-3 tabular-nums">{r?.credits}</em></button></li>;
                        })}
                      </motion.ul>}
                    </AnimatePresence>
                  </motion.div>;
                  return <motion.div key={`ghost:${g.id}`} role="button" tabIndex={0} className={`${cls} cursor-pointer hover:border-accent ${ring}`} style={{ width: GEOM.CARD_W, height: GEOM.CARD_H }} {...anim}
                    onClick={() => enqueue(g.id, g.src)} onKeyDown={e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); enqueue(g.id, g.src); } }}
                    onMouseEnter={() => enter(g.src)} onMouseLeave={leave}
                    title={`${c?.name}\n${c?.credits} cr — unlocked by ${courses.get(g.src)?.code}\nClick to pin ${courses.get(g.src)?.code} this term and queue this for the next eligible term`}>
                    <div className="h-1.5 w-1.5 shrink-0 rounded-full bg-line-strong" />
                    <div className="min-w-0 flex-1">
                      <b className="block text-[13px] font-semibold">{c?.code ?? g.id}</b>
                      <span className="card-name">{c?.name}</span>
                    </div>
                    <em className="text-[12px] not-italic text-ink-3 tabular-nums">{c?.credits}</em>
                  </motion.div>;
                })}
              </AnimatePresence>
            </div>
          </div>

          {/* ---------- RIGHT: requirements + pathways ---------- */}
          <AnimatePresence initial={false} mode="popLayout">
            {right && (
              <motion.aside key="right" initial={{ x: 16, opacity: 0 }} animate={{ x: 0, opacity: 1 }} exit={{ x: 16, opacity: 0 }} transition={T.base}
                className="w-80 shrink-0 overflow-auto border-l border-line bg-surface p-4" aria-label="Requirements">
                {!resp && <p className="text-ink-3">Requirements appear once a plan loads.</p>}
                {resp && <>
                  <h3 className="eyebrow mb-1">Major requirements</h3>
                  {resp.progress.major.map(m => {
                    const auditGroups = new Set((m.auditCompleted ?? []).map(a => a.courses.join('|')));
                    return (
                      <details key={m.name} open={m.have < m.need} className="group/d -mx-2">
                        <summary className={`flex cursor-pointer items-center gap-2 rounded-md px-2 py-1.5 text-[13px] transition hover:bg-surface-hover marker:content-none ${ring}`}>
                          <span className="w-3 text-ink-3 transition group-open/d:rotate-90" aria-hidden="true">›</span>
                          <span className="flex-1 font-medium">{m.name.replace('Major Requirements - ', '')}</span>
                          <span className={`text-[12px] tabular-nums ${m.have >= m.need ? 'text-success' : 'text-ink-2'}`}>{m.have}/{m.need} {m.unit}</span>
                        </summary>
                        <ul className="mb-1 columns-2 pl-7 pr-2 text-[12px] text-ink-2">
                          {(m.auditCompleted ?? []).map((a, i) => <li key={`audit:${i}`} className="break-inside-avoid text-success line-through decoration-success/70 decoration-2">{auditReqText(a)}</li>)}
                          {(m.completed ?? []).filter(o => !auditGroups.has(o.join('|'))).map((o, i) => <li key={`done:${i}`} className="text-success line-through decoration-success/70 decoration-2">{courseCodes(o)}</li>)}
                          {m.missing.slice(0, 24).map((o, i) => <li key={`todo:${i}`}>{courseCodes(o)}</li>)}
                          {m.missing.length > 24 && <li>… {m.missing.length - 24} more</li>}
                        </ul>
                      </details>
                    );
                  })}
                  <h3 className="eyebrow mt-5 mb-1">Pathways</h3>
                  <ul className="-mx-2">{resp.progress.pathways.map(s => (
                    <li key={s.slot} className={`flex items-center gap-2 rounded-md px-2 py-1 text-[13px] transition hover:bg-surface-hover ${s.course ? 'text-ink' : 'text-ink-2'}`}>
                      <span className={`h-1.5 w-1.5 rounded-full ${s.course ? 'bg-success' : 'bg-line-strong'}`} aria-hidden="true" />
                      <span className="flex-1">{s.label}</span>
                      {s.course && <b className="font-medium tabular-nums text-success">{courses.get(s.course)?.code}</b>}
                    </li>
                  ))}</ul>
                </>}
              </motion.aside>
            )}
          </AnimatePresence>
          <AnimatePresence>{chat && <Chat program={pid} terms={terms} term={current.kind} onClose={() => setChat(false)} />}</AnimatePresence>
        </main>
      </div>
    </MotionConfig>
  );
}
