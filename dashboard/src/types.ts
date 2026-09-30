export type ReviewStatus =
  | "queued"
  | "running"
  | "awaiting_human"
  | "completed"
  | "blocked"
  | "cancelled";
export type Decision = "approve" | "revise" | "done" | "blocked";
export interface Session {
  id: string;
  title: string;
  cwd: string;
  updated: number;
  status: string;
  subagent: boolean;
  approval_mode: "auto" | "manual";
  review_status?: ReviewStatus;
  error?: string;
  events?: unknown[];
  commands?: Command[];
}
export interface Command {
  id: string;
  kind: string;
  text: string;
  status:
    "pending" | "dispatching" | "accepted" | "consumed" | "failed" | "unknown";
  created: number;
  detail: string;
}
export interface Result {
  decision: Decision;
  summary: string;
  instruction: string;
  checks: string[];
  issues: string[];
  pause_proof?: {
    pause_verified: boolean;
    observation?: boolean;
    [key: string]: unknown;
  };
}
export interface Review {
  id: string;
  session_id: string;
  packet: { phase: string; summary: string; cwd: string; scope?: string[] };
  mode: string;
  status: ReviewStatus;
  manual: number;
  version: number;
  created: number;
  updated: number;
  deadline: number;
  result?: Result;
  suggestion?: Result;
  human?: Result;
  logs?: Record<string, string>;
  parent_id?: string;
  delivered?: boolean;
}
export interface Connector {
  online: boolean;
  home: string | null;
  home_matches: boolean;
  last_seen: number | null;
}
export interface Overview {
  service: {
    running: boolean;
    compatible: boolean;
    port?: number;
    pid?: number;
  };
  connector: Connector;
  sessions: number;
  counts: Partial<Record<ReviewStatus, number>>;
  quota: { used: number; limit: number; day: string };
  errors: { id: string; session_id: string; result: string; updated: number }[];
}
export interface Settings {
  model: string;
  home: string;
  history_dir: string;
  state_dir: string;
  codex_bin: string;
  review_timeout: number;
  max_per_day: number;
  bridge_port: number;
  connector: Connector;
}
export interface Event {
  id: number | string;
  at: string;
  kind: string;
  session_id?: string;
  review_id?: string;
  detail: Record<string, unknown>;
}
