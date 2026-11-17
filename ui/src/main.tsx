import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { ChecksPage } from "./ChecksPage";
import "./styles.css";

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <ChecksPage />
  </StrictMode>,
);
