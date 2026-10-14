const assert = require("node:assert/strict");
const path = require("node:path");
const { spawnSync } = require("node:child_process");

const root = path.resolve(__dirname, "..");
const build = spawnSync(process.execPath, [
  path.join(root, "frontend/node_modules/typescript/lib/tsc.js"),
  path.join(root, "frontend/src/utils/monitoring.ts"),
  "--rootDir", path.join(root, "frontend/src"),
  "--outDir", path.join(root, ".validation-ui"),
  "--module", "commonjs", "--target", "ES2020", "--skipLibCheck",
], { cwd: root, stdio: "inherit" });
if (build.status !== 0) process.exit(build.status ?? 1);
const { monitoringProblem, ppeChipState } = require(path.join(root, ".validation-ui/utils/monitoring.js"));
const live = {
  running: true, frame_counter: 10, last_error: null,
  video: { state: "live", modo: "ao_vivo" },
  diagnostico: { imagem_desatualizada: false },
  model: { supported_ppe: { helmet: true, mask: false } },
  compliance: { ppe: { helmet: { status: "ok" } } },
};
assert.equal(monitoringProblem(live), null);
assert.equal(ppeChipState(live, "helmet", false), "ok");
assert.equal(ppeChipState(live, "helmet", true), "miss");
assert.equal(ppeChipState(live, "mask", false), "waiting");
assert.equal(ppeChipState({ ...live, compliance: null }, "helmet", false), "waiting");
for (const problem of [
  null,
  { ...live, running: false },
  { ...live, video: { state: "reconnecting" } },
  { ...live, video: { state: "live", modo: "fixture" } },
  { ...live, diagnostico: { imagem_desatualizada: true } },
  { ...live, diagnostico: { fonte_sem_imagem: true } },
  { ...live, last_error: "model failed" },
  { ...live, frame_counter: 0 },
]) {
  assert.ok(monitoringProblem(problem));
  assert.equal(ppeChipState(problem, "helmet", false), "waiting");
}
console.log("Monitoramento: estados de falha e EPIs desconhecidos não aparecem como conformes.");
