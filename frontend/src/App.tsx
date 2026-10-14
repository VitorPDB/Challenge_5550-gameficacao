import { useCallback, useEffect, useState } from "react";
import { subscribeToServerEvents, useDashboardStore, ROLE_ACCESS } from "./store/dashboardStore";
import { Topbar, MessageBar, ScreenHead } from "./components/layout";
import { CameraGrid, type EstadoDaCamera } from "./components/camera-grid";
import { OperatorKiosk } from "./components/operator-kiosk";
import { GateKiosk } from "./components/gate";
import { AccountsScreen, SupervisorHome } from "./components/accounts";
import { CameraFocus } from "./components/camera-focus";
import { CommandPalette } from "./components/command-palette";
import { Panel } from "./components/common";
import { LoginScreen } from "./components/login";
import { MissionsPanel } from "./components/prevention";

function JourneyScreen() {
  const cameras = useDashboardStore(s => s.cameras);
  const mode = useDashboardStore(s => s.mode);
  const camId = useDashboardStore(s => s.camId);
  const setScreen = useDashboardStore(s => s.setScreen);
  const [selected, setSelected] = useState<number | null>(null);
  const camera = cameras.find(c => c.id === selected) ?? cameras.find(c => c.id === camId) ?? cameras[0];
  const roleText = mode === "supervisor" ? "Crie metas para as equipes e verifique as ações realizadas."
    : mode === "technical" ? "Transforme necessidades do setor em missões e acompanhe a execução."
      : "Participe das missões da equipe e envie as ações realizadas para revisão.";
  return <div className="screen">
    <ScreenHead title="Jornada de prevenção" sub={roleText} actions={<button type="button" className="ghost" onClick={() => {
      window.history.replaceState(null, "", window.location.pathname);
      setScreen(mode === "operator" ? "kiosk" : mode === "supervisor" ? "home" : "grid");
    }}>Voltar à operação</button>} />
    <p>Ganhe experiência por ações preventivas verificadas. Evolua em equipe, alcance metas e desbloqueie conquistas.</p>
    {mode !== "operator" && cameras.length > 1 && <label>Setor<select value={camera?.id ?? ""}
      onChange={e => setSelected(Number(e.target.value))}>{cameras.map(c => <option key={c.id} value={c.id}>{c.name}</option>)}</select></label>}
    {camera ? <><p>Setor: <strong>{camera.name}</strong></p><MissionsPanel key={camera.id} cameraId={camera.id} /></>
      : <p>Cadastre uma câmera ou peça ao supervisor para atribuir seu setor antes de iniciar a jornada.</p>}
  </div>;
}

// Placeholder do dashboard agregado do Supervisor. O conteúdo de verdade
// (risco consolidado, ranking por criticidade, feed combinado, auditoria de
// falso positivo, exportação) é o próximo passo. Existir aqui evita a tela
// ficar em branco se alguém clicar em "Visão geral" antes disso.
function SupervisorOverviewPlaceholder({ onBack }: { onBack: () => void }) {
  return (
    <div className="screen">
      <ScreenHead
        title="Visão geral"
        sub="Todas as câmeras"
        actions={
          <button type="button" className="ghost" onClick={onBack}>
            Voltar para a grade
          </button>
        }
      />
      <Panel title="Dashboard agregado" description="Consolidado do Supervisor.">
        <p className="empty-state">Em construção, é o próximo passo.</p>
      </Panel>
    </div>
  );
}

function GridScreen() {
  const cameras = useDashboardStore((s) => s.cameras);
  const mode = useDashboardStore((s) => s.mode);
  const setScreen = useDashboardStore((s) => s.setScreen);
  const access = ROLE_ACCESS[mode];

  // Cada cartão consulta o status da própria câmera; a linha embaixo do
  // título só agrega o que os cartões já sabem.
  const [estados, setEstados] = useState<Record<number, EstadoDaCamera>>({});
  const onEstado = useCallback((id: number, estado: EstadoDaCamera) => {
    setEstados((prev) => (prev[id] === estado ? prev : { ...prev, [id]: estado }));
  }, []);
  const noAr = cameras.filter((c) => estados[c.id]?.running).length;
  const alertas = cameras.reduce((n, c) => n + (estados[c.id]?.alertas.length ?? 0), 0);
  const sub = cameras.length
    ? `${noAr} de ${cameras.length} no ar, ${alertas} alerta${alertas === 1 ? "" : "s"} ativo${alertas === 1 ? "" : "s"}`
    : null;

  return (
    <div className="screen">
      <ScreenHead
        title="Câmeras"
        sub={sub}
        actions={
          access.hasOverview ? (
            <button type="button" className="ghost" onClick={() => setScreen("overview")}>
              Visão geral
            </button>
          ) : undefined
        }
      />
      <CameraGrid onEstado={onEstado} />
    </div>
  );
}

export default function App() {
  const user = useDashboardStore((s) => s.user);
  const authChecked = useDashboardStore((s) => s.authChecked);
  const checkSession = useDashboardStore((s) => s.checkSession);
  const mode = useDashboardStore((s) => s.mode);
  const screen = useDashboardStore((s) => s.screen);
  const camId = useDashboardStore((s) => s.camId);
  const cameras = useDashboardStore((s) => s.cameras);
  const camerasLoading = useDashboardStore((s) => s.camerasLoading);
  const hasCameras = useDashboardStore((s) => s.cameras.length > 0);
  const bootstrap = useDashboardStore((s) => s.bootstrap);
  const loadCameras = useDashboardStore((s) => s.loadCameras);
  const setScreen = useDashboardStore((s) => s.setScreen);

  // Primeiro descobre QUEM está logado. Só depois vale a pena abrir socket e
  // buscar dados: sem sessão, tudo isso responderia 401.
  useEffect(() => {
    checkSession().catch((err) => console.error("[checkSession] failed", err));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    if (!user) return;
    const unsubscribe = subscribeToServerEvents();
    bootstrap().catch((err) => console.error("[bootstrap] failed", err));
    loadCameras().catch((err) => console.error("[loadCameras] failed", err));
    return unsubscribe;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [user?.id]);

  useEffect(() => {
    if (!user) return;
    const openJourney = () => { if (window.location.hash === "#journey") setScreen("journey"); };
    openJourney();
    window.addEventListener("hashchange", openJourney);
    return () => window.removeEventListener("hashchange", openJourney);
  }, [user?.id, setScreen]);

  if (!authChecked) {
    return <div className="login-shell" aria-busy="true" />;
  }
  if (!user) {
    return <LoginScreen />;
  }

  // Roteamento por papel: Operador sempre kiosk (trava aplicada em setMode
  // no store, screen já chega como "kiosk"); Técnico/Supervisor navegam
  // entre grid/foco/overview livremente dentro do próprio `screen`.
  let content;
  if (camerasLoading) {
    content = <p className="centered-empty">Carregando câmeras…</p>;
  } else if (screen === "journey") {
    content = <JourneyScreen />;
  } else if (mode === "operator") {
    content =
      camId !== null ? (
        cameras.find((c) => c.id === camId)?.gate_required ? <GateKiosk camId={camId} /> : <OperatorKiosk camId={camId} />
      ) : (
        <p className="centered-empty">
          Nenhuma câmera cadastrada ainda. Peça ao Técnico ou ao Supervisor para cadastrar a câmera do seu setor.
        </p>
      );
  } else if (mode === "supervisor" && screen === "home") {
    content = <SupervisorHome />;
  } else if (mode === "supervisor" && screen === "accounts") {
    content = <AccountsScreen />;
  } else if (screen === "focus" && hasCameras) {
    content = <CameraFocus />;
  } else if (screen === "overview" && ROLE_ACCESS[mode].hasOverview) {
    content = <SupervisorOverviewPlaceholder onBack={() => setScreen("grid")} />;
  } else {
    content = (
      <>
        {mode === "supervisor" && (
          <div className="screen" style={{ paddingBottom: 0 }}>
            <button type="button" className="ghost small" onClick={() => setScreen("home")}>
              ← Início
            </button>
          </div>
        )}
        <GridScreen />
      </>
    );
  }

  return (
    <div className="shell">
      <CommandPalette />
      <Topbar />
      <MessageBar />
      {content}
    </div>
  );
}
