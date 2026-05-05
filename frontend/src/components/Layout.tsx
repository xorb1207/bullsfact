import { NavLink, Outlet } from "react-router-dom";

export function Layout() {
  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="brand">
          <span className="brand-dot" />
          dip-alert
        </div>
        <NavLink to="/" end className="nav-link">
          Watchlist
        </NavLink>
        <NavLink to="/alerts" className="nav-link">
          Alerts
        </NavLink>
        <NavLink to="/backtest" className="nav-link">
          Backtest
        </NavLink>
        <div style={{ flex: 1 }} />
        <div style={{ fontSize: 11, color: "var(--text-2)", padding: "12px" }}>
          v0.1.0 · P3 preview
        </div>
      </aside>
      <main className="main">
        <Outlet />
      </main>
    </div>
  );
}
