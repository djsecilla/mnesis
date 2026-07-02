import { API_BASE, authHeaders } from "./client";
import type { ChatDone } from "./types";

export interface ChatHistoryItem {
  role: "user" | "assistant";
  text: string;
}

export class ChatError extends Error {
  status?: number;
  constructor(message: string, status?: number) {
    super(message);
    this.name = "ChatError";
    this.status = status;
  }
}

export interface ChatHandlers {
  onToken?: (token: string) => void;
  onDone?: (done: ChatDone) => void;
  onError?: (err: unknown) => void;
}

/**
 * Stream a grounded chat answer from POST /api/chat over SSE, using fetch +
 * ReadableStream (so the answer renders token-by-token, no buffering).
 */
export async function streamChat(
  message: string,
  history: ChatHistoryItem[],
  handlers: ChatHandlers,
): Promise<void> {
  try {
    const res = await fetch(`${API_BASE}/chat`, {
      method: "POST",
      credentials: "include",
      headers: authHeaders({ "Content-Type": "application/json", Accept: "text/event-stream" }),
      body: JSON.stringify({ message, history }),
    });
    if (!res.ok || !res.body) {
      throw new ChatError(`chat failed: ${res.status} ${res.statusText}`, res.status);
    }

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buf = "";

    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buf = (buf + decoder.decode(value, { stream: true })).replace(/\r\n/g, "\n");

      let sep: number;
      while ((sep = buf.indexOf("\n\n")) >= 0) {
        const block = buf.slice(0, sep);
        buf = buf.slice(sep + 2);
        dispatch(block, handlers);
      }
    }
    if (buf.trim()) dispatch(buf.replace(/\r\n/g, "\n"), handlers);
  } catch (err) {
    handlers.onError?.(err);
  }
}

function dispatch(block: string, handlers: ChatHandlers): void {
  let event = "message";
  const dataLines: string[] = [];
  for (const line of block.split("\n")) {
    if (line.startsWith("event:")) event = line.slice(6).trim();
    else if (line.startsWith("data:")) dataLines.push(line.slice(5).replace(/^ /, ""));
  }
  const data = dataLines.join("\n");
  if (event === "token") {
    handlers.onToken?.(data);
  } else if (event === "done") {
    try {
      handlers.onDone?.(JSON.parse(data) as ChatDone);
    } catch (err) {
      handlers.onError?.(err);
    }
  }
}
