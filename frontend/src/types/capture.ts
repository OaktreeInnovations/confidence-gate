export interface CaptureSession {
  session_id: string;
  project_id: string;
  base_url: string;
  title: string;
  started_at: string;
  stopped_at: string | null;
  event_count: number;
  steps: CapturedStep[];
}

export interface CapturedStep {
  step_number: number;
  description: string;
  actions: CapturedAction[];
}

export interface CapturedAction {
  action: string;
  target?: {
    test_id?: string;
    role?: string;
    name?: string;
    label?: string;
    placeholder?: string;
    text?: string;
    css?: string;
  };
  value?: string;
}

export interface CreateSessionResponse {
  session_id: string;
  script_url: string;
}

export interface StopSessionResponse {
  session_id: string;
  step_count: number;
  steps: CapturedStep[];
}

export interface SaveSessionResponse {
  test_case_id: string;
}
