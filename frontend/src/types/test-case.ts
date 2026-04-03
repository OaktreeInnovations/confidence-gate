export type TestCasePriority = "low" | "medium" | "high" | "critical";
export type TestCaseStatus = "draft" | "active" | "archived";
export type TestType = "ui" | "api";
export type HttpMethod = "GET" | "POST" | "PUT" | "PATCH" | "DELETE";

export interface ApiStepConfig {
  method: HttpMethod;
  endpoint: string;
  headers: Record<string, string>;
  body: string;
  expected_status_code: number | null;
  expected_response: string;
  extract_vars: Record<string, string>;
}

export interface TestStep {
  step_number: number;
  action: string;
  expected: string;
  custom_code?: string;
  api_config?: ApiStepConfig | null;
}

export interface TestCase {
  id: string;
  org_id: string;
  project_id: string | null;
  test_type: TestType;
  base_url: string;
  title: string;
  description: string;
  prerequisites: string;
  steps: TestStep[];
  priority: TestCasePriority;
  status: TestCaseStatus;
  tags: string[];
  test_data: Record<string, string>;
  is_critical: boolean;
  is_informational: boolean;
  version: number;
  created_by: string;
  created_at: string;
  updated_at: string;
  flake_badge?: string | null;
  quality_grade?: string | null;  // A/B/C/D — test quality score (3.2)
}

export interface TestCaseListResponse {
  items: TestCase[];
  total: number;
  page: number;
  page_size: number;
}

export interface CreateTestCaseRequest {
  project_id: string;
  test_type?: TestType;
  base_url?: string;
  title: string;
  description?: string;
  prerequisites?: string;
  steps?: TestStep[];
  priority?: TestCasePriority;
  status?: TestCaseStatus;
  tags?: string[];
  test_data?: Record<string, string>;
}

export interface UpdateTestCaseRequest {
  test_type?: TestType;
  base_url?: string;
  title?: string;
  description?: string;
  prerequisites?: string;
  steps?: TestStep[];
  priority?: TestCasePriority;
  status?: TestCaseStatus;
  tags?: string[];
  test_data?: Record<string, string>;
}

export interface GenerateTestCasesRequest {
  project_id: string;
  user_story: string;
  prd?: string;
  srs?: string;
  existing_test_cases?: string;
  screenshots?: string[];
}

export interface GeneratedTestCase {
  title: string;
  description: string;
  test_type: TestType;
  prerequisites: string;
  steps: TestStep[];
  priority: TestCasePriority;
  tags: string[];
}

export interface GenerateTestCasesResponse {
  test_cases: GeneratedTestCase[];
  warnings: string[];
}
