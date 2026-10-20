import { NavLink, Navigate, Route, Routes } from "react-router-dom";
import { ProviderBadge } from "./components/ProviderBadge";
import { TokenSettings } from "./components/TokenSettings";
import { ApplicationsPage } from "./pages/ApplicationsPage";
import { IncidentDetailPage } from "./pages/IncidentDetailPage";
import { IncidentsPage } from "./pages/IncidentsPage";
import { RunsPage } from "./pages/RunsPage";

const navItems = [
  { to: "/applications", label: "Applications" },
  { to: "/incidents", label: "Incidents" },
  { to: "/runs", label: "Runs" },
];

function Placeholder({ title, text }: { title: string; text: string }) {
  return (
    <section>
      <h1 className="mb-2 text-xl font-semibold">{title}</h1>
      <p className="text-slate-600">{text}</p>
    </section>
  );
}

function NotFound() {
  return <Placeholder title="Not found" text="This page does not exist." />;
}

export default function App() {
  return (
    <div className="min-h-screen bg-slate-50 text-slate-900">
      <header className="border-b border-slate-200 bg-white">
        <div className="mx-auto flex max-w-6xl flex-wrap items-center gap-4 px-4 py-3">
          <span className="text-lg font-bold">Sentinel AI</span>
          <nav aria-label="Main" className="flex gap-1">
            {navItems.map((item) => (
              <NavLink
                key={item.to}
                to={item.to}
                className={({ isActive }) =>
                  `rounded px-3 py-1 text-sm ${isActive ? "bg-slate-800 text-white" : "text-slate-700 hover:bg-slate-100"}`
                }
              >
                {item.label}
              </NavLink>
            ))}
          </nav>
          <div className="ml-auto flex items-center gap-3">
            <ProviderBadge />
            <TokenSettings />
          </div>
        </div>
      </header>
      <main className="mx-auto max-w-6xl px-4 py-6">
        <Routes>
          <Route path="/" element={<Navigate to="/incidents" replace />} />
          <Route path="/applications" element={<ApplicationsPage />} />
          <Route path="/incidents" element={<IncidentsPage />} />
          <Route path="/incidents/:id" element={<IncidentDetailPage />} />
          <Route path="/runs" element={<RunsPage />} />
          <Route path="*" element={<NotFound />} />
        </Routes>
      </main>
    </div>
  );
}
