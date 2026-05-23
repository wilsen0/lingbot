import axios, { AxiosError, AxiosHeaders, type AxiosInstance, type InternalAxiosRequestConfig } from "axios";

import { useAuthStore } from "@/store/auth";

const BASE = "/api";

export const apiClient: AxiosInstance = axios.create({
  baseURL: BASE,
  timeout: 15_000,
});

let refreshInflight: Promise<string | null> | null = null;

async function refreshAccessToken(): Promise<string | null> {
  const auth = useAuthStore();
  if (!auth.refreshToken) return null;
  if (refreshInflight) return refreshInflight;
  refreshInflight = (async () => {
    try {
      const r = await axios.post(`${BASE}/auth/refresh`, { refresh: auth.refreshToken });
      const data = r.data as { access: string; refresh: string };
      auth.setTokens(data.access, data.refresh);
      return data.access;
    } catch {
      auth.clear();
      return null;
    } finally {
      refreshInflight = null;
    }
  })();
  return refreshInflight;
}

apiClient.interceptors.request.use((config: InternalAxiosRequestConfig) => {
  const auth = useAuthStore();
  if (auth.accessToken) {
    const headers =
      config.headers instanceof AxiosHeaders ? config.headers : new AxiosHeaders(config.headers);
    headers.set("Authorization", `Bearer ${auth.accessToken}`);
    config.headers = headers;
  }
  return config;
});

apiClient.interceptors.response.use(
  (r) => r,
  async (error: AxiosError) => {
    const original = error.config as (InternalAxiosRequestConfig & { _retry?: boolean }) | undefined;
    if (!original || original._retry) {
      return Promise.reject(error);
    }
    if (error.response?.status === 401 && !original.url?.includes("/auth/")) {
      original._retry = true;
      const fresh = await refreshAccessToken();
      if (fresh) {
        const headers =
          original.headers instanceof AxiosHeaders
            ? original.headers
            : new AxiosHeaders(original.headers);
        headers.set("Authorization", `Bearer ${fresh}`);
        original.headers = headers;
        return apiClient.request(original);
      }
      // Refresh failed: bounce to /login (once). We do a hard redirect
      // instead of vue-router so the redirect works even if the router
      // guard happens to be running.
      if (typeof window !== "undefined" && !window.location.pathname.startsWith("/login")) {
        const next = encodeURIComponent(window.location.pathname + window.location.search);
        window.location.href = `/login?next=${next}`;
      }
    }
    return Promise.reject(error);
  },
);

export interface LoginResponse {
  access: string;
  refresh: string;
  access_expires_at: number;
  refresh_expires_at: number;
}

export interface Profile {
  username: string;
  role: "superadmin" | "bot_admin" | "readonly";
  bots: string[];
}

export async function login(username: string, password: string): Promise<LoginResponse> {
  const r = await apiClient.post<LoginResponse>("/auth/login", { username, password });
  return r.data;
}

export async function getProfile(): Promise<Profile> {
  const r = await apiClient.get<Profile>("/profile");
  return r.data;
}

export async function logout(refresh: string): Promise<void> {
  await apiClient.post("/auth/logout", { refresh });
}

export interface HealthResponse {
  status: string;
  version: string;
  time: string;
  bots: Array<{
    id: string;
    platform: string;
    name: string;
    online: boolean;
    last_event_at: number | null;
  }>;
}

export async function getHealth(): Promise<HealthResponse> {
  const r = await apiClient.get<HealthResponse>("/health");
  return r.data;
}
