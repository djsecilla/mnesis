import { Navigate, Route, Routes } from "react-router-dom";
import Shell from "./components/Shell";
import ChatPage from "./routes/ChatPage";
import GraphPage from "./routes/GraphPage";
import PageDetail from "./routes/PageDetail";
import PagesList from "./routes/PagesList";

export default function App() {
  return (
    <Routes>
      <Route element={<Shell />}>
        <Route index element={<Navigate to="/pages" replace />} />
        <Route path="/graph" element={<GraphPage />} />
        <Route path="/pages" element={<PagesList />} />
        <Route path="/pages/:id" element={<PageDetail />} />
        <Route path="/chat" element={<ChatPage />} />
        <Route path="*" element={<Navigate to="/pages" replace />} />
      </Route>
    </Routes>
  );
}
