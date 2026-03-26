import { getFirebaseAuth } from "@/lib/firebase";

// Empty base — requests go to /api/* which Next.js rewrites to the backend.
// This works both from the user's browser and from Playwright inside Docker.
const API_BASE_URL = "";

const REQUEST_TIMEOUT_MS = 30_000;
const LONG_REQUEST_TIMEOUT_MS = 180_000;

interface ApiResponse<T> {
  data: T;
  status: number;
  error?: string;
}

async function getAuthHeaders(): Promise<Record<string, string>> {
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
  };

  try {
    const auth = getFirebaseAuth();
    if (auth.currentUser) {
      const token = await auth.currentUser.getIdToken();
      headers["Authorization"] = `Bearer ${token}`;
    }
  } catch {
    // Firebase not initialized yet, proceed without token
  }

  return headers;
}

function handle401() {
  // Force redirect to login on unauthorized
  if (typeof window !== "undefined") {
    const auth = getFirebaseAuth();
    auth.signOut().finally(() => {
      window.location.href = "/login";
    });
  }
}

async function request<T>(
  path: string,
  options: RequestInit = {},
  timeoutMs: number = REQUEST_TIMEOUT_MS,
): Promise<ApiResponse<T>> {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), timeoutMs);

  try {
    const response = await fetch(`${API_BASE_URL}${path}`, {
      ...options,
      signal: controller.signal,
    });

    if (response.status === 401) {
      handle401();
      return { data: {} as T, status: 401, error: "Unauthorized" };
    }

    // Handle 204 No Content (e.g., DELETE endpoints)
    if (response.status === 204) {
      return { data: {} as T, status: 204 };
    }

    const data = await response.json();
    return { data, status: response.status };
  } catch (err) {
    if (err instanceof DOMException && err.name === "AbortError") {
      return { data: {} as T, status: 0, error: "Request timed out" };
    }
    return {
      data: {} as T,
      status: 0,
      error: err instanceof Error ? err.message : "Network error",
    };
  } finally {
    clearTimeout(timeout);
  }
}

export async function apiGet<T>(path: string): Promise<ApiResponse<T>> {
  const headers = await getAuthHeaders();
  return request<T>(path, { headers });
}

export async function apiPost<T>(
  path: string,
  body?: unknown,
  options?: { longTimeout?: boolean },
): Promise<ApiResponse<T>> {
  const headers = await getAuthHeaders();
  const timeoutMs = options?.longTimeout ? LONG_REQUEST_TIMEOUT_MS : REQUEST_TIMEOUT_MS;
  return request<T>(path, {
    method: "POST",
    headers,
    body: body ? JSON.stringify(body) : undefined,
  }, timeoutMs);
}

export async function apiPut<T>(
  path: string,
  body?: unknown,
): Promise<ApiResponse<T>> {
  const headers = await getAuthHeaders();
  return request<T>(path, {
    method: "PUT",
    headers,
    body: body ? JSON.stringify(body) : undefined,
  });
}

export async function apiDelete<T>(
  path: string,
): Promise<ApiResponse<T>> {
  const headers = await getAuthHeaders();
  return request<T>(path, {
    method: "DELETE",
    headers,
  });
}
