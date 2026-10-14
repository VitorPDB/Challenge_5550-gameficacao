import { apiFetch } from "./client";
import type { MonitorStatus } from "./types";
import type { PpeKey } from "./ppe";

export interface PreventionZone { id: string; name: string; polygon: number[][]; required_ppe: PpeKey[]; restricted: boolean }
export interface PreventionConfig {
  enabled: boolean; trajectory: boolean; proximity: boolean; ergonomics: boolean; coverage: boolean;
  horizon_s: number; max_gap_s: number; min_history_s: number; proximity_ratio: number;
  exposure_threshold_s: number; exposure_window_s: number; zones: PreventionZone[];
}
export interface PreventionState {
  enabled: boolean; observation_reused?: boolean; warnings?: string[];
  people: { person_id: string; prediction_status: string; exposure_s: number;
    zones: { name: string; inside: boolean; approaching: boolean; eta_s: number | null; unconfirmed_ppe: string[] }[] }[];
  encounters: { people: string[]; eta_s: number }[];
  coverage: { status: string; reasons?: string[] };
}
export interface PreventionResponse { config: PreventionConfig; state: PreventionState | null; status: MonitorStatus | null }
export interface Mission { id: number; title: string; team: string; category: string; status: string; evidence: string | null; points: number }
export interface MissionResponse {
  missions: Mission[]; teams: { team: string; points: number; verified: number; achievements: string[];
    open: number; awaiting_review: number; level: number; level_name: string; next_level_points: number | null;
    points_to_next_level: number; level_progress: number; weekly_verified: number;
    challenges: { id: string; name: string; description: string; progress: number; target: number }[];
    achievement_details: { id: string; name: string; description: string; progress: number; target: number; unlocked: boolean }[];
    categories: { key: string; label: string; verified: number }[];
  }[];
  categories: { key: string; label: string }[];
  suggestions: { category: string; title: string; occurrences: number }[]; basis: string;
}
export interface ScenarioResult {
  mode: string; analyzed_frames: number; video_time_s: number; runtime_s: number; truncated: boolean;
  comparisons: { entries: unknown[]; warnings: unknown[]; anticipated_entries: number;
    lead_times_s: number[]; unconfirmed_warning_episodes: number; censored_warnings: number; basis: string }[];
  samples: { at_s: number; image: string; states: PreventionState[] }[];
}
export interface ScenarioRun {
  id: string; status: string; error: string | null; result: ScenarioResult | null;
  progress?: { analyzed_frames?: number; video_time_s?: number };
}
export const fetchPrevention = (id: number) => apiFetch<PreventionResponse>(`/api/cameras/${id}/prevention`);
export const savePrevention = (id: number, config: PreventionConfig) => apiFetch(`/api/cameras/${id}/prevention`, {
  method: "PUT", body: JSON.stringify(config),
});
export const fetchMissions = (id: number) => apiFetch<MissionResponse>(`/api/cameras/${id}/missions`);
export const createMission = (id: number, team: string, category: string, title: string) => apiFetch(`/api/cameras/${id}/missions`, {
  method: "POST", body: JSON.stringify({ team, category, title }),
});
export const submitMission = (id: number, evidence: string) => apiFetch(`/api/missions/${id}/submit`, {
  method: "POST", body: JSON.stringify({ evidence }),
});
export const reviewMission = (id: number, approve: boolean) => apiFetch(`/api/missions/${id}/review`, {
  method: "POST", body: JSON.stringify({ approve }),
});
export const fetchScenario = (id: string) => apiFetch<ScenarioRun>(`/api/scenarios/${id}`);
export const fetchScenarioList = (id: number) => apiFetch<{ runs: ScenarioRun[] }>(`/api/cameras/${id}/scenarios`);
export async function uploadScenario(id: number, file: File, configs: PreventionConfig[]) {
  const form = new FormData();
  form.append("video", file);
  form.append("configs", JSON.stringify(configs));
  const response = await fetch(`/api/cameras/${id}/scenarios`, { method: "POST", body: form });
  const body = await response.json();
  if (!response.ok) throw new Error(body.error || "Falha no envio do vídeo.");
  return body as ScenarioRun;
}
