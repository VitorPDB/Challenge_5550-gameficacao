import { useEffect, useState } from "react";
import { useDashboardStore } from "../store/dashboardStore";
import { Panel } from "./common";
import { ScreenHead } from "./layout";
import { apiFetch } from "../api/client";
import type { AuthUser, UserRole } from "../api/types";

const PAPEL: Record<string, string> = { operator: "Operador", technical: "Técnico", supervisor: "Supervisor" };

const listUsers = () => apiFetch<{ items: AuthUser[] }>("/api/users");
const createUser = (body: { email: string; name: string; password: string; role: string; camera_id: number | null }) =>
  apiFetch<{ user: AuthUser }>("/api/users", { method: "POST", body: JSON.stringify(body) });
const patchUser = (id: number, body: Record<string, unknown>) =>
  apiFetch<{ user: AuthUser }>(`/api/users/${id}`, { method: "PATCH", body: JSON.stringify(body) });
const deleteUser = (id: number) => apiFetch<{ deleted: boolean }>(`/api/users/${id}`, { method: "DELETE" });

/** Tela inicial do supervisor: dois blocos grandes. */
export function SupervisorHome() {
  const setScreen = useDashboardStore((s) => s.setScreen);
  const cameras = useDashboardStore((s) => s.cameras);
  return (
    <div className="screen">
      <ScreenHead title="Início" sub="O que você quer fazer?" />
      <div className="home-tiles">
        <button type="button" className="home-tile" onClick={() => setScreen("journey")}>
          <strong>Jornada de prevenção</strong>
          <span>Gamificação: equipes, níveis, metas, conquistas e ações aguardando sua revisão.</span>
        </button>
        <button type="button" className="home-tile" onClick={() => setScreen("grid")}>
          <strong>Câmeras</strong>
          <span>{cameras.length} câmera(s) cadastrada(s). Monitoramento, alertas, zonas e portaria.</span>
        </button>
        <button type="button" className="home-tile" onClick={() => setScreen("accounts")}>
          <strong>Contas</strong>
          <span>Crie o acesso de cada operador e gerencie as contas existentes.</span>
        </button>
      </div>
    </div>
  );
}

/** Criar e administrar contas. Só o supervisor chega aqui. */
export function AccountsScreen() {
  const setScreen = useDashboardStore((s) => s.setScreen);
  const cameras = useDashboardStore((s) => s.cameras);
  const showMessage = useDashboardStore((s) => s.showMessage);
  const eu = useDashboardStore((s) => s.user);

  const [usuarios, setUsuarios] = useState<AuthUser[]>([]);
  const [nome, setNome] = useState("");
  const [email, setEmail] = useState("");
  const [senha, setSenha] = useState("");
  const [mostrar, setMostrar] = useState(false);
  const [papel, setPapel] = useState<UserRole>("operator");
  const [cameraId, setCameraId] = useState("");
  const [salvando, setSalvando] = useState(false);
  const [aba, setAba] = useState<"menu" | "nova" | "gerenciar">("menu");

  const carregar = () =>
    listUsers()
      .then((r) => setUsuarios(r.items))
      .catch((err) => showMessage(err instanceof Error ? err.message : "Falha ao listar contas.", "error"));

  useEffect(() => {
    carregar();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const nomeDaCamera = (id: number | null) => (id == null ? "—" : cameras.find((c) => c.id === id)?.name ?? `câmera ${id}`);
  const precisaCamera = papel === "operator";

  const criar = (e: React.FormEvent) => {
    e.preventDefault();
    if (precisaCamera && !cameraId) {
      showMessage("Escolha a câmera do setor deste operador.", "warning");
      return;
    }
    setSalvando(true);
    createUser({ email, name: nome, password: senha, role: papel, camera_id: precisaCamera ? Number(cameraId) : null })
      .then((r) => {
        showMessage(`Conta criada: ${r.user.email} (${PAPEL[r.user.role] ?? r.user.role}).`, "ok");
        setNome("");
        setEmail("");
        setSenha("");
        setCameraId("");
        return carregar();
      })
      .catch((err) => showMessage(err instanceof Error ? err.message : "Falha ao criar a conta.", "error"))
      .finally(() => setSalvando(false));
  };

  const acao = (promessa: Promise<unknown>, ok: string) =>
    promessa
      .then(() => {
        showMessage(ok, "ok");
        return carregar();
      })
      .catch((err) => showMessage(err instanceof Error ? err.message : "Falha na operação.", "error"));

  const trocarSenha = (u: AuthUser) => {
    const nova = window.prompt(`Nova senha para ${u.email}:`);
    if (nova) acao(patchUser(u.id, { password: nova }), "Senha alterada. As sessões abertas dessa conta foram encerradas.");
  };

  return (
    <div className="screen">
      <ScreenHead
        title="Contas"
        sub={aba === "nova" ? "Criar nova conta" : aba === "gerenciar" ? "Gerenciar contas existentes" : "Acessos ao sistema"}
        actions={
          <button type="button" className="ghost" onClick={() => (aba === "menu" ? setScreen("home") : setAba("menu"))}>
            {aba === "menu" ? "Voltar ao início" : "Voltar"}
          </button>
        }
      />

      {aba === "menu" && (
        <div className="home-tiles">
          <button type="button" className="home-tile" onClick={() => setAba("nova")}>
            <strong>Criar nova conta</strong>
            <span>Defina e-mail, senha e a câmera do setor do operador.</span>
          </button>
          <button type="button" className="home-tile" onClick={() => setAba("gerenciar")}>
            <strong>Gerenciar contas existentes</strong>
            <span>{usuarios.length} conta(s). Trocar senha, desativar ou apagar.</span>
          </button>
        </div>
      )}

      {aba === "nova" && (
      <Panel id="panel-new-account" title="Nova conta" description="Você define o e-mail e a senha. Passe os dados ao operador em mãos.">
        <form onSubmit={criar} className="account-form">
          <label>
            Nome
            <input value={nome} onChange={(e) => setNome(e.target.value)} required maxLength={120} autoComplete="off" />
          </label>
          <label>
            E-mail (login)
            <input type="email" value={email} onChange={(e) => setEmail(e.target.value)} required autoComplete="off" />
          </label>
          <label>
            Senha
            <span className="account-pass">
              <input
                type={mostrar ? "text" : "password"}
                value={senha}
                onChange={(e) => setSenha(e.target.value)}
                required
                autoComplete="new-password"
              />
              <button type="button" className="ghost small" onClick={() => setMostrar((v) => !v)}>
                {mostrar ? "Ocultar" : "Mostrar"}
              </button>
            </span>
          </label>
          <label>
            Tipo de conta
            <select value={papel} onChange={(e) => setPapel(e.target.value as UserRole)}>
              <option value="operator">Operador (vê só a câmera do setor)</option>
              <option value="technical">Técnico (todas as câmeras e configuração)</option>
              <option value="supervisor">Supervisor (tudo, inclusive contas)</option>
            </select>
          </label>
          {precisaCamera && (
            <label>
              Câmera do setor
              <select value={cameraId} onChange={(e) => setCameraId(e.target.value)} required disabled={cameras.length === 0}>
                <option value="">{cameras.length ? "Escolha uma câmera cadastrada…" : "Nenhuma câmera cadastrada"}</option>
                {cameras.map((c) => (
                  <option key={c.id} value={c.id}>
                    {c.name}
                    {c.gate_required ? " (portaria)" : ""}
                  </option>
                ))}
              </select>
              {cameras.length === 0 && <small>Cadastre a câmera primeiro, em Câmeras, para poder vincular o operador.</small>}
            </label>
          )}
          <div className="actions">
            <button type="submit" className="primary" disabled={salvando}>
              {salvando ? "Criando…" : "Criar conta"}
            </button>
          </div>
        </form>
      </Panel>
      )}

      {aba === "gerenciar" && (
      <Panel id="panel-accounts" title="Contas existentes" description={`${usuarios.length} conta(s).`}>
        <div className="account-list">
          {usuarios.map((u) => (
            <div key={u.id} className={`account-row${u.active ? "" : " inactive"}`}>
              <div>
                <strong>{u.name}</strong>
                <div className="t-secondary">
                  {u.email} · {PAPEL[u.role] ?? u.role}
                  {u.role === "operator" ? ` · ${nomeDaCamera(u.camera_id)}` : ""}
                  {u.active ? "" : " · desativada"}
                </div>
              </div>
              {u.id !== eu?.id && (
                <div className="actions">
                  <button type="button" className="ghost small" onClick={() => trocarSenha(u)}>
                    Trocar senha
                  </button>
                  <button
                    type="button"
                    className="ghost small"
                    onClick={() => acao(patchUser(u.id, { active: !u.active }), u.active ? "Conta desativada." : "Conta reativada.")}
                  >
                    {u.active ? "Desativar" : "Reativar"}
                  </button>
                  <button
                    type="button"
                    className="ghost small"
                    onClick={() => {
                      if (window.confirm(`Apagar definitivamente a conta de ${u.email}?`)) acao(deleteUser(u.id), "Conta apagada.");
                    }}
                  >
                    Apagar
                  </button>
                </div>
              )}
            </div>
          ))}
        </div>
      </Panel>
      )}
    </div>
  );
}
