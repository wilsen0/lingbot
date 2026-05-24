import { apiClient } from "./client";

export interface EventEnvelope {
  seq: number;
  id: string;
  platform: string;
  bot_id: string;
  scope: { kind: string; id: string; platform: string };
  sender: { id: string; platform: string; display_name?: string | null; role?: string };
  time: string;
  kind: string;
  segments: Array<Record<string, unknown> & { kind: string }>;
  text: string;
}

export interface EventPage {
  items: EventEnvelope[];
  next_cursor: number | null;
}

export async function listEvents(params?: {
  bot_id?: string;
  since_seq?: number;
  kind?: string;
  mine?: boolean;
  limit?: number;
}): Promise<EventPage> {
  const r = await apiClient.get<EventPage>("/events", { params });
  return r.data;
}
