export interface PaperclipAgent {
  id: string;
  name: string;
  role: string;
}

export interface PaperclipTask {
  id: string;
  title: string;
  status: string;
}

const API_URL = process.env.PAPERCLIP_API_URL;
const API_TOKEN = process.env.PAPERCLIP_API_TOKEN;

async function request<T>(path: string): Promise<T> {
  if (!API_URL || !API_TOKEN) {
    throw new Error("Paperclip API not configured.");
  }
  const res = await fetch(`${API_URL}${path}`, {
    headers: {
      Authorization: `Bearer ${API_TOKEN}`,
      "Content-Type": "application/json",
    },
  });
  if (!res.ok) {
    throw new Error(`Paperclip API error: ${res.status}`);
  }
  return (await res.json()) as T;
}

export const paperclipAdapter = {
  async fetchAgents(): Promise<PaperclipAgent[]> {
    if (!API_URL || !API_TOKEN) {
      return [
        { id: "ceo", name: "CEO", role: "Executive" },
        { id: "cto", name: "CTO", role: "Engineering" },
        { id: "devops", name: "DevOps", role: "Infrastructure" },
      ];
    }
    return request<PaperclipAgent[]>("/agents");
  },

  async fetchTasks(): Promise<PaperclipTask[]> {
    if (!API_URL || !API_TOKEN) {
      return [
        { id: "task-1", title: "Bootstrap architecture", status: "open" },
      ];
    }
    return request<PaperclipTask[]>("/tasks");
  },

  async publishEvent(event: Record<string, unknown>) {
    if (!API_URL || !API_TOKEN) {
      return { ok: true, mocked: true, event };
    }
    const res = await fetch(`${API_URL}/events`, {
      method: "POST",
      headers: {
        Authorization: `Bearer ${API_TOKEN}`,
        "Content-Type": "application/json",
      },
      body: JSON.stringify(event),
    });
    if (!res.ok) {
      throw new Error(`Paperclip API error: ${res.status}`);
    }
    return res.json();
  },
};
