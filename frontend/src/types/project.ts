export interface Project {
  id: string;
  org_id: string;
  name: string;
  description: string;
  base_url: string;
  global_setup: string;
  created_by: string;
  created_at: string;
  updated_at: string;
  test_case_count: number;
}

export interface ProjectListResponse {
  items: Project[];
  total: number;
  page: number;
  page_size: number;
}

export interface CreateProjectRequest {
  name: string;
  description?: string;
  base_url?: string;
  global_setup?: string;
}

export interface UpdateProjectRequest {
  name?: string;
  description?: string;
  base_url?: string;
  global_setup?: string;
}
