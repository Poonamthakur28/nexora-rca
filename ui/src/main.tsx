import { StrictMode, useState } from "react";
import { createRoot } from "react-dom/client";
import { ChecksPage } from "./ChecksPage";
import { IncidentsPage } from "./IncidentsPage";
import { PipelinePage } from "./PipelinePage";
import "./styles.css";

type Tab = "checks" | "pipeline" | "incidents";

function initialTab(): Tab {
  const h = window.location.hash.replace("#", "");
  return h === "pipeline" || h === "incidents" ? h : "checks";
}

function App() {
  const [tab, setTab] = useState<Tab>(initialTab);
  const [incidentId, setIncidentId] = useState<string | null>(null);

  const go = (t: Tab) => {
    setTab(t);
    window.location.hash = t;
  };

  return (
    <div className="page">
      <nav className="nav">
        <span className="brand">Nexora RCA</span>
        {(["checks", "pipeline", "incidents"] as Tab[]).map((t) => (
          <button key={t} className={`tab ${tab === t ? "active" : ""}`} onClick={() => go(t)}>
            {t[0].toUpperCase() + t.slice(1)}
          </button>
        ))}
      </nav>
      {tab === "checks" && <ChecksPage />}
      {tab === "pipeline" && (
        <PipelinePage
          onOpenIncident={(id) => {
            setIncidentId(id);
            go("incidents");
          }}
        />
      )}
      {tab === "incidents" && <IncidentsPage selectedId={incidentId} onSelect={setIncidentId} />}
    </div>
  );
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
