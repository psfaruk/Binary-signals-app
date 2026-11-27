import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { AppProvider } from "./stores/AppStore";
import { AppShell } from "./components/layout/AppShell";
import { useRoute } from "./routes";
import { OverviewPage } from "./pages/Overview";
import { TerminalPage } from "./pages/Terminal";
import { HistoryPage } from "./pages/History";
import { AnalyticsPage } from "./pages/Analytics";
import { ModelsPage } from "./pages/Models";
import { SettingsPage } from "./pages/Settings";
import "./styles/tokens.css";
import "./styles/base.css";

function App() {
  const [route] = useRoute();
  return (
    <AppShell>
      {route === "overview"  && <OverviewPage />}
      {route === "terminal"  && <TerminalPage />}
      {route === "history"   && <HistoryPage />}
      {route === "analytics" && <AnalyticsPage />}
      {route === "models"    && <ModelsPage />}
      {route === "settings"  && <SettingsPage />}
    </AppShell>
  );
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <AppProvider>
      <App />
    </AppProvider>
  </StrictMode>,
);
