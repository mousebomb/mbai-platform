import { useCallback, useEffect, useMemo, useState } from "react";
import {
  api,
  artifactUrl,
  type Asset,
  type Health,
  type InputSpec,
  type Job,
  type JobState,
  type VariantInfo,
} from "./api";

const STATE_LABEL: Record<JobState, string> = {
  queued: "排队中",
  running: "生成中",
  succeeded: "完成",
  failed: "失败",
  cancelled: "已取消",
};

const TERMINAL: JobState[] = ["succeeded", "failed", "cancelled"];

function duration(job: Job): string {
  const end = job.finished_at ?? (job.state === "running" ? Date.now() / 1000 : null);
  if (!job.started_at || !end) return "-";
  return `${(end - job.started_at).toFixed(1)}s`;
}

function fileName(path: string): string {
  return path.split(/[\\/]/).pop() ?? path;
}

export default function App() {
  const [health, setHealth] = useState<Health | null>(null);
  const [variants, setVariants] = useState<VariantInfo[]>([]);
  const [selected, setSelected] = useState<string>("");
  const [form, setForm] = useState<Record<string, string>>({});
  // image 字段的已上传素材与上传中标记（按参数名存）
  const [assets, setAssets] = useState<Record<string, Asset>>({});
  const [uploading, setUploading] = useState<Record<string, boolean>>({});
  const [jobs, setJobs] = useState<Job[]>([]);
  const [tab, setTab] = useState<"jobs" | "gallery">("jobs");
  const [notice, setNotice] = useState<string | null>(null);
  const [offline, setOffline] = useState(false);

  const variant = useMemo(
    () => variants.find((v) => `${v.capability}/${v.variant}` === selected),
    [variants, selected]
  );

  // 变体切换时用 manifest 里的默认值重置表单
  useEffect(() => {
    if (!variant) return;
    const next: Record<string, string> = {};
    for (const [name, spec] of Object.entries(variant.inputs)) {
      next[name] = spec.default === undefined ? "" : String(spec.default);
    }
    setForm(next);
    setAssets({});
    setUploading({});
  }, [variant]);

  const loadHealth = useCallback(async () => {
    try {
      const h = await api.health();
      setHealth(h);
      setOffline(false);
      return h;
    } catch {
      setOffline(true);
      return null;
    }
  }, []);

  useEffect(() => {
    (async () => {
      const h = await loadHealth();
      if (!h) return;
      try {
        const caps = await api.capabilities();
        setVariants(caps);
        if (caps.length) setSelected(`${caps[0].capability}/${caps[0].variant}`);
      } catch (e) {
        setNotice(String(e));
      }
    })();
  }, [loadHealth]);

  // 队列轮询：2 秒一次，页面关掉任务照跑
  const refreshJobs = useCallback(async () => {
    try {
      setJobs(await api.jobs(60));
      setOffline(false);
    } catch {
      setOffline(true);
    }
  }, []);

  useEffect(() => {
    refreshJobs();
    const timer = setInterval(refreshJobs, 2000);
    const healthTimer = setInterval(loadHealth, 15000);
    return () => {
      clearInterval(timer);
      clearInterval(healthTimer);
    };
  }, [refreshJobs, loadHealth]);

  function setField(name: string, value: string) {
    setForm((prev) => ({ ...prev, [name]: value }));
  }

  /** image 字段选中文件后立即上传，表单里存的是 asset_id */
  async function uploadFile(name: string, file: File) {
    setUploading((prev) => ({ ...prev, [name]: true }));
    setNotice(null);
    try {
      const asset = await api.uploadAsset(file);
      setAssets((prev) => ({ ...prev, [name]: asset }));
      setField(name, asset.id);
    } catch (e: unknown) {
      const detail = (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail;
      setNotice(detail ?? String(e));
    } finally {
      setUploading((prev) => ({ ...prev, [name]: false }));
    }
  }

  async function submit() {
    if (!variant) return;
    setNotice(null);

    const params: Record<string, unknown> = {};
    for (const [name, spec] of Object.entries(variant.inputs)) {
      const raw = (form[name] ?? "").trim();
      if (!raw) {
        if (spec.required) {
          setNotice(`请填写 ${name}`);
          return;
        }
        continue;
      }
      try {
        params[name] = coerce(name, spec, raw);
      } catch (e) {
        setNotice(e instanceof Error ? e.message : String(e));
        return;
      }
    }

    try {
      await api.createJob({ capability: variant.capability, variant: variant.variant, params });
      setTab("jobs");
      await refreshJobs();
    } catch (e: unknown) {
      const detail = (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail;
      setNotice(detail ?? String(e));
    }
  }

  async function cancel(id: string) {
    try {
      await api.cancelJob(id);
      await refreshJobs();
    } catch (e) {
      setNotice(String(e));
    }
  }

  /** 删除任务与磁盘产物，不可恢复，所以先确认 */
  async function remove(id: string) {
    if (!window.confirm("删除这个任务和它的产物文件？不可恢复。")) return;
    try {
      await api.deleteJob(id);
      await refreshJobs();
    } catch (e: unknown) {
      const detail = (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail;
      setNotice(detail ?? String(e));
    }
  }

  const gallery = useMemo(
    () =>
      jobs
        .filter((j) => j.state === "succeeded")
        .flatMap((j) => j.artifacts.map((a) => ({ job: j, artifact: a })))
        .sort((a, b) => b.artifact.created_at - a.artifact.created_at),
    [jobs]
  );

  const gpu = health?.engine.devices?.[0];
  const engineOk = health?.engine.ok ?? false;

  return (
    <div className="app">
      <header className="topbar">
        <div className="brand">
          <span className="logo">◈</span>
          <div>
            <div className="title">AI Platform</div>
            <div className="subtitle">统一模型池 · 统一队列</div>
          </div>
        </div>
        <div className="status">
          <span className={`dot ${engineOk && !offline ? "ok" : "bad"}`} />
          <span>
            {offline ? "接口断开" : engineOk ? `引擎在线 · ${gpu?.name.replace("cuda:0 ", "") ?? ""}` : "引擎离线"}
          </span>
          {gpu && <span className="vram">{(gpu.vram_total / 1e9).toFixed(1)} GB</span>}
          <span className="variants">{health?.variants ?? 0} 个变体</span>
        </div>
      </header>

      {health?.registry_errors?.length ? (
        <div className="banner bad">注册表错误：{health.registry_errors.join("；")}</div>
      ) : null}
      {health?.missing_models?.length ? (
        <div className="banner bad">缺模型文件：{health.missing_models.join("、")}</div>
      ) : null}

      <main className="layout">
        <section className="panel composer">
          <div className="panel-head">生成</div>

          <label className="field">
            <span>能力变体</span>
            <select value={selected} onChange={(e) => setSelected(e.target.value)}>
              {variants.map((v) => (
                <option key={`${v.capability}/${v.variant}`} value={`${v.capability}/${v.variant}`}>
                  {v.title || `${v.capability} · ${v.variant}`}
                </option>
              ))}
            </select>
          </label>
          {variant?.description && <p className="hint">{variant.description}</p>}

          {variant &&
            Object.entries(variant.inputs).map(([name, spec]) => (
              <Field
                key={name}
                name={name}
                spec={spec}
                value={form[name] ?? ""}
                onChange={setField}
                asset={assets[name]}
                uploading={!!uploading[name]}
                onFile={uploadFile}
              />
            ))}

          {notice && <div className="notice">{notice}</div>}

          <button className="primary" onClick={submit} disabled={!variant || !engineOk || offline}>
            {engineOk ? "提交到队列" : "引擎离线"}
          </button>
          <p className="hint small">任务进队列后页面可关，回来看结果即可。</p>
        </section>

        <section className="panel results">
          <div className="tabs">
            <button className={tab === "jobs" ? "tab active" : "tab"} onClick={() => setTab("jobs")}>
              任务 <span className="count">{jobs.length}</span>
            </button>
            <button className={tab === "gallery" ? "tab active" : "tab"} onClick={() => setTab("gallery")}>
              作品 <span className="count">{gallery.length}</span>
            </button>
          </div>

          {tab === "jobs" ? (
            <div className="joblist">
              {jobs.length === 0 && <div className="empty">还没有任务</div>}
              {jobs.map((job) => (
                <JobRow key={job.id} job={job} onCancel={() => cancel(job.id)} onDelete={() => remove(job.id)} />
              ))}
            </div>
          ) : (
            <div className="gallery">
              {gallery.length === 0 && <div className="empty">还没有作品</div>}
              {gallery.map(({ job, artifact }) => (
                <div key={artifact.id} className="card">
                  <a href={artifactUrl(artifact)} target="_blank" rel="noreferrer">
                    <img src={artifactUrl(artifact)} alt={fileName(artifact.path)} loading="lazy" />
                  </a>
                  <div className="card-meta">
                    <span>{artifact.width}×{artifact.height}</span>
                    <span>{job.variant}</span>
                  </div>
                  <button
                    className="card-del"
                    title="删除这张作品"
                    onClick={() => remove(job.id)}
                  >
                    ×
                  </button>
                </div>
              ))}
            </div>
          )}
        </section>
      </main>
    </div>
  );
}

function Field({
  name,
  spec,
  value,
  onChange,
  asset,
  uploading,
  onFile,
}: {
  name: string;
  spec: InputSpec;
  value: string;
  onChange: (name: string, value: string) => void;
  asset?: Asset;
  uploading?: boolean;
  onFile?: (name: string, file: File) => void;
}) {
  // 图片输入：选择文件 → 立即上传 → 显示缩略图，表单值存 asset_id
  if (spec.type === "image") {
    return (
      <label className="field">
        <span>
          {name}
          {spec.required && <em> *</em>}
        </span>
        <input
          type="file"
          accept="image/png,image/jpeg,image/webp"
          disabled={uploading}
          onChange={(e) => {
            const f = e.target.files?.[0];
            if (f && onFile) onFile(name, f);
          }}
        />
        {uploading && <p className="hint small">上传中…</p>}
        {asset && !uploading && (
          <div className="asset-preview">
            <img src={asset.url} alt={asset.filename} />
            <span className="asset-name">
              {asset.filename}
              {asset.width && asset.height ? ` · ${asset.width}×${asset.height}` : ""}
            </span>
          </div>
        )}
      </label>
    );
  }

  if (spec.type === "prompt") {
    return (
      <label className="field">
        <span>
          {name}
          {spec.required && <em> *</em>}
        </span>
        <textarea
          rows={6}
          value={value}
          placeholder={'描述画面；以 { 开头则按 Layout JSON 解析，例如 {"summary":{"caption":"..."},"items":[...]}'}
          onChange={(e) => onChange(name, e.target.value)}
        />
      </label>
    );
  }

  const isNumber = spec.type === "int" || spec.type === "float";
  return (
    <label className="field">
      <span>
        {name}
        {isNumber && spec.min !== undefined && (
          <em className="range">
            {spec.min}~{spec.max ?? "∞"}
            {spec.multiple_of ? ` / ${spec.multiple_of} 的倍数` : ""}
          </em>
        )}
      </span>
      {isNumber ? (
        <input
          type="number"
          value={value}
          step={spec.multiple_of ?? (spec.type === "float" ? 0.1 : 1)}
          min={spec.min}
          max={spec.max}
          onChange={(e) => onChange(name, e.target.value)}
          onBlur={(e) => {
            // 失焦就把值收拢到合法范围，避免提交时才被后端 422 打回
            const next = normalizeNumber(e.target.value, spec);
            if (next !== e.target.value) onChange(name, next);
          }}
        />
      ) : (
        <input value={value} onChange={(e) => onChange(name, e.target.value)} />
      )}
    </label>
  );
}

function JobRow({ job, onCancel, onDelete }: { job: Job; onCancel: () => void; onDelete: () => void }) {
  const [open, setOpen] = useState(false);
  const prompt = typeof job.params.prompt === "string" ? job.params.prompt : JSON.stringify(job.params.prompt);
  const done = TERMINAL.includes(job.state);

  return (
    <div className={`job ${job.state}`} onClick={() => setOpen((v) => !v)}>
      <div className="job-main">
        <span className={`pill ${job.state}`}>{STATE_LABEL[job.state]}</span>
        <div className="job-text">
          <div className="job-prompt">{prompt || "(无提示词)"}</div>
          <div className="job-sub">
            {job.variant} · {duration(job)} · {new Date(job.created_at * 1000).toLocaleTimeString()}
          </div>
        </div>
        {job.artifacts[0] && (
          <img className="thumb" src={artifactUrl(job.artifacts[0])} alt="" loading="lazy" />
        )}
        {!done && (
          <button
            className="ghost"
            onClick={(e) => {
              e.stopPropagation();
              onCancel();
            }}
          >
            取消
          </button>
        )}
        {done && (
          <button
            className="ghost danger"
            title="删除任务与产物文件"
            onClick={(e) => {
              e.stopPropagation();
              onDelete();
            }}
          >
            删除
          </button>
        )}
      </div>

      {open && (
        <div className="job-detail">
          {job.error && <div className="error">{job.error}</div>}
          <pre>{JSON.stringify(job.params, null, 2)}</pre>
          <div className="job-id">{job.id}</div>
        </div>
      )}
    </div>
  );
}

/** 数字输入失焦时收拢到合法值：先按 multiple_of 取最近倍数，再夹到 [min, max] */
function normalizeNumber(raw: string, spec: InputSpec): string {
  if (!raw.trim()) return raw;
  const n = Number(raw);
  // 不是数字就原样留着，提交时 coerce 会给出明确报错，不要偷偷改成别的值
  if (!Number.isFinite(n)) return raw;

  const m = spec.type === "int" ? spec.multiple_of : undefined;
  let v = spec.type === "int" ? Math.trunc(n) : n;
  if (m) v = Math.round(v / m) * m;
  if (spec.min !== undefined && v < spec.min) v = m ? Math.ceil(spec.min / m) * m : spec.min;
  if (spec.max !== undefined && v > spec.max) v = m ? Math.floor(spec.max / m) * m : spec.max;
  return String(v);
}

/** 把表单字符串按 manifest 里的类型还原成 API 需要的值 */
function coerce(name: string, spec: InputSpec, raw: string): unknown {
  if (spec.type === "int") {
    const n = Number(raw);
    if (!Number.isInteger(n)) throw new Error(`${name} 需要整数`);
    return n;
  }
  if (spec.type === "float") {
    const n = Number(raw);
    if (Number.isNaN(n)) throw new Error(`${name} 需要数字`);
    return n;
  }
  if (spec.type === "prompt" && raw.startsWith("{")) {
    try {
      return JSON.parse(raw);
    } catch {
      throw new Error("Layout JSON 格式错误，检查引号和括号");
    }
  }
  return raw;
}
