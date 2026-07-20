import { createFileRoute } from "@tanstack/react-router";
import { Fragment, useEffect, useState } from "react";
import {
  useOverview, useAiPreview, useBatches, useNotTaggable, useCapabilities,
  useInventory, useRulePreview, useTagSelected, useManualTag, useRollback,
  useBatchDetail, fieldValues,
} from "@/lib/api";
import { Card } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Badge } from "@/components/ui/badge";
import { Progress } from "@/components/ui/progress";
import { Checkbox } from "@/components/ui/checkbox";
import {
  Table, TableBody, TableCell, TableHead, TableHeader, TableRow,
} from "@/components/ui/table";
import { ModeToggle } from "@/components/apx/mode-toggle";

export const Route = createFileRoute("/")({ component: () => <App /> });

// ---------- helpers ----------
const money = (v: unknown) => {
  const n = Number(v);
  if (!isFinite(n)) return "—";
  if (Math.abs(n) >= 1e6) return `$${(n / 1e6).toFixed(1)}M`;
  if (Math.abs(n) >= 1e3) return `$${(n / 1e3).toFixed(1)}K`;
  return `$${Math.round(n).toLocaleString()}`;
};
const num = (v: unknown) => Number(v ?? 0);
type Row = Record<string, unknown>;

// After an apply, jump to the batches list so the user sees the progress bar fill.
const scrollToBatches = () =>
  setTimeout(() => document.getElementById("batches-section")?.scrollIntoView({ behavior: "smooth" }), 300);

// ---------- top controls ----------
function useControls() {
  const [tagKey, setTagKey] = useState("cost_center");
  const [days, setDays] = useState(30);
  return { tagKey, setTagKey, days, setDays };
}

// Probes /api/health on load. If the app can't reach the warehouse (the classic
// blank-KPI cause — app SP missing CAN_USE, or a stripped app.yml env block), we
// show a loud, actionable error instead of an empty dashboard. Uses plain fetch
// so it works even before the typed hook is regenerated from the OpenAPI schema.
function HealthBanner() {
  const [h, setH] = useState<{ ok: boolean; detail?: string } | null>(null);
  useEffect(() => {
    fetch("/api/health")
      .then((r) => (r.ok ? r.json() : { ok: false, detail: `Health check HTTP ${r.status}` }))
      .then(setH)
      .catch((e) => setH({ ok: false, detail: String(e) }));
  }, []);
  if (!h || h.ok) return null;
  return (
    <div className="rounded-lg border border-red-500/50 bg-red-500/10 p-4 text-sm">
      <div className="font-semibold text-red-600 dark:text-red-400">
        ⚠️ The dashboard can’t load data
      </div>
      <p className="mt-1 text-muted-foreground">
        {h.detail || "The app can’t reach its SQL warehouse. KPIs will be blank until this is fixed."}
      </p>
    </div>
  );
}

function App() {
  const c = useControls();
  const [mode, setMode] = useState<"ai" | "rules" | "manual">("ai");
  const [banner, setBanner] = useState<{ kind: "info" | "warn"; html: string } | null>(null);

  return (
    <div className="min-h-screen bg-background text-foreground">
      <header className="flex items-center gap-3 border-b px-8 py-4">
        <span className="text-2xl">🏷️</span>
        <div className="flex-1">
          <h1 className="text-xl font-bold tracking-tight">Tag Governance</h1>
          <p className="text-sm text-muted-foreground">
            Find untagged spend · attribute it to teams · tag safely &amp; reversibly
          </p>
        </div>
        <ModeToggle />
      </header>

      <main className="mx-auto max-w-6xl space-y-8 px-8 py-8">
        <HealthBanner />
        {/* controls */}
        <div className="flex flex-wrap items-end gap-6">
          <div>
            <label className="mb-1 block text-xs font-semibold text-muted-foreground">Tag key</label>
            <Input value={c.tagKey} onChange={(e) => c.setTagKey(e.target.value)} className="w-56" />
          </div>
          <div>
            <label className="mb-1 block text-xs font-semibold text-muted-foreground">Lookback (days)</label>
            <select
              value={c.days}
              onChange={(e) => c.setDays(Number(e.target.value))}
              className="h-9 rounded-md border bg-background px-3 text-sm"
            >
              {[7, 14, 30, 60, 90].map((d) => <option key={d} value={d}>{d}</option>)}
            </select>
          </div>
        </div>

        <Overview tagKey={c.tagKey} days={c.days} />

        <section>
          <h2 className="mb-3 text-xs font-bold uppercase tracking-wider text-muted-foreground">
            Tag workloads
          </h2>
          <Card className="p-6">
            <div className="mb-4 flex gap-2">
              {(["ai", "rules", "manual"] as const).map((m) => (
                <Button key={m} variant={mode === m ? "default" : "secondary"}
                  size="sm" onClick={() => { setMode(m); setBanner(null); }}>
                  {m === "ai" ? "🤖 AI suggestions" : m === "rules" ? "📋 Rules (no AI)" : "✏️ Manual"}
                </Button>
              ))}
            </div>
            {mode === "ai" && <AiMode {...c} onResult={setBanner} />}
            {mode === "rules" && <RulesMode {...c} onResult={setBanner} />}
            {mode === "manual" && <ManualMode {...c} onResult={setBanner} />}
            {banner && (
              <div className={`mt-4 rounded-lg border p-3 text-sm ${
                banner.kind === "warn"
                  ? "border-amber-500/40 bg-amber-500/10"
                  : "border-sky-500/40 bg-sky-500/10"}`}
                dangerouslySetInnerHTML={{ __html: banner.html }} />
            )}
          </Card>
        </section>

        <LiveInventory />
        <NotTaggable tagKey={c.tagKey} days={c.days} />
        <CapabilityMatrix />
        <Batches />
      </main>
    </div>
  );
}

// ---------- Live asset inventory (Phase 1 scan — ground-truth tag state) ----------
function LiveInventory() {
  const { data } = useInventory();
  const rows = (data?.data.rows ?? []) as Row[];
  const meta = rows.find((r) => r.product === "__meta__");
  const products = rows.filter((r) => r.product !== "__meta__");
  if (!products.length) {
    return (
      <section>
        <h2 className="mb-3 text-xs font-bold uppercase tracking-wider text-muted-foreground">
          Live asset inventory
        </h2>
        <Card className="p-5 text-sm text-muted-foreground">
          No scan yet. Run the <code className="rounded bg-muted px-1">tag-governance-scan</code> job
          to enumerate live resources and read their true tag state.
        </Card>
      </section>
    );
  }
  const totalRes = products.reduce((s, p) => s + num(p.resources), 0);
  const totalUntagged = products.reduce((s, p) => s + num(p.untagged), 0);
  const when = meta?.scanned_at ? new Date(String(meta.scanned_at)).toLocaleString() : "";

  return (
    <section>
      <div className="mb-3 flex items-baseline justify-between">
        <h2 className="text-xs font-bold uppercase tracking-wider text-muted-foreground">
          Live asset inventory
        </h2>
        <span className="text-xs text-muted-foreground">
          {totalRes.toLocaleString()} resources · {totalUntagged.toLocaleString()} untagged
          {when ? ` · scanned ${when}` : ""}
        </span>
      </div>
      <Card className="p-0">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Product</TableHead>
              <TableHead className="text-right">Resources</TableHead>
              <TableHead className="text-right">Untagged</TableHead>
              <TableHead className="text-right">Tagged</TableHead>
              <TableHead className="w-40">Coverage</TableHead>
              <TableHead className="text-right">Read failed</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {products.map((p, i) => {
              const res = num(p.resources);
              const tagged = num(p.tagged);
              const cov = res ? Math.round((100 * tagged) / res) : 0;
              return (
                <TableRow key={i}>
                  <TableCell className="font-medium">{String(p.product)}</TableCell>
                  <TableCell className="text-right tabular-nums">{res.toLocaleString()}</TableCell>
                  <TableCell className="text-right tabular-nums">{num(p.untagged).toLocaleString()}</TableCell>
                  <TableCell className="text-right tabular-nums">{tagged.toLocaleString()}</TableCell>
                  <TableCell>
                    <Progress value={cov} className="h-1.5" />
                    <span className="text-xs text-muted-foreground">{cov}% tagged</span>
                  </TableCell>
                  <TableCell className="text-right tabular-nums">
                    {num(p.read_failed) > 0
                      ? <span className="text-amber-600">{num(p.read_failed)}</span>
                      : "—"}
                  </TableCell>
                </TableRow>
              );
            })}
          </TableBody>
        </Table>
      </Card>
    </section>
  );
}

// ---------- Capability matrix (declarative registry from the backend) ----------
function CapabilityMatrix() {
  const [open, setOpen] = useState(false);
  const { data } = useCapabilities();
  const rows = (data?.data.rows ?? []) as Row[];
  if (!rows.length) return null;

  const yn = (v: unknown) => (v ? "✓" : "—");
  const fallbackLabel: Record<string, string> = {
    DIRECT: "Direct API",
    BUDGET_POLICY: "Budget policy",
    CREATE_TIME: "Create-time only",
    UI_ONLY: "Edit in UI",
    EXCEPTION_QUEUE: "Exception queue",
  };
  const fallbackColor: Record<string, string> = {
    DIRECT: "bg-emerald-500/15 text-emerald-600",
    BUDGET_POLICY: "bg-sky-500/15 text-sky-600",
    CREATE_TIME: "bg-violet-500/15 text-violet-600",
    UI_ONLY: "bg-amber-500/15 text-amber-600",
    EXCEPTION_QUEUE: "bg-red-500/15 text-red-600",
  };

  return (
    <section>
      <button
        onClick={() => setOpen((o) => !o)}
        className="mb-3 flex items-center gap-2 text-xs font-bold uppercase tracking-wider text-muted-foreground hover:text-foreground"
      >
        <span>{open ? "▾" : "▸"}</span> Capability matrix
        <span className="font-normal normal-case tracking-normal">
          — how each product can be tagged
        </span>
      </button>
      {open && (
        <Card className="p-0">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Product</TableHead>
                <TableHead className="text-center">Direct tag</TableHead>
                <TableHead className="text-center">Policy</TableHead>
                <TableHead className="text-center">UI-only</TableHead>
                <TableHead className="text-center">Rollback</TableHead>
                <TableHead>Remediation</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {rows.map((r, i) => (
                <TableRow key={i}>
                  <TableCell>
                    <div className="font-medium">{String(r.label)}</div>
                    {r.reason ? (
                      <div className="mt-0.5 max-w-md text-xs text-muted-foreground">
                        {String(r.reason)}
                      </div>
                    ) : null}
                  </TableCell>
                  <TableCell className="text-center tabular-nums">{yn(r.direct_tag)}</TableCell>
                  <TableCell className="text-center tabular-nums">{yn(r.policy_driven)}</TableCell>
                  <TableCell className="text-center tabular-nums">{yn(r.ui_only)}</TableCell>
                  <TableCell className="text-center tabular-nums">{yn(r.rollback)}</TableCell>
                  <TableCell>
                    <span className={`rounded px-2 py-0.5 text-xs font-medium ${
                      fallbackColor[String(r.fallback)] ?? "bg-muted"}`}>
                      {fallbackLabel[String(r.fallback)] ?? String(r.fallback)}
                    </span>
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </Card>
      )}
    </section>
  );
}

// ---------- Overview KPIs + progress ----------
function Overview({ tagKey, days }: { tagKey: string; days: number }) {
  const { data, isLoading } = useOverview({ params: { tag_key: tagKey, days } });
  const k = (data?.data.kpi ?? {}) as Row;
  const products = (data?.data.products ?? []) as Row[];
  const tagged = num(k.tagged_live_cost);
  const untagged = num(k.untagged_cost);
  const pct = tagged + untagged > 0 ? (100 * tagged) / (tagged + untagged) : 0;

  const tiles = [
    { label: "Untagged spend", val: money(k.untagged_cost), accent: "bg-red-500" },
    { label: "% untagged", val: `${k.pct_untagged ?? "—"}%`, accent: "bg-amber-500" },
    { label: "Untagged workloads", val: num(k.untagged_workloads).toLocaleString(), accent: "bg-sky-500" },
    { label: "Tagged so far (live)", val: money(k.tagged_live_cost), accent: "bg-emerald-500" },
  ];

  return (
    <section>
      <h2 className="mb-3 text-xs font-bold uppercase tracking-wider text-muted-foreground">
        Untagged spend
      </h2>
      <div className="grid grid-cols-2 gap-4 md:grid-cols-4">
        {tiles.map((t) => (
          <Card key={t.label} className="relative overflow-hidden p-5">
            <div className={`absolute left-0 top-0 h-full w-1 ${t.accent}`} />
            <div className="text-xs font-medium uppercase tracking-wide text-muted-foreground">{t.label}</div>
            <div className="mt-1 text-3xl font-extrabold tabular-nums">{isLoading ? "…" : t.val}</div>
          </Card>
        ))}
      </div>
      <div className="mt-4">
        <Progress value={pct} className="h-2" />
        <p className="mt-1.5 text-sm text-muted-foreground">
          {num(k.tagged_live_workloads) > 0
            ? <><b>{money(tagged)}</b> attributed across <b>{num(k.tagged_live_workloads).toLocaleString()}</b> workloads this session — updates live as you tag.</>
            : "No workloads tagged yet this session. Tag some below and watch this move."}
        </p>
      </div>
      {products.length > 0 && (
        <Card className="mt-4 p-5">
          <div className="mb-3 flex items-baseline justify-between">
            <h3 className="text-sm font-semibold">Untagged spend by product</h3>
            <span className="text-xs text-muted-foreground">ranked by untagged cost</span>
          </div>
          <div className="space-y-2.5">
            {(() => {
              const sorted = [...products].sort(
                (a, b) => num(b.untagged_cost) - num(a.untagged_cost));
              const max = num(sorted[0]?.untagged_cost) || 1;
              return sorted.map((p, i) => {
                const untagged = num(p.untagged_cost);
                const pct = num(p.pct_untagged);
                const w = Math.max(2, Math.round((100 * untagged) / max));
                // Hotter color = more fully-untagged (higher risk).
                const bar = pct >= 95 ? "bg-red-500" : pct >= 70 ? "bg-amber-500" : "bg-sky-500";
                return (
                  <div key={i} className="grid grid-cols-[9rem_1fr_5.5rem_3rem] items-center gap-3">
                    <span className="truncate text-sm font-medium" title={String(p.product)}>
                      {String(p.product)}
                    </span>
                    <div className="h-5 overflow-hidden rounded bg-muted/40">
                      <div className={`h-full ${bar} transition-all`} style={{ width: `${w}%` }} />
                    </div>
                    <span className="text-right text-sm tabular-nums">{money(untagged)}</span>
                    <span className="text-right text-xs tabular-nums text-muted-foreground">{pct}%</span>
                  </div>
                );
              });
            })()}
          </div>
        </Card>
      )}
    </section>
  );
}

type ModeProps = {
  tagKey: string; days: number;
  onResult: (b: { kind: "info" | "warn"; html: string } | null) => void;
};

// ---------- AI mode (editable values) ----------
function AiMode({ tagKey, days, onResult }: ModeProps) {
  const [conf, setConf] = useState(0.8);
  const [rows, setRows] = useState<Row[]>([]);
  const [vals, setVals] = useState<Record<number, string>>({});
  const [dry, setDry] = useState(false);
  const preview = useAiPreview({ params: { tag_key: tagKey, days, min_confidence: conf }, query: { enabled: false } });
  const apply = useTagSelected();

  const doPreview = async () => {
    const r = await preview.refetch();
    const wl = (r.data?.data.workloads ?? []) as Row[];
    setRows(wl);
    setVals(Object.fromEntries(wl.map((w, i) => [i, String(w.new_tag_value ?? "")])));
    const imp = (r.data?.data.impact ?? {}) as Row;
    onResult(wl.length === 0
      ? { kind: "warn", html: "No taggable workloads have a confident suggestion at this cutoff." }
      : { kind: "info", html: `<b>${num(imp.workloads).toLocaleString()}</b> workloads (${money(imp.cost)}) — edit any AI value, then apply.` });
  };

  const doApply = async () => {
    const workloads = rows.map((w, i) => ({
      workload_id: w.workload_id, product: w.product, workspace_id: w.workspace_id,
      workload_name: w.workload_name, is_serverless: w.is_serverless,
      tag_value: (vals[i] ?? "").trim(), cost: w.cost,
    })).filter((w) => w.tag_value);
    if (!workloads.length) return onResult({ kind: "warn", html: "No values to apply." });
    if (!dry && !confirm(`Apply tags for REAL to ${workloads.length} workload(s)?`)) return;
    const res = await apply.mutateAsync({ tag_key: tagKey, workloads, dry_run: dry });
    const d = res.data;
    onResult(d.run
      ? { kind: "info", html: `Batch <b>${d.batch_id}</b> — ${d.total_rows} queued, writer ${dry ? "dry-run" : "LIVE"}: <a class="underline" href="${(d.run as Row).url}" target="_blank">view run →</a>` }
      : { kind: "warn", html: d.message ?? "Nothing to do" });
    if (d.run) scrollToBatches();
  };

  return (
    <div className="space-y-4">
      <p className="text-sm text-muted-foreground">
        The AI suggests a value per untagged workload above the cutoff. <b>Edit any</b> before applying.
      </p>
      <div className="flex items-end gap-4">
        <div>
          <label className="mb-1 block text-xs font-semibold text-muted-foreground">Confidence cutoff</label>
          <select value={conf} onChange={(e) => setConf(Number(e.target.value))}
            className="h-9 rounded-md border bg-background px-3 text-sm">
            {[0.6, 0.7, 0.8, 0.9].map((v) => <option key={v} value={v}>{v}</option>)}
          </select>
        </div>
        <Button variant="secondary" onClick={doPreview} disabled={preview.isFetching}>
          1️⃣ Preview which workloads
        </Button>
      </div>
      {rows.length > 0 && (
        <>
          <div className="max-h-80 overflow-auto rounded-md border">
            <Table>
              <TableHeader><TableRow>
                <TableHead>Workload</TableHead><TableHead>Product</TableHead>
                <TableHead className="text-right">Cost</TableHead>
                <TableHead>Set value (editable)</TableHead><TableHead className="text-right">Conf</TableHead>
              </TableRow></TableHeader>
              <TableBody>
                {rows.map((w, i) => (
                  <TableRow key={i}>
                    <TableCell className="max-w-52 truncate">{String(w.workload_name ?? w.workload_id)}</TableCell>
                    <TableCell>{String(w.product)}</TableCell>
                    <TableCell className="text-right tabular-nums">{money(w.cost)}</TableCell>
                    <TableCell>
                      <Input value={vals[i] ?? ""} onChange={(e) => setVals({ ...vals, [i]: e.target.value })}
                        className="h-8 w-40" />
                    </TableCell>
                    <TableCell className="text-right tabular-nums">{String(w.confidence)}</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </div>
          <label className="flex items-center gap-2 text-sm">
            <Checkbox checked={dry} onCheckedChange={(v) => setDry(!!v)} /> Dry run only (writes nothing)
          </label>
          <Button onClick={doApply} disabled={apply.isPending}>2️⃣ Apply to these workloads</Button>
        </>
      )}
    </div>
  );
}

// ---------- Rules mode ----------
type Rule = { field: string; op: string; value: string; tagVal: string };
function RulesMode({ tagKey, days, onResult }: ModeProps) {
  const [rules, setRules] = useState<Rule[]>([{ field: "owner", op: "contains", value: "", tagVal: "" }]);
  const [rows, setRows] = useState<Row[]>([]);
  const [vals, setVals] = useState<Record<number, string>>({});
  const [checked, setChecked] = useState<Record<number, boolean>>({});
  const [dry, setDry] = useState(false);
  const [ownerOpts, setOwnerOpts] = useState<string[]>([]);
  const preview = useRulePreview();
  const apply = useTagSelected();

  const apiRules = () => rules.filter((r) => r.value && r.tagVal)
    .map((r) => ({ field: r.field, op: r.op, value: r.value, tags: { [tagKey]: r.tagVal } }));

  const doPreview = async () => {
    const rs = apiRules();
    if (!rs.length) return onResult({ kind: "warn", html: "Add at least one complete rule." });
    const res = await preview.mutateAsync({ tag_key: tagKey, days, rules: rs });
    const wl = (res.data.workloads ?? []) as Row[];
    setRows(wl);
    setVals(Object.fromEntries(wl.map((w, i) => [i, String(w.new_tag_value ?? "")])));
    setChecked(Object.fromEntries(wl.map((_, i) => [i, true])));
    const imp = (res.data.impact ?? {}) as Row;
    onResult({ kind: "info", html: `Matches <b>${num(imp.matched_count).toLocaleString()}</b> of ${num(imp.total_untagged).toLocaleString()} untagged (${money(imp.matched_cost)}). Uncheck any that don't belong; edit values.` });
  };

  const doApply = async () => {
    const workloads = rows.map((w, i) => ({
      workload_id: w.workload_id, product: w.product, workspace_id: w.workspace_id,
      workload_name: w.workload_name, is_serverless: w.is_serverless,
      tag_value: (vals[i] ?? "").trim(), cost: w.cost,
    })).filter((_, i) => checked[i]).filter((w) => w.tag_value);
    if (!workloads.length) return onResult({ kind: "warn", html: "No workloads checked." });
    if (!dry && !confirm(`Apply tags for REAL to ${workloads.length} workload(s)?`)) return;
    const res = await apply.mutateAsync({ tag_key: tagKey, workloads, dry_run: dry });
    const d = res.data;
    onResult(d.run
      ? { kind: "info", html: `Batch <b>${d.batch_id}</b> — ${d.total_rows} queued, ${dry ? "dry-run" : "LIVE"}: <a class="underline" href="${(d.run as Row).url}" target="_blank">view run →</a>` }
      : { kind: "warn", html: d.message ?? "Nothing matched" });
    if (d.run) scrollToBatches();
  };

  return (
    <div className="space-y-4">
      <p className="text-sm text-muted-foreground">Deterministic rules — no AI. Build rules, preview the exact workloads, apply only what you keep.</p>
      {rules.map((r, i) => (
        <div key={i} className="flex flex-wrap items-center gap-2">
          <select value={r.field} className="h-9 rounded-md border bg-background px-2 text-sm"
            onChange={async (e) => {
              const nr = [...rules]; nr[i].field = e.target.value; setRules(nr);
              const v = await fieldValues({ field: e.target.value, days });
              setOwnerOpts((v.data.values as Row[]).map((x) => String(x.value)));
            }}>
            {["owner", "name", "product", "workspace"].map((f) => <option key={f}>{f}</option>)}
          </select>
          <select value={r.op} className="h-9 rounded-md border bg-background px-2 text-sm"
            onChange={(e) => { const nr = [...rules]; nr[i].op = e.target.value; setRules(nr); }}>
            {["contains", "equals", "matches"].map((o) => <option key={o}>{o}</option>)}
          </select>
          <Input list="ownervals" placeholder="value" value={r.value} className="w-52"
            onChange={(e) => { const nr = [...rules]; nr[i].value = e.target.value; setRules(nr); }} />
          <span className="text-muted-foreground">→</span>
          <Input placeholder="tag value" value={r.tagVal} className="w-40"
            onChange={(e) => { const nr = [...rules]; nr[i].tagVal = e.target.value; setRules(nr); }} />
          <Button variant="ghost" size="sm" onClick={() => setRules(rules.filter((_, j) => j !== i))}>✕</Button>
        </div>
      ))}
      <datalist id="ownervals">{ownerOpts.map((o) => <option key={o} value={o} />)}</datalist>
      <div className="flex gap-2">
        <Button variant="secondary" size="sm" onClick={() => setRules([...rules, { field: "owner", op: "contains", value: "", tagVal: "" }])}>+ Add rule</Button>
        <Button variant="secondary" size="sm" onClick={doPreview} disabled={preview.isPending}>1️⃣ Show the workloads this will tag</Button>
      </div>
      {rows.length > 0 && (
        <>
          <div className="max-h-80 overflow-auto rounded-md border">
            <Table>
              <TableHeader><TableRow>
                <TableHead className="w-8"></TableHead><TableHead>Workload</TableHead>
                <TableHead>Product</TableHead><TableHead>Owner</TableHead>
                <TableHead className="text-right">Cost</TableHead><TableHead>Set value</TableHead>
              </TableRow></TableHeader>
              <TableBody>
                {rows.map((w, i) => (
                  <TableRow key={i}>
                    <TableCell><Checkbox checked={checked[i] ?? true} onCheckedChange={(v) => setChecked({ ...checked, [i]: !!v })} /></TableCell>
                    <TableCell className="max-w-44 truncate">{String(w.workload_name ?? w.workload_id)}</TableCell>
                    <TableCell>{String(w.product)}</TableCell>
                    <TableCell className="text-muted-foreground">{String(w.owner ?? "—")}</TableCell>
                    <TableCell className="text-right tabular-nums">{money(w.cost)}</TableCell>
                    <TableCell><Input value={vals[i] ?? ""} className="h-8 w-40"
                      onChange={(e) => setVals({ ...vals, [i]: e.target.value })} /></TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </div>
          <label className="flex items-center gap-2 text-sm">
            <Checkbox checked={dry} onCheckedChange={(v) => setDry(!!v)} /> Dry run only
          </label>
          <Button onClick={doApply} disabled={apply.isPending}>2️⃣ Apply tags to the checked workloads</Button>
        </>
      )}
    </div>
  );
}

// ---------- Manual mode ----------
function ManualMode({ tagKey, onResult }: ModeProps) {
  const [f, setF] = useState({ product: "", workload_id: "", workload_name: "", tag_value: "" });
  const [dry, setDry] = useState(false);
  const apply = useManualTag();
  const doApply = async () => {
    if (!f.product || !f.workload_id || !f.tag_value)
      return onResult({ kind: "warn", html: "Product, workload id, and tag value are required." });
    if (!dry && !confirm(`Tag workload ${f.workload_id} for REAL?`)) return;
    const res = await apply.mutateAsync({
      product: f.product.toUpperCase(), workload_id: f.workload_id, workload_name: f.workload_name,
      tag_key: tagKey, tag_value: f.tag_value, dry_run: dry,
    });
    const d = res.data;
    onResult(d.run
      ? { kind: "info", html: `Batch <b>${d.batch_id}</b> queued, ${dry ? "dry-run" : "LIVE"}: <a class="underline" href="${(d.run as Row).url}" target="_blank">view run →</a>` }
      : { kind: "warn", html: d.message ?? d.status ?? "Failed" });
    if (d.run) scrollToBatches();
  };
  return (
    <div className="space-y-3">
      <p className="text-sm text-muted-foreground">Tag a single workload with a value you type — no AI, no rules.</p>
      <div className="flex flex-wrap gap-3">
        <Input placeholder="Product (e.g. JOBS)" value={f.product} onChange={(e) => setF({ ...f, product: e.target.value })} className="w-40" />
        <Input placeholder="Workload id" value={f.workload_id} onChange={(e) => setF({ ...f, workload_id: e.target.value })} className="w-64" />
        <Input placeholder="Name (optional)" value={f.workload_name} onChange={(e) => setF({ ...f, workload_name: e.target.value })} className="w-48" />
        <Input placeholder="Tag value" value={f.tag_value} onChange={(e) => setF({ ...f, tag_value: e.target.value })} className="w-40" />
      </div>
      <label className="flex items-center gap-2 text-sm"><Checkbox checked={dry} onCheckedChange={(v) => setDry(!!v)} /> Dry run only</label>
      <Button onClick={doApply} disabled={apply.isPending}>Tag this workload</Button>
    </div>
  );
}

// ---------- Not-taggable panel ----------
const REASON: Record<string, { label: string; color: string; action: string }> = {
  POLICY_GOVERNED: { label: "Attributed via budget policy (serverless)", color: "border-amber-500",
    action: "Apps, serverless SQL, model serving, pipelines, Lakebase — assign a budget policy (UI or databricks_budget_policy Terraform), not a per-resource tag." },
  UI_ONLY: { label: "Set in the pipeline definition", color: "border-sky-500",
    action: "DLT/SDP tags live in clusters[].custom_tags in the pipeline definition." },
  NO_API: { label: "No documented tag path", color: "border-red-500",
    action: "No per-resource tag API or budget-policy coverage documented — check the resource UI." },
};
function NotTaggable({ tagKey, days }: { tagKey: string; days: number }) {
  const { data } = useNotTaggable({ params: { tag_key: tagKey, days } });
  const rows = (data?.data.rows ?? []) as Row[];
  if (!rows.length) return null;
  const groups = rows.reduce<Record<string, Row[]>>((acc, r) => {
    (acc[String(r.reason)] ||= []).push(r); return acc;
  }, {});
  return (
    <section>
      <h2 className="mb-3 text-xs font-bold uppercase tracking-wider text-muted-foreground">
        Tagged a different way <span className="normal-case font-normal">(not a per-resource tag write)</span>
      </h2>
      <div className="space-y-3">
        {Object.entries(groups).map(([reason, grp]) => {
          const info = REASON[reason] ?? REASON.NO_API;
          const cost = grp.reduce((s, x) => s + num(x.cost), 0);
          const wl = grp.reduce((s, x) => s + num(x.workloads), 0);
          return (
            <Card key={reason} className={`border-l-4 p-4 ${info.color}`}>
              <div className="font-semibold">{info.label} — {wl.toLocaleString()} workloads · {money(cost)}</div>
              <p className="mt-1 text-sm text-muted-foreground">{info.action}</p>
            </Card>
          );
        })}
      </div>
    </section>
  );
}

// Drill-in shown under a batch row: per-(product, reason) breakdown of WHY rows
// didn't tag — PermissionDenied, ResourceDoesNotExist, UNSUPPORTED product, etc.
// Answers "when stuff really doesn't get tagged, can we know why?" with the real
// error, not just a count.
function WhyDetail({ batchId }: { batchId: string }) {
  const { data, isLoading } = useBatchDetail({ params: { batch_id: batchId } });
  const rows = (data?.data.rows ?? []) as Row[];
  if (isLoading) return <div className="py-2 text-xs text-muted-foreground">Loading reasons…</div>;
  if (!rows.length) return <div className="py-2 text-xs text-muted-foreground">No failure detail recorded.</div>;
  return (
    <div className="py-1">
      <div className="mb-1 text-xs font-semibold text-muted-foreground">Why these didn’t tag</div>
      <Table>
        <TableHeader><TableRow>
          <TableHead>Product</TableHead><TableHead>Status</TableHead>
          <TableHead className="text-right">Rows</TableHead>
          <TableHead className="text-right">Cost</TableHead><TableHead>Reason</TableHead>
        </TableRow></TableHeader>
        <TableBody>
          {rows.map((r, i) => (
            <TableRow key={i}>
              <TableCell className="text-sm">{String(r.product ?? "")}</TableCell>
              <TableCell>
                <Badge variant={String(r.status) === "FAILED" ? "destructive" : "secondary"}>
                  {String(r.status ?? "")}
                </Badge>
              </TableCell>
              <TableCell className="text-right tabular-nums">{num(r.rows)}</TableCell>
              <TableCell className="text-right tabular-nums">{money(r.cost)}</TableCell>
              <TableCell className="text-xs" title={String(r.sample_reason ?? "")}>
                {String(r.reason ?? r.sample_reason ?? "unknown")}
              </TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
      <div className="mt-1 text-xs text-muted-foreground">
        FAILED = usually a permission or deleted-resource issue (the writer identity
        can’t manage that resource); “can’t tag” = the product is attributed a
        different way (budget policy / pipeline definition). Hover a reason for the full message.
      </div>
    </div>
  );
}

// ---------- Batches + live progress + rollback ----------
function Batches() {
  // Poll fast (2s) while any job is in flight so tagging fills / rollback drains live.
  const { data, refetch } = useBatches({ params: { limit: 15 }, query: { refetchInterval: 2000 } });
  const rollback = useRollback();
  const [links, setLinks] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState<Record<string, string>>({}); // batch_id -> status text
  const [why, setWhy] = useState<Record<string, boolean>>({}); // batch_id -> show failure reasons
  const batches = (data?.data.batches ?? []) as Row[];

  const doRollback = async (id: string) => {
    if (!confirm(`Roll back batch ${id} for REAL?\n\nRemoves the tags it applied (restoring prior values) and makes those workloads re-taggable. The bar drains toward 0%.`)) return;
    setBusy((b) => ({ ...b, [id]: "starting rollback…" }));
    try {
      const res = await rollback.mutateAsync({ batch_id: id, dry_run: false });
      const url = (res.data.run as Row | undefined)?.url;
      if (url) setLinks((l) => ({ ...l, [id]: String(url) }));
      setBusy((b) => ({ ...b, [id]: "rolling back…" }));
    } catch (e) {
      setBusy((b) => ({ ...b, [id]: "rollback failed — check permissions" }));
      return;
    }
    refetch();
  };

  return (
    <section id="batches-section">
      <h2 className="mb-3 text-xs font-bold uppercase tracking-wider text-muted-foreground">Batches &amp; rollback</h2>
      <Card className="p-0">
        <Table>
          <TableHeader><TableRow>
            <TableHead>Batch</TableHead><TableHead className="text-right">Cost</TableHead>
            <TableHead className="w-48">Progress</TableHead><TableHead>Outcome</TableHead><TableHead></TableHead>
          </TableRow></TableHeader>
          <TableBody>
            {batches.map((b) => {
              const id = String(b.batch_id);
              const total = num(b.rows);
              const tagged = num(b.succeeded);
              const rolledBack = num(b.rolled_back);
              const pending = num(b.pending);
              const running = num(b.running);
              const inFlight = pending > 0 || running > 0;
              // A rollback drains `tagged` toward 0. Show progress as the fraction
              // reverted out of what was ever tagged (tagged + rolled_back), so the
              // bar visibly empties as rows flip SUCCEEDED -> ROLLED_BACK live.
              const everTagged = tagged + rolledBack;
              const rollbackMode = rolledBack > 0 || (busy[id] && everTagged > 0);
              const pct = rollbackMode
                ? (everTagged ? Math.round((100 * rolledBack) / everTagged) : 100)
                : (total ? Math.round((100 * tagged) / total) : 0);
              // Clear the "rolling back…" note once the run is no longer in flight.
              // (Do NOT wait for tagged===0 — removals can legitimately fail when a
              // resource was deleted, leaving some rows tagged forever.)
              if (busy[id] && !inFlight && rolledBack > 0) {
                queueMicrotask(() => setBusy((x) => { const n = { ...x }; delete n[id]; return n; }));
              }
              const parts: string[] = [];
              if (tagged) parts.push(`${tagged} tagged`);
              if (rolledBack) parts.push(`${rolledBack} rolled back`);
              if (num(b.failed)) parts.push(`${num(b.failed)} failed`);
              if (num(b.unsupported)) parts.push(`${num(b.unsupported)} can't tag`);
              if (pending) parts.push(`${pending} not run yet`);
              if (running) parts.push(`${running} running…`);
              // Honest note when a rollback couldn't fully revert.
              const residual = rolledBack > 0 && tagged > 0 && !inFlight;
              // How many rows didn't get tagged (so we can offer a "Why?" drill-in).
              const notTagged = num(b.failed) + num(b.unsupported);
              return (
                <Fragment key={id}>
                <TableRow>
                  <TableCell><Badge variant="secondary">{id}</Badge></TableCell>
                  <TableCell className="text-right tabular-nums">{money(b.cost)}</TableCell>
                  <TableCell>
                    <Progress value={pct} className="h-1.5" />
                    <span className="text-xs text-muted-foreground">
                      {rollbackMode
                        ? `${rolledBack}/${everTagged} reverted · ${pct}%`
                        : `${tagged}/${total} tagged · ${pct}%`}
                      {busy[id] ? ` · ${busy[id]}` : ""}
                    </span>
                    {residual && (
                      <span className="block text-xs text-amber-600">
                        {tagged} couldn’t be removed (resource deleted or removal failed)
                      </span>
                    )}
                  </TableCell>
                  <TableCell className="text-sm">
                    {parts.join(" · ") || "—"}
                    {notTagged > 0 && (
                      <button
                        className="ml-2 text-xs underline text-muted-foreground hover:text-foreground"
                        onClick={() => setWhy((w) => ({ ...w, [id]: !w[id] }))}>
                        {why[id] ? "hide why" : `why? (${notTagged})`}
                      </button>
                    )}
                  </TableCell>
                  <TableCell className="whitespace-nowrap">
                    {tagged > 0 && (
                      <Button variant="secondary" size="sm" disabled={!!busy[id]}
                        onClick={() => doRollback(id)}>Rollback</Button>
                    )}
                    {links[id] && (
                      <a className="ml-2 text-xs underline" href={links[id]} target="_blank" rel="noreferrer">view run →</a>
                    )}
                  </TableCell>
                </TableRow>
                {why[id] && (
                  <TableRow>
                    <TableCell colSpan={5} className="bg-muted/30">
                      <WhyDetail batchId={id} />
                    </TableCell>
                  </TableRow>
                )}
                </Fragment>
              );
            })}
          </TableBody>
        </Table>
      </Card>
    </section>
  );
}
