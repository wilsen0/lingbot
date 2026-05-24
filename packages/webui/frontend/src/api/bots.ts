import { apiClient } from "./client";

export interface BotInfo {
  id: string;
  platform: string;
  name: string;
  online: boolean;
  last_event_at: number | null;
}

export async function listBots(): Promise<BotInfo[]> {
  const r = await apiClient.get<BotInfo[]>("/bots");
  return r.data;
}

export interface SettingsView {
  host: string;
  port: number;
  root_path: string;
  cors_origins: string[];
  jwt_secret: string; // "***"
  jwt_algorithm: string;
  access_token_ttl_s: number;
  refresh_token_ttl_s: number;
  auth_db_path: string;
  static_dir: string;
  event_buffer_size: number;
  login_rate_per_minute: number;
  write_rate_per_minute: number;
  role: string;
}

export async function getSettings(): Promise<SettingsView> {
  const r = await apiClient.get<SettingsView>("/settings");
  return r.data;
}
