import { QueryClientProvider } from "@tanstack/react-query";
import React from "react";
import ReactDOM from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import App from "./App";
import { AuthProvider } from "./auth/AuthContext";
import { queryClient } from "./queryClient";
import { VaultProvider } from "./vault/VaultContext";
import "./index.css";

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <QueryClientProvider client={queryClient}>
      <AuthProvider>
        <VaultProvider>
          <BrowserRouter>
            <App />
          </BrowserRouter>
        </VaultProvider>
      </AuthProvider>
    </QueryClientProvider>
  </React.StrictMode>,
);
