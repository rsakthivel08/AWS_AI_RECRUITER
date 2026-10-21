import React, { Component, ErrorInfo, ReactNode, StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { Amplify } from "aws-amplify";
import App from "./App";

interface Props {
  children: ReactNode;
}
interface State {
  hasError: boolean;
  error: Error | null;
}

class ErrorBoundary extends Component<Props, State> {
  public state: State = { hasError: false, error: null };

  public static getDerivedStateFromError(error: Error): State {
    return { hasError: true, error };
  }

  public componentDidCatch(error: Error, errorInfo: ErrorInfo) {
    console.error("Uncaught application error:", error, errorInfo);
  }

  public render() {
    if (this.state.hasError) {
      return (
        <div style={{ padding: 40, background: "#0a0d14", color: "#f2f5fb", minHeight: "100vh", fontFamily: "system-ui, sans-serif" }}>
          <h1 style={{ color: "#ff7a8a" }}>Application Error</h1>
          <p>An unexpected error occurred while rendering the page:</p>
          <pre style={{ background: "rgba(255,255,255,0.06)", padding: 16, borderRadius: 8, overflowX: "auto", color: "#ffcc66" }}>
            {this.state.error?.stack || this.state.error?.message}
          </pre>
          <button
            onClick={() => { localStorage.clear(); sessionStorage.clear(); window.location.reload(); }}
            style={{ marginTop: 16, padding: "10px 20px", background: "#7aa2ff", border: "none", borderRadius: 8, cursor: "pointer", fontWeight: "bold" }}
          >
            Clear Cache & Reload
          </button>
        </div>
      );
    }
    return this.props.children;
  }
}

const COG_USER_POOL_ID = import.meta.env.VITE_USER_POOL_ID ?? "";
const COG_CLIENT_ID = import.meta.env.VITE_CLIENT_ID ?? "";
const COG_DOMAIN = import.meta.env.VITE_COGNITO_DOMAIN ?? "";
const API_URL = import.meta.env.VITE_API_URL ?? "";

try {
  Amplify.configure({
    Auth: {
      Cognito: {
        userPoolId: COG_USER_POOL_ID,
        userPoolClientId: COG_CLIENT_ID,
        loginWith: {
          oauth: {
            domain: COG_DOMAIN,
            scopes: ["email", "openid", "profile"],
            redirectSignIn: [window.location.origin + "/auth/callback"],
            redirectSignOut: [window.location.origin + "/"],
            responseType: "code",
          },
        },
      },
    },
    API: { REST: { api: { endpoint: API_URL, region: "ap-south-1" } } },
  });
} catch (e) {
  console.error("Amplify configure error:", e);
}

const rootEl = document.getElementById("root");
if (rootEl) {
  createRoot(rootEl).render(
    <StrictMode>
      <ErrorBoundary>
        <App />
      </ErrorBoundary>
    </StrictMode>,
  );
}

