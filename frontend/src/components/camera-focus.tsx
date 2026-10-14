import { useEffect, useRef, useState } from "react";
import { AnimatePresence, motion, useReducedMotion } from "motion/react";
import { FeatureGroups, OverlayControls, PartToggles } from "./features";
import { Tabs, Panel, EmptyState, type TabItem } from "./common";
import { RiskAreaEditorPanel, RiskEditorCanvas } from "./video";
import { GateConfigPanel } from "./gate";
import { LlmPanel } from "./llm-panel";
import { MissionsPanel, PreventionPanel, ScenarioPanel } from "./prevention";
import { ChecklistPanel, ModelStatusPanel, SettingsPanel } from "./diagnostics";
import { RiskScoreCard } from "./risk-score";
import { AlertPanel, AlertHistoryPanel, SafetyEventsPanel } from "./alerts";
import { ComplianceCard, PersonCard } from "./compliance";
import { TimelineCard } from "./timeline";
import { ExportPanel } from "./export";
import { EASE, ScreenHead } from "./layout";
import { temAlertaCritico, textoDeEstado, useCameraEstado } from "./camera-grid";
import { deleteCamera, discoverCameras, getCameraStatus, startCamera, stopCamera, updateCamera } from "../api/endpoints";
import type { CameraRecord, MonitorStatus } from "../api/types";
import { useDashboardStore, type ViewMode } from "../store/dashboardStore";
import { paraDate } from "../utils/datas";

/** Tempo decorrido desde o início do alerta, em "m:ss" ou "h:mm:ss". */
function decorrido(desde: string | null, agora: number): string {
  if (!desde) return "";
  const s = Math.max(0, Math.floor((agora - paraDate(desde).getTime()) / 1000));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const ss = String(s % 60).padStart(2, "0");
  return h ? `${h}:${String(m).padStart(2, "0")}:${ss}` : `${m}:${ss}`;
}

/**
 * Vídeo principal da tela de foco, por câmera de verdade. Os dados da antiga
 * faixa de status (estado do stream, fps, resolução, socket) flutuam sobre o
 * topo do vídeo em pílulas translúcidas, que é onde importam. Alerta crítico
 * ganha a própria pílula, à direita, com o tempo decorrido.
 */
function MainCameraVideo({ camera }: { camera: CameraRecord }) {
  const estado = useCameraEstado(camera.id);
  const connected = useDashboardStore((s) => s.connected);
  const shouldReduceMotion = useReducedMotion();
  const [starting, setStarting] = useState(false);
  const wrapRef = useRef<HTMLDivElement>(null);
  const riskEditorActive = useDashboardStore((st) => st.riskEditorActive);
  const [agora, setAgora] = useState(() => Date.now());
  const critico = temAlertaCritico(estado);

  // O relógio do alerta anda sozinho; sem isso o "decorrido" congelava entre
  // dois pollings.
  useEffect(() => {
    if (!critico) return undefined;
    const id = setInterval(() => setAgora(Date.now()), 1000);
    return () => clearInterval(id);
  }, [critico]);

  const handleStart = () => {
    setStarting(true);
    startCamera(camera.id)
      .catch((err) => console.error(err))
      .finally(() => setStarting(false));
  };

  const dotDoStream = !estado.running || estado.modo === "reconectando" || estado.modo === "fixture" ? "off" : "ok";

  return (
    <section className="card video-card">
      <div className={`video-frame ${riskEditorActive ? "editing-risk" : ""}`.trim()} ref={wrapRef}>
        {estado.running ? (
          <>
            <div className="over-video">
              <span className="over-pill">
                <span className={`dot ${dotDoStream}`} />
                {textoDeEstado(estado)}
                {estado.diagnostico && (
                  <span className="t-data">
                    {estado.diagnostico.fps.toFixed(1)} fps
                    {estado.diagnostico.resolucao ? ` · ${estado.diagnostico.resolucao}` : ""}
                  </span>
                )}
              </span>
              <span className="over-pill">
                <span className={`dot ${connected ? "ok" : "off"}`} />
                {connected ? "Conectado" : "Sem conexão"}
              </span>
              <AnimatePresence initial={false}>
                {critico && (
                  <motion.span
                    key={critico.id}
                    className="over-pill alert"
                    role="status"
                    initial={shouldReduceMotion ? false : { opacity: 0, scale: 0.98 }}
                    animate={{ opacity: 1, scale: 1 }}
                    exit={{ opacity: 0 }}
                    transition={{ duration: shouldReduceMotion ? 0 : 0.2, ease: EASE }}
                  >
                    <span className="dot" />
                    {critico.message}
                    <span className="t-data">{decorrido(critico.first_seen_at ?? critico.created_at, agora)}</span>
                  </motion.span>
                )}
              </AnimatePresence>
            </div>
            <img src={`/api/cameras/${camera.id}/video_feed`} alt={`Vídeo de ${camera.name}`} />
            <RiskEditorCanvas wrapRef={wrapRef} />
          </>
        ) : (
          <div className="video-empty">
            <h3>Aguardando conexão de vídeo</h3>
            <p>Inicie o monitoramento de {camera.name} para começar a receber o feed.</p>
            <button type="button" className="primary" disabled={starting} onClick={handleStart}>
              {starting ? "Iniciando…" : "Iniciar"}
            </button>
          </div>
        )}
      </div>
      {estado.running && <PartToggles />}
    </section>
  );
}

function CameraLiveControl({ camera }: { camera: CameraRecord }) {
  const [status, setStatus] = useState<MonitorStatus | null>(null);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const refresh = () => {
    getCameraStatus(camera.id)
      .then(setStatus)
      .catch((err) => setError(err instanceof Error ? err.message : "Falha ao consultar status"));
  };

  // Consulta ao trocar de câmera e a cada 3 s enquanto essa aba estiver
  // aberta; polling simples resolve pra esta fase.
  useEffect(() => {
    setStatus(null);
    setError(null);
    refresh();
    const interval = setInterval(refresh, 3000);
    return () => clearInterval(interval);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [camera.id]);

  const running = Boolean(status?.running);

  const handleStart = () => {
    setPending(true);
    setError(null);
    startCamera(camera.id)
      .then(setStatus)
      .catch((err) => setError(err instanceof Error ? err.message : "Falha ao iniciar câmera"))
      .finally(() => setPending(false));
  };

  const handleStop = () => {
    setPending(true);
    setError(null);
    stopCamera(camera.id)
      .then(setStatus)
      .catch((err) => setError(err instanceof Error ? err.message : "Falha ao parar câmera"))
      .finally(() => setPending(false));
  };

  return (
    <Panel
      title="Vídeo desta câmera"
      description="Inicia e para só esta câmera, independente da câmera padrão do topo da tela."
      action={
        <span className={`chip ${running ? "ok" : ""}`.trim()}>
          {running && <span className="dot" />}
          {running ? "Rodando" : "Parada"}
        </span>
      }
    >
      {error && <p className="status-text error">{error}</p>}
      <div className="video-frame">
        {running ? (
          <img src={`/api/cameras/${camera.id}/video_feed`} alt={`Feed da câmera ${camera.id}`} />
        ) : (
          <div className="video-empty">
            <p>Câmera parada. Clique em Iniciar para ver o feed.</p>
          </div>
        )}
      </div>
      <div className="actions">
        <button type="button" className="primary" disabled={pending || running} onClick={handleStart}>
          {pending && !running ? "Iniciando…" : "Iniciar esta câmera"}
        </button>
        <button type="button" className="stop" disabled={pending || !running} onClick={handleStop}>
          {pending && running ? "Parando…" : "Parar esta câmera"}
        </button>
      </div>
    </Panel>
  );
}

function CameraConfigPanel({ camera }: { camera: CameraRecord }) {
  const setScreen = useDashboardStore((s) => s.setScreen);
  const loadCameras = useDashboardStore((s) => s.loadCameras);
  const [name, setName] = useState(camera.name);
  const [location, setLocation] = useState(camera.location ?? "");
  const [sourceType, setSourceType] = useState(camera.source_type);
  const [source, setSource] = useState(camera.source);
  const [fps, setFps] = useState(camera.fps);
  const [width, setWidth] = useState(camera.width);
  const [height, setHeight] = useState(camera.height);
  const [rotation, setRotation] = useState(camera.rotation);
  const [saving, setSaving] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [testing, setTesting] = useState(false);
  const [message, setMessage] = useState<string | null>(null);

  // Se o usuário trocar de câmera, os campos precisam refletir a câmera
  // nova, não continuar mostrando o rascunho da anterior.
  useEffect(() => {
    setName(camera.name);
    setLocation(camera.location ?? "");
    setSourceType(camera.source_type);
    setSource(camera.source);
    setFps(camera.fps);
    setWidth(camera.width);
    setHeight(camera.height);
    setRotation(camera.rotation);
    setMessage(null);
  }, [camera.id]);

  const handleSave = () => {
    setSaving(true);
    setMessage(null);
    updateCamera(camera.id, { name, location: location || null, source_type: sourceType, source, fps, width, height, rotation })
      .then(() => {
        setMessage("Câmera salva. Se estava rodando, já reiniciou sozinha com a configuração nova.");
        return loadCameras();
      })
      .catch((err) => setMessage(err instanceof Error ? err.message : "Falha ao salvar câmera"))
      .finally(() => setSaving(false));
  };

  const handleDelete = () => {
    if (!confirm("Remover esta câmera do sistema? Isso para o monitoramento dela e apaga o cadastro.")) return;
    setDeleting(true);
    deleteCamera(camera.id)
      .then(() => {
        setScreen("grid");
        return loadCameras();
      })
      .catch((err) => setMessage(err instanceof Error ? err.message : "Falha ao remover câmera"))
      .finally(() => setDeleting(false));
  };

  const handleTest = () => {
    if (sourceType !== "USB") {
      setMessage("Teste automático só cobre fontes USB por enquanto. RTSP e Arquivo exigem verificação manual.");
      return;
    }
    setTesting(true);
    setMessage(null);
    discoverCameras(5)
      .then((res) => {
        const match = res.items.find((d) => d.source === source);
        setMessage(match?.available ? `Índice ${source}: respondendo agora.` : `Índice ${source}: não respondeu ao teste.`);
      })
      .catch((err) => setMessage(err instanceof Error ? err.message : "Falha ao testar conexão"))
      .finally(() => setTesting(false));
  };

  return (
    <>
      <CameraLiveControl camera={camera} />
      <Panel title="Configuração da câmera" description="Fonte de vídeo e identificação, específico desta câmera.">
        <div className="settings-grid">
          <label>
            Nome
            <input type="text" value={name} onChange={(e) => setName(e.target.value)} />
          </label>
          <label>
            Local
            <input type="text" value={location} onChange={(e) => setLocation(e.target.value)} />
          </label>
          <label>
            Tipo de fonte
            <select value={sourceType} onChange={(e) => setSourceType(e.target.value as CameraRecord["source_type"])}>
              <option value="USB">USB</option>
              <option value="RTSP">RTSP</option>
              <option value="Arquivo">Arquivo</option>
            </select>
          </label>
          <label>
            Endereço ou índice
            <input type="text" value={source} onChange={(e) => setSource(e.target.value)} />
          </label>
          <label>
            FPS alvo
            <input type="number" value={fps} min={1} max={60} onChange={(e) => setFps(Number(e.target.value) || 12)} />
          </label>
          <label>
            Largura (px)
            <input type="number" value={width} min={160} max={3840} onChange={(e) => setWidth(Number(e.target.value) || 960)} />
          </label>
          <label>
            Altura (px)
            <input type="number" value={height} min={120} max={2160} onChange={(e) => setHeight(Number(e.target.value) || 540)} />
          </label>
          <label>
            Orientação da câmera
            <select value={rotation} onChange={(e) => setRotation(Number(e.target.value) as 0 | 90 | 180 | 270)}>
              <option value={0}>Normal (0°)</option>
              <option value={90}>90° horário</option>
              <option value={180}>180° (de cabeça para baixo)</option>
              <option value={270}>270° horário (90° anti-horário)</option>
            </select>
          </label>
        </div>
        <p className="status-text">
          Corrige câmera montada de lado ou invertida — a imagem gira antes de qualquer detecção
          (pessoa, EPI e pose), não é só um efeito visual.
        </p>
        {message && <p className="status-text">{message}</p>}
        <div className="actions actions-top">
          <button type="button" className="small" disabled={testing} onClick={handleTest}>
            {testing ? "Testando…" : "Testar conexão"}
          </button>
        </div>
        <div className="actions">
          <button type="button" className="primary" disabled={saving} onClick={handleSave}>
            {saving ? "Salvando…" : "Salvar câmera"}
          </button>
          <button type="button" className="stop small" disabled={deleting} onClick={handleDelete}>
            {deleting ? "Removendo…" : "Remover câmera"}
          </button>
        </div>
      </Panel>
    </>
  );
}

function LogsPanel() {
  return (
    <Panel title="Log de erros" description="Exceções e eventos técnicos desta câmera.">
      <EmptyState>
        Ainda não existe um log persistente por câmera no backend. O erro mais recente aparece no Checklist (linha "Vídeo") e na linha de estado do cartão, na grade.
      </EmptyState>
    </Panel>
  );
}

const TABS_BY_MODE: Record<Exclude<ViewMode, "operator">, TabItem[]> = {
  technical: [
    { key: "checklist", label: "Checklist", content: <ChecklistPanel /> },
    { key: "alerts", label: "Alertas", content: <AlertPanel /> },
    { key: "safety", label: "Quedas e postura", content: <SafetyEventsPanel /> },
    { key: "compliance", label: "Conformidade", content: <ComplianceCard /> },
    { key: "people", label: "Pessoas", content: <PersonCard /> },
    { key: "timeline", label: "Timeline", content: <TimelineCard /> },
    { key: "overlay", label: "Overlay", content: <OverlayControls /> },
    { key: "zone", label: "Zona", content: <RiskAreaEditorPanel /> },
    { key: "gate", label: "Portaria", content: <></> },
    { key: "llm", label: "Segunda opinião", content: <></> },
    { key: "camconfig", label: "Config. câmera", content: <></> },
    { key: "settings", label: "Parâmetros", content: <SettingsPanel /> },
    { key: "model", label: "Modelo", content: <ModelStatusPanel /> },
    { key: "logs", label: "Logs", content: <></> },
    { key: "history", label: "Histórico", content: <AlertHistoryPanel /> },
  ],
  supervisor: [
    { key: "trend", label: "Gráficos", content: <RiskScoreCard /> },
    { key: "checklist", label: "Checklist", content: <ChecklistPanel /> },
    { key: "alerts", label: "Alertas", content: <AlertPanel /> },
    { key: "safety", label: "Quedas e postura", content: <SafetyEventsPanel /> },
    { key: "compliance", label: "Conformidade", content: <ComplianceCard /> },
    { key: "people", label: "Pessoas", content: <PersonCard /> },
    { key: "timeline", label: "Timeline", content: <TimelineCard /> },
    { key: "overlay", label: "Overlay", content: <OverlayControls /> },
    { key: "zone", label: "Zona", content: <RiskAreaEditorPanel /> },
    { key: "gate", label: "Portaria", content: <></> },
    { key: "llm", label: "Segunda opinião", content: <></> },
    { key: "camconfig", label: "Config. câmera", content: <></> },
    { key: "settings", label: "Parâmetros", content: <SettingsPanel /> },
    { key: "model", label: "Modelo", content: <ModelStatusPanel /> },
    { key: "logs", label: "Logs", content: <></> },
    { key: "history", label: "Histórico", content: <AlertHistoryPanel /> },
    { key: "export", label: "Exportação", content: <ExportPanel /> },
  ],
};

/** Um setor na sidebar: nome como rótulo principal, ponto de 7 px que só
 * acende com estado real (perigo em alerta, apagado offline). */
function SectorItem({ camera, current, onSelect }: { camera: CameraRecord; current: boolean; onSelect: (id: number) => void }) {
  const estado = useCameraEstado(camera.id);
  const cls = ["side-item", current ? "current" : "", temAlertaCritico(estado) ? "alert" : !estado.running ? "offline" : ""]
    .filter(Boolean)
    .join(" ");
  return (
    <button type="button" className={cls} aria-current={current ? "page" : undefined} onClick={() => onSelect(camera.id)}>
      <span className="dot" />
      <span className="side-item-body">
        {camera.name}
        {camera.location && <small>{camera.location}</small>}
      </span>
    </button>
  );
}

/**
 * Foco individual de uma câmera, Técnico/Supervisor. Sidebar com os setores
 * e os recursos; à direita, o vídeo e as abas. Operador nunca chega aqui.
 */
export function CameraFocus() {
  const camId = useDashboardStore((s) => s.camId);
  const cameras = useDashboardStore((s) => s.cameras);
  const setCamId = useDashboardStore((s) => s.setCamId);
  const setScreen = useDashboardStore((s) => s.setScreen);
  const mode = useDashboardStore((s) => s.mode);
  const camera = cameras.find((c) => c.id === camId);
  const [activeTab, setActiveTab] = useState<string>(mode === "supervisor" ? "trend" : "checklist");

  if (!camera || mode === "operator") return null;

  const tabs = TABS_BY_MODE[mode].map((tab) => {
    if (tab.key === "llm") return { ...tab, content: <LlmPanel camId={camera.id} /> };
    if (tab.key === "gate") return { ...tab, content: <GateConfigPanel camera={camera} /> };
    if (tab.key === "camconfig") return { ...tab, content: <CameraConfigPanel camera={camera} /> };
    if (tab.key === "logs") return { ...tab, content: <LogsPanel /> };
    return tab;
  });
  tabs.push(
    { key: "prevention", label: "Prevenção", content: <PreventionPanel key={camera.id} cameraId={camera.id} /> },
    { key: "missions", label: "Missões", content: <MissionsPanel key={camera.id} cameraId={camera.id} /> },
    { key: "scenarios", label: "Laboratório", content: <ScenarioPanel key={camera.id} cameraId={camera.id} /> },
  );

  const sub = [camera.location, `${camera.source_type} ${camera.source}`].filter(Boolean).join(", ");

  return (
    <div className="screen with-sidebar">
      <nav className="sidebar" aria-label="Setores e recursos">
        <div className="sidebar-group">
          <h2>Setores</h2>
          {cameras.map((c) => (
            <SectorItem key={c.id} camera={c} current={c.id === camId} onSelect={setCamId} />
          ))}
        </div>
        <FeatureGroups />
      </nav>
      <div>
        <ScreenHead
          title={camera.name}
          sub={sub}
          actions={
            <button type="button" className="ghost" onClick={() => setScreen("grid")}>
              Voltar para a grade
            </button>
          }
        />
        <div className="focus-main">
          <div>
            <MainCameraVideo camera={camera} />
          </div>
          <div className="side-column">
            <Tabs
              tabs={tabs}
              active={activeTab}
              onChange={setActiveTab}
              idPrefix={`focus-${camId}`}
              primary={["trend", "checklist", "alerts", "compliance", "people"]}
            />
          </div>
        </div>
      </div>
    </div>
  );
}
