import type { PpeKey } from "./ppe";

/** Papéis reais, vindos da sessão. Hierárquicos: supervisor > technical > operator. */
export type UserRole = "operator" | "technical" | "supervisor";

export interface AuthUser {
  id: number;
  email: string;
  name: string;
  role: UserRole;
  active: boolean;
  /** Câmera do setor — só faz sentido para operator. */
  camera_id: number | null;
  last_login_at: string | null;
  created_at: string | null;
}

/**
 * DTOs mirroring the Flask backend contract exactly as read from source
 * (not from the old app.js). Each type links back to the Python source of
 * truth so a payload shape change is traceable to one file.
 */

// ---- app/services/feature_manager.py: FeatureFlag.to_dict() -------------
export interface FeatureFlag {
  key: string;
  label: string;
  description: string;
  enabled: boolean;
  group: string;
}

// ---- app/models.py: Alert.to_dict() --------------------------------------
/**
 * Toda carga vinda de um CameraWorker carrega a câmera de origem. `null` =
 * evento global/legado (ex.: um alerta gravado antes do multi-câmera). O store
 * usa isso pra descartar o que não é da câmera em foco — ver
 * `belongsToFocusedCamera` em store/dashboardStore.ts.
 */
export interface CameraScoped {
  camera_id?: number | null;
}

export interface Alert extends CameraScoped {
  id: number;
  key: string;
  rule: string;
  severity: "critical" | "high" | "medium" | "low" | "info" | string;
  message: string;
  feature: string | null;
  status: "active" | "resolved" | string;
  frame_ref: string | null;
  metadata: Record<string, unknown>;
  false_positive: boolean;
  occurrences: number;
  created_at: string | null;
  first_seen_at: string | null;
  last_seen_at: string | null;
  resolved_at: string | null;
}

// ---- app/models.py: EventLog.to_dict() -----------------------------------
export interface TimelineEvent extends CameraScoped {
  id: number;
  event_type: string;
  severity: string;
  message: string;
  subject: string | null;
  metadata: Record<string, unknown>;
  created_at: string | null;
}

// ---- app/vision/schemas.py: BoundingBox.to_dict() ------------------------
export interface BoundingBox {
  x1: number;
  y1: number;
  x2: number;
  y2: number;
}

// ---- app/vision/schemas.py: Detection.to_dict() --------------------------
export interface Detection {
  label: string;
  confidence: number;
  box: BoundingBox;
  class_id: number | null;
  category: string;
}

// ---- app/vision/schemas.py: PoseLandmark.to_dict() -----------------------
export interface PoseLandmark {
  name: string;
  x: number;
  y: number;
  z: number;
  visibility: number;
}

// ---- app/vision/schemas.py: PoseResult.to_dict() -------------------------
export interface PoseResult {
  found: boolean;
  landmarks: PoseLandmark[];
}

// ---- app/vision/schemas.py: FrameAnalysis.to_dict(), extended in --------
// ---- monitor_service.py._loop() with alerts/compliance/model ------------
export interface AnalysisPayload extends CameraScoped {
  detections: Detection[];
  pose: PoseResult;
  risk_events: Record<string, unknown>[];
  alerts: Alert[];
  alert_changes: Alert[];
  alert_resolved: Alert[];
  compliance: ComplianceState;
  model: ModelDiagnostics;
}

// ---- app/vision/yolo_ppe_detector.py: _build_diagnostics() ---------------
export interface ModelClassInfo {
  id: number | string;
  name: string;
  normalized: string;
}

// Chaveado por PpeKey (api/ppe.ts) — as 6 classes que o modelo pode reportar.
// Record<> em vez de campos soltos: assim adicionar uma classe nova em PPE_KEYS
// quebra o typecheck em todo lugar que precisa saber dela, em vez de silenciar.
export type SupportedPpe = Record<PpeKey, boolean>;

export interface ModelDiagnostics extends CameraScoped {
  model_path: string;
  confidence: number;
  device: string | null;
  classes: ModelClassInfo[];
  supported_ppe: SupportedPpe;
  person_supported: boolean;
  ppe_ready: boolean;
  multi_person_detection: boolean;
  max_detections: number;
  warning: string | null;
  error: string | null;
  ppe_feature_enabled?: boolean;
}

// ---- app/vision/person_compliance_matcher.py: build() --------------------
export interface PersonPpeCheck {
  key: string;
  status: "ok" | "missing" | "unsupported" | "disabled" | string;
  message: string;
  detections: Detection[];
  confidence: number;
}

export interface PersonRiskArea {
  status: "inside" | "outside" | string;
  message: string;
}

export interface PersonCompliance {
  id: string;
  label: string;
  confidence: number;
  box: BoundingBox;
  ppe: Record<PpeKey, PersonPpeCheck>;
  risk_area?: PersonRiskArea;
}

// ---- app/services/compliance_service.py: build_state() -------------------
export interface PpeComplianceCard {
  key: string;
  label: string;
  status: "ok" | "missing" | "critical" | "warning" | "unsupported" | "waiting" | "disabled" | string;
  message: string;
  enabled: boolean;
  supported: boolean;
  detections: Detection[];
  active_alert: Alert | null;
}

export interface PoseComplianceState {
  enabled: boolean;
  status: "disabled" | "waiting" | "critical" | "warning" | "ok" | string;
  message: string;
  active_alert?: Alert;
}

export interface RiskAreaComplianceState {
  enabled: boolean;
  status: "disabled" | "warning" | "ok" | string;
  message: string;
  active_alert?: Alert;
}

export interface ComplianceState extends CameraScoped {
  person_present: boolean;
  person_count: number;
  people: PersonCompliance[];
  ppe: Record<PpeKey, PpeComplianceCard>;
  pose: PoseComplianceState;
  risk_area: RiskAreaComplianceState;
  model: ModelDiagnostics;
  active_alerts: Alert[];
}

// ---- app/services/monitor_service.py: settings() -------------------------
export interface RuntimeSettings extends CameraScoped {
  video_source: string;
  target_fps: number;
  jpeg_quality: number;
  yolo_confidence: number;
  yolo_max_detections: number;
  multi_person_detection: boolean;
  alert_create_after_frames: number;
  alert_resolve_after_frames: number;
  cleanup_on_monitor_start: boolean;
  snapshot_enabled: boolean;
  snapshot_jpeg_quality: number;
  risk_area_name: string;
}

// ---- app/services/monitor_service.py: overlay_options --------------------
export interface OverlayOptions extends CameraScoped {
  boxes: boolean;
  labels: boolean;
  confidence: boolean;
  pose: boolean;
  risk_area: boolean;
  part_head: boolean;
  part_face: boolean;
  part_ear: boolean;
  part_hands: boolean;
  part_foot: boolean;
  part_tool: boolean;
}

// ---- app/services/monitor_service.py: risk_area_state() ------------------
export interface RiskAreaPoint {
  x: number;
  y: number;
}

export interface RiskAreaState extends CameraScoped {
  name: string;
  polygon: RiskAreaPoint[];
  enabled: boolean;
}

// ---- app/services/snapshot_service.py: info() (shape read via /status) --
export interface SnapshotInfo {
  enabled: boolean;
  snapshot_dir: string;
  jpeg_quality: number;
  [key: string]: unknown;
}

// ---- app/services/monitor_service.py: status() ---------------------------
/**
 * Estado real da captura, reportado pelo VideoStream do backend.
 *
 * Antes o dashboard só conseguia INFERIR isso pela idade do último frame
 * ("congelado" depois de 4,5s). Agora dá pra distinguir uma fonte que acabou
 * de cair de uma que está morta há minutos, e mostrar quando será a próxima
 * tentativa. Espelha StreamStatus em app/vision/video_stream.py.
 */
export interface VideoStreamStatus {
  state: "idle" | "live" | "reconnecting" | "unavailable";
  consecutive_failures: number;
  reconnect_attempts: number;
  total_reconnects: number;
  last_error: string | null;
  seconds_until_retry: number;
  /**
   * De QUAL fonte a câmera está lendo — ortogonal a `state`, que diz se está
   * entregando frame. Em modo fixture a câmera está `live` e `fixture` ao
   * mesmo tempo: é fallback deliberado, não falha.
   *
   * Opcional porque um backend anterior a esta versão não manda o campo.
   */
  modo?: "ao_vivo" | "reconectando" | "fixture";
  /** Fonte em uso, com a credencial já redigida pelo backend. */
  fonte?: string;
}

// ---- app/services/camera_worker.py: diagnostico() ------------------------
// As três perguntas de campo, respondidas sem abrir terminal. `fps` vem do
// LOOP DE CAPTURA, e não do intervalo entre eventos `analysis` como o
// `dashboardStore` calcula: aquele tem teto em TELEMETRY_HZ (8) e não
// distingue "o pipeline caiu para 6" de "o pipeline está a 19 e a telemetria
// está limitada". `resolucao` é a do frame que CHEGOU — em RTSP o backend
// FFMPEG ignora a resolução pedida no cadastro.
export interface CameraDiagnostico {
  idade_frame_s?: number | null;
  imagem_desatualizada?: boolean;
  fps: number;
  resolucao: string | null;
  largura: number;
  altura: number;
  /** Contagem por classe, só de frames com inferência NOVA (não reaproveitada). */
  deteccoes_30s: Record<string, number>;
  janela_deteccoes_s: number;
  /** Brilho médio aproximado do último frame amostrado. `null` antes da 1ª amostra. */
  brilho?: number | null;
  /**
   * Frame chega, imagem não. É o caso do Windows entregando stream PRETO para o
   * segundo app que abre uma webcam já tomada por outro: FPS saudável,
   * resolução certa, e cada pixel em ~0. É só diagnóstico — não para a captura
   * nem gera alerta, porque cena legitimamente escura não é falha.
   */
  fonte_sem_imagem?: boolean;
  /** O limiar em uso, para quem lê o aviso saber contra que número foi comparado. */
  brilho_minimo?: number;
}

export interface MonitorStatus extends CameraScoped {
  compliance?: ComplianceState | null;
  running: boolean;
  frame_counter: number;
  last_error: string | null;
  video?: VideoStreamStatus;
  /** Opcional: um backend anterior a esta fase não manda o campo. */
  diagnostico?: CameraDiagnostico;
  features: Record<string, FeatureFlag>;
  model: ModelDiagnostics;
  active_alerts: Alert[];
  cleanup: Record<string, unknown> | null;
  overlay: OverlayOptions;
  settings: RuntimeSettings;
  risk_area: RiskAreaState;
  snapshot: SnapshotInfo;
}

// ---- GET /status: {"system": "VisionEPI", ...status()} -------------------
export interface StatusResponse extends MonitorStatus {
  system: string;
}

// ---- app/services/monitor_service.py: preflight() ------------------------
export interface PreflightCheck {
  key: string;
  label: string;
  status: "ok" | "warning" | "error" | string;
  message: string;
}

export interface PreflightSummary {
  ok: number;
  warning: number;
  error: number;
}

export interface PreflightResponse {
  checks: PreflightCheck[];
  summary: PreflightSummary;
  can_start: boolean;
}

// ---- GET /features -> {features: FeatureFlag[]} --------------------------
export interface FeaturesResponse {
  features: FeatureFlag[];
  camera_id?: number | null;
}

// ---- GET /alerts -> {items: Alert[], count} ------------------------------
export interface AlertsResponse {
  items: Alert[];
  count: number;
}

// ---- GET /events -> {items: TimelineEvent[], count} ----------------------
export interface EventsResponse {
  items: TimelineEvent[];
  count: number;
}

// ---- GET /settings -> {settings, overlay} --------------------------------
export interface SettingsResponse {
  settings: RuntimeSettings;
  overlay: OverlayOptions;
}

// ---- app/services/risk_score_service.py: compute_risk_score() ------------
// Estatística sobre janela deslizante (contagem de alertas ponderada por
// severidade) — não é predição por IA/ML.
export type RiskLevel = "baixo" | "moderado" | "alto" | "critico";

export interface RiskFeatureScore {
  score: number;
  level: RiskLevel | string;
  alert_count: number;
}

export interface RiskScoreOverall extends RiskFeatureScore {
  driving_feature: string | null;
}

export interface RiskScore extends CameraScoped {
  window_minutes: number;
  overall: RiskScoreOverall;
  features: Record<string, RiskFeatureScore>;
  computed_at: string;
}

// ---- app/services/risk_score_service.py: compute_risk_trend() ------------
// Cada bucket é pontuado isoladamente (não é janela deslizante) — mostra a
// frequência daquela hora específica, não o /risk-score.overall.
export interface RiskTrendBucket {
  bucket_start: string;
  bucket_end: string;
  overall: { score: number; level: RiskLevel | string };
  features: Record<string, RiskFeatureScore>;
}

export interface RiskTrendResponse {
  hours: number;
  bucket_hours: number;
  buckets: RiskTrendBucket[];
}

// ---- MOCK: multi-câmera (fase de interface, antes do backend multi-source) --
// Sem endpoint real ainda. Alimenta grid/kiosk/foco por câmera enquanto o
// Espelha DEFAULT_CAMERA_FEATURES em app/models.py — mesmas chaves que o
// FeatureManager reconhece, validadas em VALID_FEATURE_KEYS (app/api/cameras.py).
export interface CameraFeatureSet {
  helmet: boolean;
  vest: boolean;
  gloves: boolean;
  glasses: boolean;
  mask: boolean;
  safety_shoe: boolean;
  ear_protection: boolean;
  pose: boolean;
  falls: boolean;
  posture: boolean;
  risk_area: boolean;
  // Toggle de grupo do backend (liga/desliga todos os EPIs juntos) — existe
  // na API real, mas a UI só o expõe na sidebar de features, não como chip
  // por câmera; opcional aqui por isso.
  ppe?: boolean;
}

// ---- REAL: câmeras multi-source (Fase A, Passo 6 — ver app/api/cameras.py) --
// Câmeras de verdade cadastradas pelo usuário, sem seed automático. Mesmo
// shape de Camera.to_dict() no backend.
export interface CameraRecord {
  id: number;
  name: string;
  location: string | null;
  source_type: "USB" | "RTSP" | "Arquivo";
  source: string;
  fps: number;
  width: number;
  height: number;
  rotation: 0 | 90 | 180 | 270;
  gate_required: string[] | null;
  enabled: boolean;
  features: CameraFeatureSet;
  created_at: string | null;
  updated_at: string | null;
}

export interface CamerasResponse {
  items: CameraRecord[];
  count: number;
}

// ---- GET /api/cameras/discover: testa índices USB reais na hora ----------
export interface CameraDiscoveryEntry {
  index: number;
  source: string;
  available: boolean;
  width: number | null;
  height: number | null;
  already_registered: boolean;
  registered_as: string | null;
}

export interface CameraDiscoveryResponse {
  items: CameraDiscoveryEntry[];
  count: number;
}


// ---- app/api/cameras.py: modo portaria ------------------------------------
export interface GateState {
  camera_id: number;
  enabled: boolean;
  required: string[];
  verdict: "off" | "waiting" | "approved" | "denied";
  missing: string[];
  people: number;
}
