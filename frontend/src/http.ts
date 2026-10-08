// The one fetch wrapper every API client uses. Same-origin: nginx (docker) or the Vite dev proxy forwards
// each path to its service. The session cookie goes along by itself (it's HttpOnly: this code never sees it).

/** Error carrying the server's explanation (FastAPI puts it in `detail`). */
export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

// A 401 on any API call means the session ended (expired, signed out elsewhere, account disabled).
// AuthProvider listens and sends the user to the sign-in page.
const unauthorizedListeners = new Set<() => void>();
export function onUnauthorized(fn: () => void): () => void {
  unauthorizedListeners.add(fn);
  return () => unauthorizedListeners.delete(fn);
}

/** nginx's 502/503/504: the service behind it is down, restarting, or not run on this machine at all (a server may
 *  run without backtest-service, see docker-compose.server.yml). */
function unreachable(url: string, status: number): string {
  if (/^\/(runs|scans|ws)\b/.test(url))
    return `Backtests aren't available here (${status}): backtest-service isn't running on this machine. Run them on your Mac, or start the backtest profile on the server.`;
  return `The service didn't answer (${status}): it's restarting, or doesn't run on this machine. Try again in a minute.`;
}

export async function request<T>(method: string, url: string, body?: unknown, { signedOutOk = false } = {}): Promise<T> {
  const res = await fetch(url, {
    method,
    headers: body ? { "Content-Type": "application/json" } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) {
    if (res.status === 401 && !signedOutOk) unauthorizedListeners.forEach((fn) => fn());
    let msg = `${res.status} ${res.statusText}`;
    try {
      const data = await res.json();
      // 422 validation errors come as a list of {loc, msg}; everything else as a string
      msg = Array.isArray(data.detail)
        ? data.detail.map((d: { loc: string[]; msg: string }) => `${d.loc[d.loc.length - 1]}: ${d.msg}`).join("; ")
        : data.detail ?? msg;
    } catch {
      // A non-JSON error comes from nginx, not the service: the service didn't answer at all
      if (res.status >= 502 && res.status <= 504) msg = unreachable(url, res.status);
    }
    throw new ApiError(res.status, msg);
  }
  // 204 No Content (sign-out, password change, delete) has no body to parse
  return (res.status === 204 ? undefined : res.json()) as Promise<T>;
}
