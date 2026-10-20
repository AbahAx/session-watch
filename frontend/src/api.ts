export type Settings = {
  source_type: 'local' | 'panel'; local_snapshot_path: string; monitor_enabled: boolean; delivery_paused: boolean; source_name: string;
  settings_revision?: number;
  panel_url: string; panel_username: string; panel_password?: string; panel_totp?: string;
  panel_password_configured?: boolean; panel_totp_configured?: boolean;
  online_api: 'auto' | 'clients' | 'inbounds'; log_path: string;
  poll_seconds: number; offline_seconds: number; timezone: string; log_timezone: string;
  top_targets: number; retention_days: number; telegram_enabled: boolean; ip_location_enabled: boolean;
  telegram_token?: string; telegram_token_configured?: boolean; telegram_chat_id: string;
}
export type User = { email: string; alias: string; monitored: boolean; state: string | null;
  source_enabled: boolean | null; source_present: boolean | null; source_inbounds: string | null;
  session_id: number | null; started_at: number | null; offline_since: number | null;
  last_ip: string; requests: number; targets: number; last_seen: number | null }
export type Session = { id: number; email: string; alias: string; state: string;
  started_at: number; last_online: number; offline_since: number | null; ended_at: number | null;
  last_activity: number | null; requests: number; targets: number; last_ip: string;
  first_target: string; first_inbound: string }
export type Notification = { id: number; alias: string; email: string; kind: string; text: string;
  created_at: number; status: string; error: string; attempts: number; session_id: number }
export type Snapshot = { version: string; now: number; settings: Settings; admin_username: string;
  health: { panel: string; log: string; inventory: string; last_inventory: number | null; last_log_check: number | null;
    log_detail: string; last_poll: number | null; last_log: number | null; parsed: number; skipped: number; detail: string };
  delivery: {status: string; pending: number; failed: number; oldest: number | null; last_success: number | null; last_error: string};
  users: User[]; sessions: Session[]; notifications: Notification[];
  totals: { requests: number; sessions: number; online: number; pending: number; failures: number };
  histogram: {hour: number; count: number}[]; targets: {value: string; count: number}[];
  diagnostics: {at: number; level: string; message: string}[] }
export type SessionDetail = Omit<Session, 'targets'> & { targets: {value: string; count: number}[];
  ips: {value: string; count: number}[]; online_message: string; offline_message: string }

export async function api<T = {ok: boolean; message?: string}>(url: string, method = 'GET', body?: unknown): Promise<T> {
  const response = await fetch('/api' + url, {
    method, credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'session-watch' },
    body: body === undefined ? undefined : JSON.stringify(body),
  })
  const data = await response.json()
  if (!response.ok) {
    if (response.status === 401 && !url.startsWith('/auth/')) window.dispatchEvent(new Event('session-expired'))
    const detail = data.detail
    throw new Error(Array.isArray(detail) ? detail.map((e: {msg: string}) => e.msg).join('；') : detail || '请求失败')
  }
  return data
}
