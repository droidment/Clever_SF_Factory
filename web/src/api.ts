let accessToken = '';
export function setToken(value: string) {
  accessToken = value;
}
export async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch(path, {
    ...init,
    headers: {
      'Content-Type': 'application/json',
      ...(accessToken ? { Authorization: `Bearer ${accessToken}` } : {}),
      ...init.headers,
    },
  });
  if (!response.ok) {
    const body = await response.json().catch(() => ({ detail: response.statusText }));
    const detail = Array.isArray(body.detail)
      ? body.detail.map((e: { msg: string }) => e.msg).join('; ')
      : body.detail;
    throw new Error(detail || `Request failed (${response.status})`);
  }
  return response.json();
}
export function connectEvents(onEvent: () => void, onState: (connected: boolean) => void) {
  const controller = new AbortController();
  let cursor = 0;
  let timer: ReturnType<typeof setTimeout>;
  async function stream() {
    try {
      const response = await fetch(`/api/events?after=${cursor}`, {
        signal: controller.signal,
        headers: accessToken ? { Authorization: `Bearer ${accessToken}` } : {},
      });
      if (!response.ok || !response.body) throw new Error('Event stream unavailable');
      onState(true);
      const reader = response.body.getReader(),
        decoder = new TextDecoder();
      let buffer = '';
      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        let end;
        while ((end = buffer.indexOf('\n\n')) >= 0) {
          const frame = buffer.slice(0, end);
          buffer = buffer.slice(end + 2);
          const id = frame.match(/^id: (\d+)/m);
          if (id) {
            cursor = Number(id[1]);
            onEvent();
          }
        }
      }
    } catch {
      if (controller.signal.aborted) return;
    }
    onState(false);
    if (!controller.signal.aborted) timer = setTimeout(stream, 2000);
  }
  void stream();
  return () => {
    controller.abort();
    clearTimeout(timer);
  };
}
