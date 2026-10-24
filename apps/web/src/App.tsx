import { useCallback, useEffect, useMemo, useState } from "react";
import { MaskEditor } from "./MaskEditor";
import {
  api,
  artifactUrl,
  type Artifact,
  type Asset,
  type CapabilityMatrixRow,
  type Health,
  type InputSpec,
  type Job,
  type JobState,
  type Maturity,
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

const LEVELS: Maturity[] = ["L0", "L1", "L2", "L3"];

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
  const [matrix, setMatrix] = useState<CapabilityMatrixRow[]>([]);
  const [tab, setTab] = useState<"jobs" | "gallery" | "matrix">("jobs");
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

  // 能力矩阵：进「矩阵」tab 时刷新，其余时间不动
  const loadMatrix = useCallback(async () => {
    try {
      setMatrix(await api.matrix());
    } catch (e) {
      setNotice(String(e));
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

  // 最新一个 running 任务（列表按创建时间倒序，find 取到的就是最新）
  const runningJob = useMemo(() => jobs.find((j) => j.state === "running") ?? null, [jobs]);
  // 对它开 SSE 实时刷进度；出错（含服务端正常断流）静默关闭、退回轮询
  useEffect(() => {
    if (!runningJob) return;
    const es = new EventSource(`/v1/jobs/${runningJob.id}/events`);
    es.onmessage = (ev) => {
      try {
        const next = JSON.parse(ev.data) as Job;
        setJobs((prev) => prev.map((j) => (j.id === next.id ? next : j)));
      } catch {
        // 坏帧忽略，2 秒轮询会兜底
      }
    };
    es.onerror = () => es.close();
    return () => es.close();
  }, [runningJob?.id]);

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

  // 顶部一行汇总：L0 x · L1 x · L2 x · L3 x，让「缺什么」一眼可见
  const levelCounts = useMemo(() => {
    const c: Record<Maturity, number> = { L0: 0, L1: 0, L2: 0, L3: 0 };
    for (const row of matrix) c[row.level] += 1;
    return c;
  }, [matrix]);

  // 蒙版画布的源图：优先取名为 image 的输入，没有就取第一个 image 类型的输入
  const sourceImageName = useMemo(() => {
    if (!variant) return "";
    const entries = Object.entries(variant.inputs);
    const named = entries.find(([n, s]) => n === "image" && s.type === "image");
    return named?.[0] ?? entries.find(([, s]) => s.type === "image")?.[0] ?? "";
  }, [variant]);

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
                // 蒙版要画在「同一变体的原图」上：取第一个 image 输入
                maskSource={assets[sourceImageName]}
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
            <button
              className={tab === "matrix" ? "tab active" : "tab"}
              onClick={() => {
                setTab("matrix");
                loadMatrix();
              }}
            >
              矩阵 <span className="count">{matrix.length}</span>
            </button>
          </div>

          {tab === "jobs" ? (
            <div className="joblist">
              {jobs.length === 0 && <div className="empty">还没有任务</div>}
              {jobs.map((job) => (
                <JobRow key={job.id} job={job} onCancel={() => cancel(job.id)} onDelete={() => remove(job.id)} />
              ))}
            </div>
          ) : tab === "gallery" ? (
            <div className="gallery">
              {gallery.length === 0 && <div className="empty">还没有作品</div>}
              {gallery.map(({ job, artifact }) => (
                <div key={artifact.id} className="card">
                  {/* 图片点开看大图；音视频直接内联播放，套 <a> 会误触跳转 */}
                  {artifact.kind === "image" ? (
                    <a
                      href={artifactUrl(artifact)}
                      target="_blank"
                      rel="noreferrer"
                      className="card-media"
                    >
                      <ArtifactMedia artifact={artifact} />
                    </a>
                  ) : (
                    <div className="card-media">
                      {artifact.kind === "audio" && (
                        <span className="media-label">
                          {String(job.params.voice ?? "克隆")}
                        </span>
                      )}
                      <ArtifactMedia artifact={artifact} />
                    </div>
                  )}
                  <div className="card-meta">
                    <span>{artifactSpec(artifact)}</span>
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
          ) : (
            <div className="matrix">
              <div className="matrix-summary">
                {LEVELS.map((lv) => (
                  <span key={lv} className={`m-badge ${lv}`}>
                    {lv} × {levelCounts[lv]}
                  </span>
                ))}
              </div>
              <div className="matrix-grid">
                {matrix.length === 0 && <div className="empty">还没有矩阵数据</div>}
                {matrix.map((row) => (
                  <div key={row.capability} className="mcard">
                    <div className="mcard-head">
                      <span className="m-title">{row.title}</span>
                      <span className={`m-badge ${row.level}`}>{row.level}</span>
                    </div>
                    <div className="m-sub">
                      {row.stage && <span className="mstage">{row.stage}</span>}
                      <span className="m-cap">{row.capability}</span>
                    </div>
                    {row.note && <p className="m-note">{row.note}</p>}
                    {row.variants.length > 0 && (
                      <div className="m-chips">
                        {row.variants.map((v) => (
                          <span key={v.variant} className="chip">
                            {v.variant} · {v.engine}
                          </span>
                        ))}
                      </div>
                    )}
                  </div>
                ))}
              </div>
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
  maskSource,
}: {
  name: string;
  spec: InputSpec;
  value: string;
  onChange: (name: string, value: string) => void;
  asset?: Asset;
  uploading?: boolean;
  onFile?: (name: string, file: File) => void;
  /** mask 字段要用它当画布底图 */
  maskSource?: Asset;
}) {
  // 蒙版：不是上传文件，而是在原图上涂抹后自动导出 PNG
  if (spec.type === "mask") {
    return (
      <div className="field">
        <span>
          {name}
          {spec.required && <em> *</em>}
        </span>
        <MaskEditor source={maskSource} assetId={value} onChange={(id) => onChange(name, id)} />
      </div>
    );
  }

  // 图片 / 音频输入：选择文件 → 立即上传 → 预览，表单值存 asset_id
  if (spec.type === "image" || spec.type === "audio") {
    const isAudio = spec.type === "audio";
    return (
      <label className="field">
        <span>
          {name}
          {spec.required && <em> *</em>}
        </span>
        <input
          type="file"
          accept={isAudio ? "audio/wav,audio/mpeg,audio/flac,audio/ogg,audio/mp4" : "image/png,image/jpeg,image/webp"}
          disabled={uploading}
          onChange={(e) => {
            const f = e.target.files?.[0];
            if (f && onFile) onFile(name, f);
          }}
        />
        {uploading && <p className="hint small">上传中…</p>}
        {asset && !uploading && (
          <div className="asset-preview">
            {isAudio ? (
              <audio controls src={asset.url} />
            ) : (
              <img src={asset.url} alt={asset.filename} />
            )}
            <span className="asset-name">
              {asset.filename}
              {asset.width && asset.height ? ` · ${asset.width}×${asset.height}` : ""}
            </span>
          </div>
        )}
      </label>
    );
  }

  // 长文本：TTS 台词这类，给个大输入框
  if (spec.type === "text") {
    return (
      <label className="field">
        <span>
          {name}
          {spec.required && <em> *</em>}
        </span>
        <textarea
          rows={5}
          value={value}
          placeholder="要合成的文本"
          onChange={(e) => onChange(name, e.target.value)}
        />
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

  // 有固定候选值的参数（音色之类）渲染成下拉，避免手打错名字
  if (spec.options && spec.options.length > 0) {
    return (
      <label className="field">
        <span>
          {name}
          {spec.required && <em> *</em>}
        </span>
        <select value={value} onChange={(e) => onChange(name, e.target.value)}>
          {spec.options.map((opt) => (
            <option key={opt} value={opt}>
              {opt}
            </option>
          ))}
        </select>
        {spec.type === "voice" && (
          <p className="hint small">候选来自音色目录（config.txt + 目录里的 wav/txt）；新增音色后刷新页面</p>
        )}
      </label>
    );
  }

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

/** 按产物类型渲染：图片缩略图 / 音频播放器 / 视频播放器 */
function ArtifactMedia({ artifact, className }: { artifact: Artifact; className?: string }) {
  const url = artifactUrl(artifact);
  if (artifact.kind === "audio")
    // preload=metadata：卡片上直接显示时长，不用等用户点播放
    return <audio className={className} controls preload="metadata" src={url} />;
  if (artifact.kind === "video")
    return <video className={className} controls playsInline src={url} />;
  return <img className={className} src={url} alt={fileName(artifact.path)} loading="lazy" />;
}

/** 产物规格：图片是宽高，音视频是时长 */
function artifactSpec(a: Artifact): string {
  if (a.kind === "image") return a.width && a.height ? `${a.width}×${a.height}` : "";
  return a.duration ? `${a.duration.toFixed(1)}s` : "";
}

function JobRow({ job, onCancel, onDelete }: { job: Job; onCancel: () => void; onDelete: () => void }) {
  const [open, setOpen] = useState(false);
  // 图片任务用 prompt，TTS 用 text；抠图/超分这类没有提示词，交给下面的占位符
  const promptSource = job.params.prompt ?? job.params.text;
  const prompt =
    typeof promptSource === "string"
      ? promptSource
      : promptSource
        ? JSON.stringify(promptSource)
        : "";
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
        {job.artifacts[0] && job.artifacts[0].kind === "image" && (
          <img className="thumb" src={artifactUrl(job.artifacts[0])} alt="" loading="lazy" />
        )}
        {job.artifacts[0] && job.artifacts[0].kind !== "image" && (
          <span className="thumb-tag">[{job.artifacts[0].kind}]</span>
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

      {job.state === "running" && <ProgressBar progress={job.progress} />}

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

/** running 任务的细进度条：宽度 = progress*100%，旁边带百分比数字 */
function ProgressBar({ progress }: { progress: number }) {
  const pct = Math.min(100, Math.round(progress * 100));
  return (
    <div className="job-progress">
      <div className="job-progress-track">
        <div className="job-progress-fill" style={{ width: `${pct}%` }} />
      </div>
      <span className="job-progress-pct">{pct}%</span>
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
