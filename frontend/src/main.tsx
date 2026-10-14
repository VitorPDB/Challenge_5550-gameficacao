import React from "react";
import ReactDOM from "react-dom/client";
import App from "./App";
import { applyTheme, readStoredTheme } from "./store/dashboardStore";
import "./styles/tokens.css";
import "./styles/global.css";
import "./styles/journey.css";

// Antes do primeiro paint do React: sem isso a tela abriria no tema do
// sistema e pularia pro tema salvo um quadro depois. Sem valor salvo, o
// atributo não é marcado e o prefers-color-scheme decide.
applyTheme(readStoredTheme());

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);
