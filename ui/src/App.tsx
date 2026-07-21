import { lazy } from "react";
import { Navigate, Route, Routes } from "react-router-dom";
import { useAuth } from "./auth/AuthContext";
import Shell from "./components/Shell";

// Lazy-loaded routes — keeps the initial bundle small (cytoscape only loads on /graph).
const GraphPage = lazy(() => import("./routes/GraphPage"));
const PagesList = lazy(() => import("./routes/PagesList"));
const PageDetail = lazy(() => import("./routes/PageDetail"));
const ChatPage = lazy(() => import("./routes/ChatPage"));
const AddPage = lazy(() => import("./routes/AddPage"));
const BatchPage = lazy(() => import("./routes/BatchPage"));
const SourcesPage = lazy(() => import("./routes/SourcesPage"));
const ReviewPage = lazy(() => import("./routes/ReviewPage"));
const AdminUsersPage = lazy(() => import("./routes/AdminUsersPage"));

/** Client route guard for the admin area — UX only (redirect a non-admin who navigates
 * directly). The real control is the server: the R7 /api/admin/* endpoints 403 a non-admin
 * regardless of the client. The role is the server-resolved session role, never a guess. */
function AdminRoute({ children }: { children: React.ReactNode }) {
  const { isAdmin } = useAuth();
  return isAdmin ? <>{children}</> : <Navigate to="/pages" replace />;
}

export default function App() {
  return (
    <Routes>
      <Route element={<Shell />}>
        <Route index element={<Navigate to="/pages" replace />} />
        <Route path="/graph" element={<GraphPage />} />
        <Route path="/pages" element={<PagesList />} />
        <Route path="/pages/:id" element={<PageDetail />} />
        <Route path="/chat" element={<ChatPage />} />
        <Route path="/add" element={<AddPage />} />
        <Route path="/add/batch" element={<BatchPage />} />
        <Route path="/sources" element={<SourcesPage />} />
        <Route path="/review" element={<ReviewPage />} />
        <Route path="/admin/users" element={<AdminRoute><AdminUsersPage /></AdminRoute>} />
        <Route path="*" element={<Navigate to="/pages" replace />} />
      </Route>
    </Routes>
  );
}
