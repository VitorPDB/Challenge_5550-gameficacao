import { useEffect, useState } from "react";
import type { ReactNode } from "react";
import { AnimatePresence, motion, useReducedMotion } from "motion/react";
import { effectiveTheme, ROLE_ACCESS, useDashboardStore } from "../store/dashboardStore";
import { Mark, Wordmark } from "./brand";
import { exportAlertsCsv } from "./export";

// Mesma curva de tokens.css (--ease-out). Motion precisa do bezier numérico.
export const EASE = [0.23, 1, 0.32, 1] as const;

const ROTULO_DO_PAPEL: Record<string, string> = {
  operator: "Operador",
  technical: "Técnico",
  supervisor: "Supervisor",
};

/** Sol e lua em traço de 1,5 px na cor do texto ao lado. */
function ThemeIcon({ dark }: { dark: boolean }) {
  return dark ? (
    <svg viewBox="0 0 24 24" aria-hidden="true">
      <path d="M20 14.5A8 8 0 0 1 9.5 4a8 8 0 1 0 10.5 10.5z" />
    </svg>
  ) : (
    <svg viewBox="0 0 24 24" aria-hidden="true">
      <circle cx="12" cy="12" r="4" />
      <path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4" />
    </svg>
  );
}

export function ThemeToggle() {
  const theme = useDashboardStore((s) => s.theme);
  const toggleTheme = useDashboardStore((s) => s.toggleTheme);
  const dark = effectiveTheme(theme) === "dark";
  return (
    <button
      type="button"
      className="icon"
      onClick={toggleTheme}
      aria-label={dark ? "Mudar para o tema claro" : "Mudar para o tema escuro"}
      title={dark ? "Tema escuro" : "Tema claro"}
    >
      <ThemeIcon dark={dark} />
    </button>
  );
}

/** Alto-falante em traço, riscado quando mudo. O estado também está no
 * aria-label e no title, então não depende só do desenho. */
function SpeakerIcon({ muted }: { muted: boolean }) {
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true">
      <path d="M4 9.5v5h3.5l4.5 3.5v-12L7.5 9.5H4z" />
      {muted ? <path d="M16 9.5l5 5M21 9.5l-5 5" /> : <path d="M16.5 8.5a5 5 0 0 1 0 7" />}
    </svg>
  );
}

export function MuteToggle() {
  const muted = useDashboardStore((s) => s.muted);
  const toggleMuted = useDashboardStore((s) => s.toggleMuted);
  return (
    <button
      type="button"
      className="icon"
      onClick={toggleMuted}
      aria-pressed={muted}
      aria-label={muted ? "Ativar som de alertas" : "Silenciar som de alertas"}
      title={muted ? "Som desligado" : "Som ligado"}
    >
      <SpeakerIcon muted={muted} />
    </button>
  );
}

/** Aviso para o Operador cuja conta ainda não tem setor atribuído.
 *
 * O backend não entrega câmera nenhuma nesse estado (ver camera_scope em
 * app/utils/auth.py), então não há o que escolher, só a quem pedir. */
function OperadorSemSetor() {
  const user = useDashboardStore((s) => s.user);
  if (user?.camera_id != null) return null;
  return (
    <span className="appbar-note" role="status" title="Peça ao supervisor para atribuir a câmera do seu setor.">
      Sem setor atribuído, peça ao supervisor
    </span>
  );
}

/** Quem está logado + sair. */
function UserMenu() {
  const user = useDashboardStore((s) => s.user);
  const logout = useDashboardStore((s) => s.logout);
  const [saindo, setSaindo] = useState(false);
  if (!user) return null;

  return (
    <div className="user-menu">
      <div className="user-menu-info">
        <strong>{user.name}</strong>
        <span>{ROTULO_DO_PAPEL[user.role] ?? user.role}</span>
      </div>
      <button
        type="button"
        className="ghost"
        disabled={saindo}
        onClick={() => {
          setSaindo(true);
          logout().finally(() => setSaindo(false));
        }}
      >
        {saindo ? "Saindo…" : "Sair"}
      </button>
    </div>
  );
}

/** Uma barra translúcida: logotipo à esquerda, ações à direita. Os dados que
 * a antiga faixa de status mostrava vivem agora sobre o vídeo, na tela de
 * foco, e embaixo do título da tela, na grade. */
export function Topbar() {
  const mode = useDashboardStore((s) => s.mode);
  const screen = useDashboardStore((s) => s.screen);
  const setScreen = useDashboardStore((s) => s.setScreen);
  const start = useDashboardStore((s) => s.start);
  const stop = useDashboardStore((s) => s.stop);
  const running = useDashboardStore((s) => s.running);
  const setCommandPaletteOpen = useDashboardStore((s) => s.setCommandPaletteOpen);
  const alertHistory = useDashboardStore((s) => s.alertHistory);
  const access = ROLE_ACCESS[mode];
  // Feedback de "seu clique registrou" durante a latência real do REST.
  const [pending, setPending] = useState<"start" | "stop" | null>(null);
  const runAction = (which: "start" | "stop", action: () => Promise<void>) => {
    setPending(which);
    action()
      .catch((err) => console.error(err))
      .finally(() => setPending(null));
  };
  return (
    <header className="appbar">
      <span className="brand-lockup">
        <Mark size={24} />
        <Wordmark />
      </span>
      <div className="appbar-actions">
        <button type="button" className={screen === "journey" ? "primary" : "ghost"}
          aria-current={screen === "journey" ? "page" : undefined} onClick={() => setScreen("journey")}>
          Jornada
        </button>
        {mode === "operator" && <OperadorSemSetor />}
        <button type="button" className="ghost" onClick={() => setCommandPaletteOpen(true)}>
          Buscar <kbd>Ctrl K</kbd>
        </button>
        {access.hasOverview && (
          <button
            type="button"
            className="ghost"
            disabled={!alertHistory.length}
            title="Baixa em CSV o histórico de alertas carregado agora"
            onClick={() => exportAlertsCsv(alertHistory)}
          >
            Exportar
          </button>
        )}
        <ThemeToggle />
        <MuteToggle />
        <UserMenu />
        {running ? (
          <button
            type="button"
            className={`stop ${pending === "stop" ? "is-pending" : ""}`.trim()}
            disabled={pending !== null}
            onClick={() => runAction("stop", stop)}
          >
            {pending === "stop" ? "Parando…" : "Parar"}
          </button>
        ) : (
          <button
            type="button"
            className={`primary ${pending === "start" ? "is-pending" : ""}`.trim()}
            disabled={pending !== null}
            onClick={() => runAction("start", start)}
          >
            {pending === "start" ? "Iniciando…" : "Iniciar"}
          </button>
        )}
      </div>
    </header>
  );
}

/** Cabeçalho de tela: título em 28 px e, ao lado, uma linha secundária. */
export function ScreenHead({ title, sub, actions }: { title: string; sub?: string | null; actions?: ReactNode }) {
  return (
    <div className="screen-head">
      <h1>{title}</h1>
      {sub && <span className="screen-sub">{sub}</span>}
      {actions && <div className="screen-actions">{actions}</div>}
    </div>
  );
}

export function MessageBar() {
  const { message, hideMessage } = useDashboardStore();
  const shouldReduceMotion = useReducedMotion();
  return (
    <AnimatePresence initial={false}>
      {message && (
        <motion.section
          key={message.text}
          className={`message-bar ${message.tone === "warning" ? "" : message.tone}`.trim()}
          onClick={hideMessage}
          role="status"
          initial={shouldReduceMotion ? false : { opacity: 0, scale: 0.98 }}
          animate={{ opacity: 1, scale: 1 }}
          exit={{ opacity: 0 }}
          transition={{ duration: shouldReduceMotion ? 0 : 0.2, ease: EASE }}
        >
          {message.tone === "ok" && <span className="dot" />}
          {message.text}
        </motion.section>
      )}
    </AnimatePresence>
  );
}

/** Estado do vídeo da câmera padrão, derivado como antes: análise fresca
 * -> ok, velha -> instável, muito velha -> congelado. Recalculado a cada
 * segundo porque envelhece mesmo sem evento novo. */
export function useVideoStatus() {
  const running = useDashboardStore((s) => s.running);
  const lastAnalysisAt = useDashboardStore((s) => s.lastAnalysisAt);
  const video = useDashboardStore((s) => s.videoStream);
  const [, forceTick] = useState(0);
  useEffect(() => {
    const id = setInterval(() => forceTick((n) => n + 1), 1000);
    return () => clearInterval(id);
  }, []);
  if (!running) return { status: "warn" as const, label: "parado" };

  if (video) {
    // Modo fixture vem ANTES dos estados de falha: é fallback deliberado,
    // não degradação. Fica em `warn`, chama atenção sem alarmar.
    if (video.modo === "fixture") {
      return { status: "warn" as const, label: "modo fixture, fonte de demonstração" };
    }
    if (video.state === "unavailable") {
      return { status: "error" as const, label: `sem sinal, nova tentativa em ${Math.ceil(video.seconds_until_retry)}s` };
    }
    if (video.state === "reconnecting") {
      return { status: "error" as const, label: `reconectando (tentativa ${video.reconnect_attempts + 1})` };
    }
  }

  if (!lastAnalysisAt) return { status: "warn" as const, label: "aguardando frame" };
  const age = Date.now() - lastAnalysisAt;
  if (age > 4500) return { status: "error" as const, label: "congelado" };
  if (age > 1800) return { status: "warn" as const, label: "instável" };
  return { status: "ok" as const, label: "recebendo" };
}
