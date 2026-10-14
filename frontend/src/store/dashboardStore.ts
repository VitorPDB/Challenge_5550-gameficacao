import { create } from "zustand";
import { devtools } from "zustand/middleware";
import { socket } from "../socket/client";
import { playAlertChime, playAllClearChime } from "../audio/chime";
import type { ServerEventName, ServerEvents } from "../socket/events";
import {
  getFeatures,
  getOverlay,
  getRiskArea,
  getRiskScore,
  getRiskTrend,
  getSettings,
  getStatus,
  listAlerts,
  listCameras,
  listEvents,
  acknowledgeAlert as apiAcknowledgeAlert,
  getMe,
  login as apiLogin,
  logout as apiLogout,
  markFalsePositive as apiMarkFalsePositive,
  patchFeatures as apiPatchFeatures,
  patchOverlay as apiPatchOverlay,
  patchRiskArea as apiPatchRiskArea,
  patchSettings as apiPatchSettings,
  startMonitor,
  stopMonitor,
  updateCamera as apiUpdateCamera,
} from "../api/endpoints";
import type {
  Alert,
  AuthUser,
  CameraFeatureSet,
  CameraRecord,
  ComplianceState,
  Detection,
  FeatureFlag,
  ModelDiagnostics,
  OverlayOptions,
  PoseResult,
  RiskAreaState,
  RiskScore,
  RiskTrendResponse,
  RuntimeSettings,
  TimelineEvent,
  VideoStreamStatus,
} from "../api/types";

export type ViewMode = "operator" | "technical" | "supervisor";

// Tela dentro do "espaço multi-câmera". Independente de `mode`: o mesmo
// `mode` pode passear entre grid/foco (Técnico/Supervisor), e Operador fica
// travado em "kiosk" (ver setMode e setScreenForMode abaixo).
export type CameraScreen = "grid" | "focus" | "kiosk" | "overview" | "home" | "accounts" | "journey";

// Acesso por papel. A FONTE é a sessão (`user.role`), não um seletor de tela —
// e o backend valida de novo em cada rota, então isto aqui é só para a UI não
// oferecer o que a pessoa não pode fazer. Nunca é a única barreira.
export interface RoleAccess {
  seeAllCameras: boolean;
  canConfigure: boolean;
  hasOverview: boolean;
}
export const ROLE_ACCESS: Record<ViewMode, RoleAccess> = {
  operator: { seeAllCameras: false, canConfigure: false, hasOverview: false },
  technical: { seeAllCameras: true, canConfigure: true, hasOverview: false },
  supervisor: { seeAllCameras: true, canConfigure: true, hasOverview: true },
};

interface DashboardState {
  // ---- sessão ------------------------------------------------------------
  // `mode` deixa de ser um seletor na topbar: é o PAPEL da pessoa logada.
  user: AuthUser | null;
  authChecked: boolean;
  login: (email: string, password: string) => Promise<void>;
  logout: () => Promise<void>;
  checkSession: () => Promise<void>;

  // connection
  connected: boolean;
  mode: ViewMode;

  // audio: sons são poucos e opt-out — persistido por perfil (Fase 4: cada
  // perfil lembra sua própria preferência; `muted` é o valor "ao vivo" do
  // perfil atual, derivado de mutedByProfile a cada troca de mode).
  muted: boolean;
  mutedByProfile: Record<ViewMode, boolean>;
  toggleMuted: () => void;

  // tema: persistido em localStorage (visionepi-theme). null = sistema decide.
  theme: Theme | null;
  toggleTheme: () => void;

  // Aba ativa por perfil — em memória, não persiste (ver App.tsx e o
  // comentário da Fase 1 sobre não resetar ao alternar perfil e voltar).
  // Vive no store, não em useState local, porque o cmdk (jumpTo) também
  // precisa poder setar a aba antes de rolar até o painel.
  activeTabByMode: Record<ViewMode, string>;
  setActiveTab: (mode: ViewMode, tabKey: string) => void;

  // command palette: estado simples de UI, mesmo padrão do riskEditorActive
  // abaixo — não é dado de servidor, só precisa ser lido por 2 componentes
  // que não têm relação de pai/filho (Topbar e CommandPalette).
  commandPaletteOpen: boolean;
  setCommandPaletteOpen: (open: boolean) => void;

  // true até bootstrap() resolver — cards mostram skeleton em vez de piscar
  // vazio->populado nos primeiros segundos antes do WS conectar.
  bootstrapping: boolean;

  // Timestamp da última transição real "havia alerta ativo -> zero alertas"
  // (não o boot inicial). AlertPanel observa isso pra disparar o pulso de
  // cor sem duplicar a lógica de transição que já existe pro chime de áudio.
  allClearAt: number | null;

  // monitor
  running: boolean;
  frameCounter: number;
  lastError: string | null;
  videoStream: VideoStreamStatus | null;

  // domain slices, each owned by exactly one socket event / REST call
  features: FeatureFlag[];
  overlay: OverlayOptions | null;
  settings: RuntimeSettings | null;
  riskArea: RiskAreaState | null;
  riskScore: RiskScore | null;
  riskTrend: RiskTrendResponse | null;
  model: ModelDiagnostics | null;
  compliance: ComplianceState | null;
  activeAlerts: Alert[];
  // Set only by the alert_created socket event (never alert_updated) — lets
  // AlertPanel highlight the one row that's genuinely new, not a re-confirm.
  lastAlertCreatedId: number | null;
  alertHistory: Alert[];
  timeline: TimelineEvent[];
  message: { text: string; tone: "ok" | "warning" | "error" } | null;
  lastAnalysisAt: number | null;
  wsFrameCounter: number;
  fps: number;
  lastDetections: Detection[];
  lastPose: PoseResult | null;

  // Local-only UI state for the risk-area canvas editor (not server state —
  // riskArea above holds the saved polygon; this is the in-progress draft).
  riskEditorActive: boolean;
  riskEditorPoints: { x: number; y: number }[];
  toggleRiskEditor: () => void;
  clearRiskEditorPoints: () => void;
  addRiskEditorPoint: (point: { x: number; y: number }) => void;
  moveRiskEditorPoint: (index: number, point: { x: number; y: number }) => void;
  resetRiskEditorFromServer: () => void;

  // actions
  setMode: (mode: ViewMode) => void;
  showMessage: (text: string, tone?: "ok" | "warning" | "error") => void;
  hideMessage: () => void;
  bootstrap: () => Promise<void>;
  start: () => Promise<void>;
  stop: () => Promise<void>;
  updateFeatures: (updates: Record<string, boolean>) => Promise<void>;
  updateOverlay: (updates: Partial<OverlayOptions>) => Promise<void>;
  updateSettings: (updates: Partial<RuntimeSettings>) => Promise<void>;
  updateRiskArea: (payload: { name?: string; polygon: { x: number; y: number }[] }) => Promise<void>;
  markFalsePositive: (alertId: number, reason?: string) => Promise<void>;
  acknowledgeAlert: (alertId: number, note?: string) => Promise<void>;

  // ---- multi-câmera REAL (Fase A, Passo 6 — ver app/api/cameras.py) -------
  // `screen` decide qual tela do "espaço multi-câmera" aparece; é ortogonal
  // a `mode`, mas setMode força screen="kiosk" pro Operador (trava de
  // navegação: ele nunca deveria conseguir cair no grid/foco de propósito).
  // Sem seed automático no backend — cameras começa vazio até loadCameras()
  // resolver, e continua vazio até o usuário cadastrar a primeira.
  cameras: CameraRecord[];
  camerasLoading: boolean;
  camId: number | null;
  screen: CameraScreen;
  // Câmera "do setor" do Operador — simulado por enquanto (seletor na UI).
  // Vira sessão/login mais adiante; é por isso que fica separado de `camId`
  // (que é só "qual câmera está sendo exibida agora"). null enquanto não
  // existe nenhuma câmera cadastrada.
  operatorCam: number | null;
  loadCameras: () => Promise<void>;
  setScreen: (screen: CameraScreen) => void;
  setCamId: (camId: number) => void;
  setOperatorCam: (camId: number) => void;
  toggleCameraFeature: (camId: number, key: keyof CameraFeatureSet) => void;
}

const MAX_TIMELINE = 90;
const MAX_HISTORY = 80;
// Matches the original app.js's state.fpsSamples: 20-sample moving average,
// not an instantaneous delta (too noisy for diagnostic use in Técnico mode).
// Module-level, not store state — it's a rolling accumulator, not something
// any component reads directly (only the derived `fps` average is exposed).
const FPS_SAMPLE_WINDOW = 20;
const fpsSamples: number[] = [];
const MUTED_STORAGE_KEY = "visionepi-muted"; // legado (pré-Fase 4): boolean único, só lido pra migração
const MUTED_BY_PROFILE_STORAGE_KEY = "visionepi-muted-by-profile";
const MODE_STORAGE_KEY = "visionepi-mode";
const THEME_STORAGE_KEY = "visionepi-theme";

// Tema: "light" | "dark" quando a pessoa escolheu; null = segue o sistema
// (prefers-color-scheme), que é o padrão. Mesmo padrão de leitura do muted:
// try/catch porque em janela privada o localStorage lança.
export type Theme = "light" | "dark";

export function readStoredTheme(): Theme | null {
  try {
    const stored = localStorage.getItem(THEME_STORAGE_KEY);
    return stored === "light" || stored === "dark" ? stored : null;
  } catch {
    return null;
  }
}

/** Marca o tema no <html>. Sem valor, tira o atributo e o CSS volta a
 * decidir pelo prefers-color-scheme. Troca de tema não recarrega a página. */
export function applyTheme(theme: Theme | null) {
  if (theme) document.documentElement.setAttribute("data-theme", theme);
  else document.documentElement.removeAttribute("data-theme");
}

function persistTheme(theme: Theme) {
  try {
    localStorage.setItem(THEME_STORAGE_KEY, theme);
  } catch {
    // localStorage indisponível (modo privado etc.), só não persiste
  }
}

/** Tema em vigor agora: o escolhido, ou o do sistema quando ninguém escolheu. */
export function effectiveTheme(theme: Theme | null): Theme {
  if (theme) return theme;
  return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}
const SEVERITIES_WITH_CHIME = new Set(["critical", "high"]);
// Supervisor nasce mudo (perfil de apresentação/gestão, som de alerta
// atrapalha reunião); Operador/Técnico mantêm o padrão de sempre (som ligado).
const DEFAULT_MUTED_BY_PROFILE: Record<ViewMode, boolean> = { operator: false, technical: false, supervisor: true };
const DEFAULT_ACTIVE_TAB_BY_MODE: Record<ViewMode, string> = { operator: "risk", technical: "checklist", supervisor: "trend" };

function readStoredMutedByProfile(): Record<ViewMode, boolean> {
  try {
    const stored = localStorage.getItem(MUTED_BY_PROFILE_STORAGE_KEY);
    if (stored) return { ...DEFAULT_MUTED_BY_PROFILE, ...JSON.parse(stored) };
    // Migração: quem já tinha a chave antiga (boolean único) não perde a
    // preferência na primeira carga pós-Fase 4 — vira o default de
    // operator/technical. Supervisor não existia antes, então nasce mudo
    // pelo default acima, não pelo valor legado.
    const legacy = localStorage.getItem(MUTED_STORAGE_KEY);
    if (legacy !== null) {
      const legacyMuted = legacy === "1";
      return { ...DEFAULT_MUTED_BY_PROFILE, operator: legacyMuted, technical: legacyMuted };
    }
    return DEFAULT_MUTED_BY_PROFILE;
  } catch {
    return DEFAULT_MUTED_BY_PROFILE;
  }
}

function persistMutedByProfile(map: Record<ViewMode, boolean>) {
  try {
    localStorage.setItem(MUTED_BY_PROFILE_STORAGE_KEY, JSON.stringify(map));
  } catch {
    // localStorage indisponível (modo privado etc.) — só não persiste
  }
}

// Modo persiste (é um perfil de tela, não um estado transiente). A aba ativa
// dentro de cada modo NÃO persiste — fica em memória (ver activeTabByMode
// acima), só pra não resetar ao alternar de perfil e voltar.
function readStoredMode(): ViewMode {
  try {
    const stored = localStorage.getItem(MODE_STORAGE_KEY);
    return stored === "technical" || stored === "supervisor" ? stored : "operator";
  } catch {
    return "operator";
  }
}

/**
 * Tudo que é DADO DE CONTA e não pode sobreviver a um logout.
 *
 * Num terminal compartilhado de chão de fábrica, a próxima pessoa a entrar não
 * pode ver, nem por um instante, os alertas e as detecções da anterior. Só
 * `mode`/`screen` ficam de fora porque o login seguinte os define na hora.
 */
const ESTADO_LIMPO: Partial<DashboardState> = {
  cameras: [],
  camerasLoading: true,
  camId: null,
  operatorCam: null,
  activeAlerts: [],
  alertHistory: [],
  lastAlertCreatedId: null,
  timeline: [],
  compliance: null,
  riskScore: null,
  riskTrend: null,
  model: null,
  settings: null,
  overlay: null,
  riskArea: null,
  features: [],
  lastDetections: [],
  lastPose: null,
  videoStream: null,
  running: false,
  frameCounter: 0,
  wsFrameCounter: 0,
  fps: 0,
  lastAnalysisAt: null,
  lastError: null,
  message: null,
  bootstrapping: true,
  riskEditorActive: false,
  riskEditorPoints: [],
};

export const useDashboardStore = create<DashboardState>()(
  devtools(
    (set) => ({
      user: null,
      authChecked: false,
      login: async (email, password) => {
        const res = await apiLogin(email, password);
        aplicarSessao(res.user);
      },
      logout: async () => {
        try {
          await apiLogout();
        } finally {
          // Derruba o socket junto: a conexão foi autorizada pela sessão que
          // acabou de morrer.
          socket.disconnect();
          // E ZERA o estado sensível. Sem isto, o próximo login (possivelmente
          // de outra pessoa, no mesmo terminal de chão de fábrica) abria a tela
          // já pintada com alertas, pessoas detectadas e câmeras da conta
          // anterior, antes de qualquer request novo responder.
          set({ ...ESTADO_LIMPO, user: null, authChecked: true }, false, "logout");
        }
      },
      checkSession: async () => {
        try {
          const res = await getMe();
          if (res.user) aplicarSessao(res.user);
          set({ authChecked: true }, false, "checkSession:done");
        } catch {
          set({ user: null, authChecked: true }, false, "checkSession:error");
        }
      },

      connected: socket.connected,
      mode: readStoredMode(),

      mutedByProfile: readStoredMutedByProfile(),
      muted: readStoredMutedByProfile()[readStoredMode()],
      toggleMuted: () =>
        set(
          (state) => {
            const next = !state.muted;
            const nextByProfile = { ...state.mutedByProfile, [state.mode]: next };
            persistMutedByProfile(nextByProfile);
            return { muted: next, mutedByProfile: nextByProfile };
          },
          false,
          "toggleMuted",
        ),

      theme: readStoredTheme(),
      toggleTheme: () =>
        set(
          (state) => {
            const next: Theme = effectiveTheme(state.theme) === "dark" ? "light" : "dark";
            persistTheme(next);
            applyTheme(next);
            return { theme: next };
          },
          false,
          "toggleTheme",
        ),

      activeTabByMode: { ...DEFAULT_ACTIVE_TAB_BY_MODE },
      setActiveTab: (mode, tabKey) =>
        set((state) => ({ activeTabByMode: { ...state.activeTabByMode, [mode]: tabKey } }), false, "setActiveTab"),

      commandPaletteOpen: false,
      setCommandPaletteOpen: (open) => set({ commandPaletteOpen: open }, false, "setCommandPaletteOpen"),

      bootstrapping: true,
      allClearAt: null,

      running: false,
      frameCounter: 0,
      lastError: null,
      videoStream: null,

      features: [],
      overlay: null,
      settings: null,
      riskArea: null,
      riskScore: null,
      riskTrend: null,
      model: null,
      compliance: null,
      activeAlerts: [],
      lastAlertCreatedId: null,
      alertHistory: [],
      timeline: [],
      message: null,
      lastAnalysisAt: null,
      wsFrameCounter: 0,
      fps: 0,
      lastDetections: [],
      lastPose: null,

      riskEditorActive: false,
      riskEditorPoints: [],
      toggleRiskEditor: () => set((state) => ({ riskEditorActive: !state.riskEditorActive }), false, "toggleRiskEditor"),
      clearRiskEditorPoints: () => set({ riskEditorPoints: [] }, false, "clearRiskEditorPoints"),
      addRiskEditorPoint: (point) =>
        set((state) => ({ riskEditorPoints: [...state.riskEditorPoints, point] }), false, "addRiskEditorPoint"),
      moveRiskEditorPoint: (index, point) =>
        set(
          (state) => ({ riskEditorPoints: state.riskEditorPoints.map((p, i) => (i === index ? point : p)) }),
          false,
          "moveRiskEditorPoint",
        ),
      resetRiskEditorFromServer: () =>
        set(
          (state) => ({ riskEditorPoints: (state.riskArea?.polygon || []).map((p) => ({ x: p.x, y: p.y })) }),
          false,
          "resetRiskEditorFromServer",
        ),

      setMode: (mode) =>
        set(
          (state) => {
            try {
              localStorage.setItem(MODE_STORAGE_KEY, mode);
            } catch {
              // localStorage indisponível (modo privado etc.) — só não persiste
            }
            // Trava de navegação do Operador: ele só tem a câmera do setor
            // dele, então trocar pra Operador sempre pousa direto no kiosk
            // daquela câmera, nunca no grid (ele não pode "ver as outras").
            // Técnico/Supervisor voltam pro grid — não têm uma câmera "dona"
            // fixa, então grid é o ponto de partida natural dos dois.
            const nextScreen: CameraScreen = mode === "operator" ? "kiosk" : mode === "supervisor" ? "home" : "grid";
            const nextCamId = mode === "operator" ? state.operatorCam : state.camId;
            // Som "ao vivo" segue o default/última preferência do perfil pra
            // onde se está indo, não do perfil anterior.
            return { mode, muted: state.mutedByProfile[mode], screen: nextScreen, camId: nextCamId };
          },
          false,
          "setMode",
        ),

      // ---- multi-câmera REAL (Fase A, Passo 6) ----------------------------
      cameras: [],
      camerasLoading: true,
      camId: null,
      operatorCam: null,
      screen: readStoredMode() === "operator" ? "kiosk" : readStoredMode() === "supervisor" ? "home" : "grid",
      loadCameras: async () => {
        try {
          const res = await listCameras();
          set(
            (state) => {
              const cameras = res.items;
              const ids = cameras.map((c) => c.id);
              // Se a câmera selecionada/simulada sumiu (ou nenhuma nunca foi
              // escolhida ainda), cai pra primeira da lista — sem isso, o
              // kiosk/foco ficam presos numa id que não existe mais.
              const camId = state.camId !== null && ids.includes(state.camId) ? state.camId : (cameras[0]?.id ?? null);
              const operatorCam = state.operatorCam !== null && ids.includes(state.operatorCam) ? state.operatorCam : (cameras[0]?.id ?? null);
              return { cameras, camerasLoading: false, camId, operatorCam };
            },
            false,
            "loadCameras",
          );
        } catch (error) {
          console.error("[loadCameras] failed", error);
          set({ camerasLoading: false }, false, "loadCameras:error");
        }
      },
      setScreen: (screen) => set({ screen }, false, "setScreen"),
      setCamId: (camId) => {
        set({ camId }, false, "setCamId");
        // A zona de risco é de cada câmera: ao trocar o foco, carrega a DESTA
        // câmera (senão o editor mostraria — e sobrescreveria — a da anterior).
        getRiskArea(camId)
          .then((res) => {
            if (useDashboardStore.getState().camId !== camId) return;
            set(
              {
                riskArea: res.risk_area,
                riskEditorPoints: (res.risk_area.polygon || []).map((p) => ({ x: p.x, y: p.y })),
              },
              false,
              "setCamId:riskArea",
            );
          })
          .catch(() => undefined);
        // Recursos ligados/desligados também são de cada câmera.
        getFeatures(camId)
          .then((res) => {
            if (useDashboardStore.getState().camId !== camId) return;
            set({ features: res.features }, false, "setCamId:features");
          })
          .catch(() => undefined);
      },
      setOperatorCam: (camId) =>
        set(
          (state) => ({
            operatorCam: camId,
            // Se o Operador já está olhando o kiosk, trocar o "setor
            // simulado" precisa refletir na tela na hora — senão o
            // seletor muda mas o vídeo continua mostrando a câmera antiga.
            camId: state.mode === "operator" ? camId : state.camId,
          }),
          false,
          "setOperatorCam",
        ),
      toggleCameraFeature: (camId, key) => {
        const current = useDashboardStore.getState().cameras.find((c) => c.id === camId);
        if (!current) return;
        const nextValue = !current.features[key];
        // Otimista: atualiza a UI na hora, sem esperar a resposta — reverte
        // sozinho no próximo loadCameras() se a chamada falhar (o catch já
        // loga o erro; não reverte manualmente pra não complicar o fluxo
        // por um caso raro de falha de rede numa ação de toggle).
        set(
          (state) => ({
            cameras: state.cameras.map((cam) => (cam.id === camId ? { ...cam, features: { ...cam.features, [key]: nextValue } } : cam)),
          }),
          false,
          "toggleCameraFeature:optimistic",
        );
        apiUpdateCamera(camId, { features: { [key]: nextValue } })
          .then((updated) =>
            set(
              (state) => ({ cameras: state.cameras.map((cam) => (cam.id === camId ? updated : cam)) }),
              false,
              "toggleCameraFeature:confirmed",
            ),
          )
          .catch((error) => console.error("[toggleCameraFeature] failed", error));
      },
      showMessage: (text, tone = "warning") => set({ message: { text, tone } }, false, "showMessage"),
      hideMessage: () => set({ message: null }, false, "hideMessage"),

      bootstrap: async () => {
        // allSettled, não all: /status, /settings, /overlay e /risk-area
        // operam sobre "a câmera padrão" e respondem 404 quando NENHUMA câmera
        // está cadastrada — que é o estado inicial legítimo de todo clone novo
        // (não existe mais seed automático). Com Promise.all, esse 404
        // rejeitava a promessa inteira e derrubava junto os dados que não
        // dependem de câmera nenhuma (features, alertas, eventos, risco), e a
        // primeira tela abria completamente vazia.
        const [status, featuresRes, settingsRes, overlayRes, riskAreaRes, riskScoreRes, riskTrendRes, eventsRes, alertsRes] =
          await Promise.allSettled([
            getStatus(),
            getFeatures(),
            getSettings(),
            getOverlay(),
            getRiskArea(),
            getRiskScore(),
            getRiskTrend(),
            listEvents({ limit: 80, eventType: "alert_resolved" }),
            listAlerts({ limit: 50 }),
          ]);

        const value = <T,>(result: PromiseSettledResult<T>): T | null => (result.status === "fulfilled" ? result.value : null);

        const statusValue = value(status);
        const riskArea = value(riskAreaRes)?.risk_area ?? null;
        set(
          {
            running: statusValue?.running ?? false,
            frameCounter: statusValue?.frame_counter ?? 0,
            lastError: statusValue?.last_error ?? null,
            videoStream: statusValue?.video ?? null,
            model: statusValue?.model ?? null,
            activeAlerts: statusValue?.active_alerts ?? [],
            features: value(featuresRes)?.features ?? [],
            settings: value(settingsRes)?.settings ?? null,
            overlay: value(overlayRes)?.overlay ?? null,
            riskArea,
            riskScore: value(riskScoreRes),
            riskTrend: value(riskTrendRes),
            timeline: value(eventsRes)?.items ?? [],
            alertHistory: value(alertsRes)?.items ?? [],
            riskEditorPoints: (riskArea?.polygon || []).map((p) => ({ x: p.x, y: p.y })),
            bootstrapping: false,
          },
          false,
          "bootstrap",
        );

        // Só as chamadas que NÃO dependem de câmera contam como falha real de
        // bootstrap. As escopadas na câmera padrão falharem sem câmera
        // cadastrada é esperado, e a tela já tem empty state pra isso.
        const cameraIndependent = [featuresRes, riskScoreRes, riskTrendRes, eventsRes, alertsRes];
        const failed = cameraIndependent.filter((item) => item.status === "rejected");
        if (failed.length) {
          set({ message: { text: "Não foi possível carregar todos os dados do painel.", tone: "error" } }, false, "bootstrap:partial");
          throw (failed[0] as PromiseRejectedResult).reason;
        }
      },

      start: async () => {
        const status = await startMonitor();
        set({ running: status.running, frameCounter: status.frame_counter }, false, "start");
      },
      stop: async () => {
        const status = await stopMonitor();
        set({ running: status.running }, false, "stop");
      },
      updateFeatures: async (updates) => {
        const res = await apiPatchFeatures(updates, useDashboardStore.getState().camId);
        set({ features: res.features }, false, "updateFeatures");
      },
      updateOverlay: async (updates) => {
        const res = await apiPatchOverlay(updates);
        set({ overlay: res.overlay }, false, "updateOverlay");
      },
      updateSettings: async (updates) => {
        const res = await apiPatchSettings(updates);
        set({ settings: res.settings, message: { text: "Configurações runtime salvas.", tone: "ok" } }, false, "updateSettings");
      },
      updateRiskArea: async (payload) => {
        try {
          const res = await apiPatchRiskArea(payload, useDashboardStore.getState().camId);
          set({ riskArea: res.risk_area, message: { text: "Área de risco atualizada no backend.", tone: "ok" } }, false, "updateRiskArea");
        } catch (error) {
          set({ message: { text: error instanceof Error ? error.message : "Falha ao salvar área de risco.", tone: "error" } }, false, "updateRiskArea:error");
          throw error;
        }
      },
      markFalsePositive: async (alertId, reason) => {
        try {
          const res = await apiMarkFalsePositive(alertId, reason);
          resolveAlert(res.alert);
          set({ message: { text: "Alerta marcado como falso positivo.", tone: "ok" } }, false, "markFalsePositive");
        } catch (error) {
          set({ message: { text: error instanceof Error ? error.message : "Falha ao marcar falso positivo.", tone: "error" } }, false, "markFalsePositive:error");
        }
      },
      acknowledgeAlert: async (alertId, note) => {
        try {
          const res = await apiAcknowledgeAlert(alertId, note);
          upsertActiveAlert(res.alert);
          set({ message: { text: "Registrado: colaborador avisado.", tone: "ok" } }, false, "acknowledgeAlert");
        } catch (error) {
          set({ message: { text: error instanceof Error ? error.message : "Falha ao registrar o aviso.", tone: "error" } }, false, "acknowledgeAlert:error");
        }
      },
    }),
    { name: "visionepi-dashboard" },
  ),
);

// "Voltou tudo certo" só conta como celebração se havia alerta ativo antes —
// não no boot inicial (que já começa em zero). Watcher de módulo, não parte
// de nenhum setter específico, porque activeAlerts muda em vários pontos
// (bootstrap, ws:active_alerts, ws:analysis, resolveAlert, upsertActiveAlert).
let hadActiveAlerts = false;
useDashboardStore.subscribe((state) => {
  const has = state.activeAlerts.length > 0;
  if (has) {
    hadActiveAlerts = true;
    return;
  }
  if (hadActiveAlerts) {
    hadActiveAlerts = false;
    if (!state.muted) playAllClearChime();
    useDashboardStore.setState({ allClearAt: Date.now() }, false, "allClear:pulse");
  }
});

/**
 * Um payload de socket só entra no estado "da tela" se for da câmera em foco.
 *
 * O backend roda um CameraWorker por câmera e todos emitem no mesmo canal.
 * Antes do carimbo de `camera_id`, dois workers ativos sobrescreviam
 * `compliance`/`model`/`activeAlerts` um do outro a cada frame (~12x/s cada),
 * e um `stop()` numa câmera marcava `running: false` pro dashboard inteiro.
 *
 * `camera_id` ausente/null = evento global ou legado: aceita, porque é o
 * comportamento single-camera de sempre.
 */
function belongsToFocusedCamera(payload: { camera_id?: number | null }): boolean {
  if (payload.camera_id == null) return true;
  const focused = useDashboardStore.getState().camId;
  return focused == null || payload.camera_id === focused;
}

/**
 * Aplica a sessão recém-obtida ao estado.
 *
 * O papel define o modo — quem entra como operador cai no kiosk. E a câmera do
 * setor vem de `user.camera_id`, definido pelo supervisor: é isso que substitui
 * o antigo seletor "simular setor" da topbar.
 */
function aplicarSessao(user: AuthUser) {
  useDashboardStore.setState({ user, authChecked: true }, false, "sessao:aplicar");
  useDashboardStore.getState().setMode(user.role);
  if (user.camera_id != null) {
    useDashboardStore.getState().setOperatorCam(user.camera_id);
  }
}

/** Shared by the alert_resolved socket handler and the false-positive REST action. */
function resolveAlert(alert: Alert) {
  useDashboardStore.setState(
    (state) => ({
      activeAlerts: state.activeAlerts.filter((item) => item.id !== alert.id),
      alertHistory: [alert, ...state.alertHistory.filter((item) => item.id !== alert.id)].slice(0, MAX_HISTORY),
    }),
    false,
    "resolveAlert",
  );
}

function upsertActiveAlert(alert: Alert) {
  if (alert.status === "resolved") return;
  useDashboardStore.setState(
    (state) => ({
      activeAlerts: [...state.activeAlerts.filter((item) => item.id !== alert.id), alert],
    }),
    false,
    "upsertActiveAlert",
  );
}

/**
 * Liga cada evento Socket.IO do backend a uma atualização do store.
 * Chamar uma vez na raiz do app (ver main.tsx). Em dev, loga cada evento para
 * inspeção sem precisar da extensão Redux DevTools.
 */
export function subscribeToServerEvents(): () => void {
  const set = useDashboardStore.setState;
  // Track exact handler references: socket.off(event) with no handler wipes
  // every listener for that event, which breaks under React 18 StrictMode's
  // dev-only double-invoke (mount -> cleanup -> mount) — the first mount's
  // cleanup would delete the second mount's listeners too.
  const teardown: Array<() => void> = [];

  function on<K extends ServerEventName>(event: K, handler: (payload: ServerEvents[K]) => void) {
    const wrapped = (payload: ServerEvents[K]) => {
      // Só em dev: `analysis` chega ~12x/s com detecções + 33 landmarks de
      // pose. Em produção isso inundava o console e mantinha cada payload
      // vivo na memória do devtools.
      if (import.meta.env.DEV) console.log(`[ws] ${event}`, payload);
      handler(payload);
    };
    // TS can't prove `wrapped`'s type lines up with the overloaded, K-indexed
    // `Socket#on` signature when K is itself generic (a known TS limitation
    // with higher-order generics against overloads) — every call site below
    // is a concrete, correctly-typed K, so this cast is safe.
    (socket.on as (event: K, handler: (payload: ServerEvents[K]) => void) => void)(event, wrapped);
    teardown.push(() => (socket.off as (event: K, handler: (payload: ServerEvents[K]) => void) => void)(event, wrapped));
  }

  const onConnect = () => set({ connected: true }, false, "ws:connect");
  const onDisconnect = () => set({ connected: false }, false, "ws:disconnect");
  socket.on("connect", onConnect);
  socket.on("disconnect", onDisconnect);
  teardown.push(() => socket.off("connect", onConnect), () => socket.off("disconnect", onDisconnect));

  on("monitor_status", (status) => {
    if (!belongsToFocusedCamera(status)) return;
    set(
      {
        running: status.running,
        frameCounter: status.frame_counter,
        lastError: status.last_error,
        videoStream: status.video ?? null,
        model: status.model,
        activeAlerts: status.active_alerts,
        overlay: status.overlay,
        settings: status.settings,
        riskArea: status.risk_area,
      },
      false,
      "ws:monitor_status",
    );
  });
  on("features_updated", (payload) => {
    // Evento de outra câmera não pode sobrescrever a lista da que está na tela.
    const atual = useDashboardStore.getState().camId;
    if (payload.camera_id != null && atual != null && payload.camera_id !== atual) return;
    set({ features: payload.features }, false, "ws:features_updated");
  });
  on("model_diagnostics", (model) => {
    if (!belongsToFocusedCamera(model)) return;
    set({ model }, false, "ws:model_diagnostics");
    if (model.warning) useDashboardStore.getState().showMessage(model.warning, model.error ? "error" : "warning");
    else useDashboardStore.getState().hideMessage();
  });
  on("settings_updated", (settings) => {
    if (belongsToFocusedCamera(settings)) set({ settings }, false, "ws:settings_updated");
  });
  on("overlay_updated", (overlay) => {
    if (belongsToFocusedCamera(overlay)) set({ overlay }, false, "ws:overlay_updated");
  });
  on("risk_area_updated", (riskArea) => {
    if (belongsToFocusedCamera(riskArea)) set({ riskArea }, false, "ws:risk_area_updated");
  });
  on("risk_score", (riskScore) => {
    if (belongsToFocusedCamera(riskScore)) set({ riskScore }, false, "ws:risk_score");
  });
  on("compliance_state", (compliance) => {
    if (belongsToFocusedCamera(compliance)) set({ compliance }, false, "ws:compliance_state");
  });
  on("active_alerts", (payload) => {
    if (belongsToFocusedCamera(payload)) set({ activeAlerts: payload.items }, false, "ws:active_alerts");
  });
  on("analysis", (payload) => {
    if (!belongsToFocusedCamera(payload)) return;
    set(
      (state) => {
        const now = Date.now();
        if (state.lastAnalysisAt) {
          const delta = Math.max(1, now - state.lastAnalysisAt);
          fpsSamples.push(1000 / delta);
          if (fpsSamples.length > FPS_SAMPLE_WINDOW) fpsSamples.shift();
        }
        const fps = fpsSamples.length ? fpsSamples.reduce((a, b) => a + b, 0) / fpsSamples.length : state.fps;
        return {
          compliance: payload.compliance,
          model: payload.model,
          activeAlerts: payload.alerts,
          lastAnalysisAt: now,
          wsFrameCounter: state.wsFrameCounter + 1,
          fps,
          lastDetections: payload.detections,
          lastPose: payload.pose,
        };
      },
      false,
      "ws:analysis",
    );
  });
  // timeline_event / alert_* NÃO passam pelo gate de propósito: o histórico e
  // a linha do tempo são consolidados (todas as câmeras). Quem é por câmera é
  // o estado "ao vivo" acima.
  on("timeline_event", (event) =>
    set((state) => ({ timeline: [event, ...state.timeline].slice(0, MAX_TIMELINE) }), false, "ws:timeline_event"),
  );
  on("alert_created", (alert) => {
    if (!belongsToFocusedCamera(alert)) return;
    upsertActiveAlert(alert);
    set({ lastAlertCreatedId: alert.id }, false, "ws:alert_created:lastId");
    if (!useDashboardStore.getState().muted && SEVERITIES_WITH_CHIME.has(alert.severity)) playAlertChime();
  });
  on("alert", (alert) => {
    if (belongsToFocusedCamera(alert)) upsertActiveAlert(alert);
  });
  on("alert_updated", (alert) => {
    if (belongsToFocusedCamera(alert)) upsertActiveAlert(alert);
  });
  on("alert_resolved", (alert) => {
    if (belongsToFocusedCamera(alert)) resolveAlert(alert);
  });

  if (!socket.connected) socket.connect();

  // Revalida a sessão do socket periodicamente. Um socket aberto vive horas:
  // sem isto, desativar alguém (ou trocar a senha dela) não teria efeito
  // nenhum sobre o feed ao vivo que já estava chegando — vídeo, alertas e
  // pessoas detectadas continuariam fluindo pra quem perdeu o acesso.
  const revalidacao = setInterval(() => {
    if (socket.connected) socket.emit("revalidate");
  }, 30_000);
  teardown.push(() => clearInterval(revalidacao));

  // Sparkline não precisa da cadência do socket (buckets são de 1h) — um
  // refresh a cada 5min é mais que suficiente e evita bater a rota extra
  // toda vez que risk_score chega via WS.
  const trendInterval = setInterval(() => {
    getRiskTrend()
      .then((riskTrend) => set({ riskTrend }, false, "trend:refresh"))
      .catch((err) => console.error("[risk-trend] refresh failed", err));
  }, 5 * 60 * 1000);
  teardown.push(() => clearInterval(trendInterval));

  return () => teardown.forEach((fn) => fn());
}
