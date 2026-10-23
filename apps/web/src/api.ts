import axios from "axios";

export type JobState = "queued" | "running" | "succeeded" | "failed" | "cancelled";

export interface Artifact {
  id: string;
  job_id: string;
  kind: "image" | "video" | "audio";
  path: string;
  width: number | null;
  height: number | null;
  created_at: number;
}

export interface Job {
  id: string;
  capability: string;
  variant: string;
  model_id: string;
  params: Record<string, unknown>;
  state: JobState;
  progress: number;
  engine_ref: string | null;
  error: string | null;
  artifacts: Artifact[];
  created_at: number;
  started_at: number | null;
  finished_at: number | null;
}

export interface InputSpec {
  type: "string" | "int" | "float" | "prompt";
  required?: boolean;
  default?: string | number;
  min?: number;
  max?: number;
  multiple_of?: number;
}

export interface VariantInfo {
  capability: string;
  variant: string;
  version: string;
  model_id: string;
  title: string;
  description: string;
  inputs: Record<string, InputSpec>;
}

export interface Health {
  engine: { url: string; ok: boolean; error?: string; devices?: { name: string; vram_total: number }[] };
  registry_errors: string[];
  missing_models: string[];
  variants: number;
}

const http = axios.create({ baseURL: "/v1", timeout: 20000 });

export const api = {
  health: () => http.get<Health>("/health").then((r) => r.data),
  capabilities: () => http.get<VariantInfo[]>("/capabilities").then((r) => r.data),
  jobs: (limit = 50) => http.get<Job[]>("/jobs", { params: { limit } }).then((r) => r.data),
  createJob: (body: { capability: string; variant: string; params: Record<string, unknown> }) =>
    http.post<Job>("/jobs", body).then((r) => r.data),
  cancelJob: (id: string) => http.post<Job>(`/jobs/${id}/cancel`).then((r) => r.data),
};

/** 产物在磁盘上的完整路径 → HTTP 文件名（后端只暴露任务目录下的直接子文件） */
export function artifactUrl(a: Artifact): string {
  const name = a.path.split(/[\\/]/).pop() ?? "";
  return `/v1/jobs/${a.job_id}/file/${encodeURIComponent(name)}`;
}
