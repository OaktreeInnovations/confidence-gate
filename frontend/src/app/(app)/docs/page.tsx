"use client";

import { useState } from "react";
import { BookOpen, Code2, Database, ChevronRight, Copy, Check } from "lucide-react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";

function CopyButton({ text }: { text: string }) {
  const [copied, setCopied] = useState(false);
  async function copy() {
    await navigator.clipboard.writeText(text);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  }
  return (
    <button
      onClick={copy}
      className="ml-auto flex items-center gap-1 rounded px-2 py-0.5 text-[10px] text-muted-foreground hover:text-foreground hover:bg-muted transition-colors"
    >
      {copied ? <Check className="h-3 w-3 text-green-500" /> : <Copy className="h-3 w-3" />}
      {copied ? "Copied" : "Copy"}
    </button>
  );
}

function CodeBlock({ code, lang = "json" }: { code: string; lang?: string }) {
  return (
    <div className="rounded-lg border bg-muted/40 overflow-hidden">
      <div className="flex items-center border-b px-3 py-1.5 bg-muted/60">
        <span className="text-[10px] text-muted-foreground font-mono">{lang}</span>
        <CopyButton text={code} />
      </div>
      <pre className="p-4 text-xs font-mono leading-relaxed overflow-x-auto whitespace-pre">{code}</pre>
    </div>
  );
}

function Section({ id, title, children }: { id: string; title: string; children: React.ReactNode }) {
  return (
    <section id={id} className="space-y-4 scroll-mt-6">
      <h2 className="text-lg font-semibold border-b pb-2">{title}</h2>
      {children}
    </section>
  );
}

function Param({ name, type, required, children }: { name: string; type: string; required?: boolean; children: React.ReactNode }) {
  return (
    <div className="flex gap-3 py-2 border-b last:border-0">
      <div className="w-40 shrink-0">
        <code className="text-xs font-mono text-foreground">{name}</code>
        {required && <span className="ml-1 text-[9px] text-destructive font-medium">required</span>}
      </div>
      <div className="w-24 shrink-0">
        <span className="text-[10px] font-mono text-muted-foreground">{type}</span>
      </div>
      <div className="text-xs text-muted-foreground flex-1">{children}</div>
    </div>
  );
}

const TOKEN_EXAMPLES = `// Static test data (defined in test case)
\${email}         → your defined value
\${password}      → your defined value
\${url}           → your defined value

// Dynamic built-in tokens (generated fresh each run)
\${uuid}          → a3f2c1d4-9b8e-4f2a-b1c3-d4e5f6a7b8c9
\${timestamp}     → 1743040512
\${date}          → 2026-03-27
\${random_int}    → 847291
\${random_string} → k4m9xz2w
\${random_email}  → test_k4m9xz@example.com
\${random_name}   → User_k4m9xz`;

const INTENT_FULL = `{
  "step_number": 3,
  "description": "Enter password into the password field",
  "actions": [
    {
      "action": "input",
      "target": {
        "css": "input[type='password']"
      },
      "value": "\${password}"
    }
  ]
}`;

const INTENT_NAVIGATE = `{
  "step_number": 1,
  "description": "Navigate to the login page",
  "actions": [
    {
      "action": "navigate",
      "value": "\${url}"
    }
  ]
}`;

const INTENT_NAVIGATE_WAIT = `{
  "step_number": 5,
  "description": "Go to departments page and wait for table",
  "actions": [
    {
      "action": "navigate",
      "value": "https://staging.example.com/departments"
    },
    {
      "action": "wait_for_text",
      "target": { "css": "body" },
      "value": "Departments"
    }
  ]
}`;

const INTENT_CLICK_ROLE = `{
  "step_number": 4,
  "description": "Click the Sign In button",
  "actions": [
    {
      "action": "click",
      "target": {
        "role": "button",
        "name": "Sign In"
      }
    }
  ]
}`;

const INTENT_MULTI = `{
  "step_number": 6,
  "description": "Select a dropdown option",
  "actions": [
    {
      "action": "click",
      "target": { "css": "#department-select" }
    },
    {
      "action": "select",
      "target": { "css": "#department-select" },
      "value": "Engineering"
    }
  ]
}`;

const INTENT_ASSERT = `{
  "step_number": 7,
  "description": "Assert the success message is visible",
  "actions": [
    {
      "action": "assert_visible",
      "target": {
        "text": "Successfully saved"
      }
    },
    {
      "action": "assert_value",
      "target": { "css": "#email-field" },
      "value": "\${email}"
    }
  ]
}`;

const INTENT_SIGNUP = `{
  "step_number": 2,
  "description": "Fill registration form with unique data",
  "actions": [
    {
      "action": "input",
      "target": { "label": "Email" },
      "value": "\${random_email}"
    },
    {
      "action": "input",
      "target": { "label": "Username" },
      "value": "\${random_name}"
    },
    {
      "action": "input",
      "target": { "label": "Reference Code" },
      "value": "REF-\${random_int}"
    }
  ]
}`;

const sections = [
  { id: "test-data", label: "Test Data & Tokens" },
  { id: "intent-schema", label: "Intent Schema" },
  { id: "action-types", label: "Action Types" },
  { id: "target-descriptor", label: "Target Descriptor" },
  { id: "examples", label: "Examples" },
];

export default function DocsPage() {
  const [activeSection, setActiveSection] = useState("test-data");

  return (
    <div className="flex gap-6 p-6 min-h-screen">
      {/* Sidebar TOC */}
      <aside className="w-48 shrink-0">
        <div className="sticky top-6 space-y-1">
          <p className="px-2 pb-2 text-xs font-semibold text-muted-foreground uppercase tracking-wider">Contents</p>
          {sections.map((s) => (
            <a
              key={s.id}
              href={`#${s.id}`}
              onClick={() => setActiveSection(s.id)}
              className={`flex items-center gap-1.5 rounded px-2 py-1.5 text-xs transition-colors ${
                activeSection === s.id
                  ? "bg-accent text-accent-foreground font-medium"
                  : "text-muted-foreground hover:text-foreground hover:bg-muted"
              }`}
            >
              <ChevronRight className="h-3 w-3 shrink-0" />
              {s.label}
            </a>
          ))}
        </div>
      </aside>

      {/* Content */}
      <div className="flex-1 max-w-3xl space-y-10">
        <div>
          <div className="flex items-center gap-2 mb-1">
            <BookOpen className="h-5 w-5 text-muted-foreground" />
            <h1 className="text-2xl font-bold">Reference</h1>
          </div>
          <p className="text-sm text-muted-foreground">
            Manual intent coding and test data substitution reference for Confidence Gate.
          </p>
        </div>

        {/* ── Test Data & Tokens ── */}
        <Section id="test-data" title="Test Data & Tokens">
          <p className="text-sm text-muted-foreground">
            Test data key-value pairs are defined on each test case. Reference them in step actions
            and custom intents using <code className="text-xs bg-muted px-1 py-0.5 rounded">${"{key}"}</code> syntax.
            Built-in tokens are generated fresh on every run — no setup needed.
          </p>

          <Card>
            <CardHeader className="pb-2">
              <CardTitle className="text-sm flex items-center gap-2">
                <Database className="h-4 w-4" />
                All available tokens
              </CardTitle>
            </CardHeader>
            <CardContent>
              <CodeBlock code={TOKEN_EXAMPLES} lang="tokens" />
            </CardContent>
          </Card>

          <div className="rounded-lg border divide-y text-sm">
            <div className="grid grid-cols-3 px-3 py-2 bg-muted/40 text-xs font-semibold text-muted-foreground">
              <span>Token</span><span>Type</span><span>Output</span>
            </div>
            {[
              ["${key}", "Static", "Value from test data field named key"],
              ["${uuid}", "Dynamic", "Random UUID v4 e.g. a3f2c1d4-9b8e-..."],
              ["${timestamp}", "Dynamic", "Unix timestamp e.g. 1743040512"],
              ["${date}", "Dynamic", "Today's date in ISO format e.g. 2026-03-27"],
              ["${random_int}", "Dynamic", "Random 6-digit integer e.g. 847291"],
              ["${random_string}", "Dynamic", "Random 8-char alphanumeric e.g. k4m9xz2w"],
              ["${random_email}", "Dynamic", "Unique email e.g. test_k4m9xz@example.com"],
              ["${random_name}", "Dynamic", "Unique username e.g. User_k4m9xz"],
            ].map(([token, type, desc]) => (
              <div key={token} className="grid grid-cols-3 px-3 py-2 text-xs">
                <code className="font-mono text-foreground">{token}</code>
                <span className={type === "Dynamic" ? "text-blue-600" : "text-muted-foreground"}>{type}</span>
                <span className="text-muted-foreground">{desc}</span>
              </div>
            ))}
          </div>

          <div className="rounded-lg bg-amber-50 border border-amber-200 px-4 py-3 text-xs text-amber-800 space-y-1">
            <p className="font-semibold">Same token, same value within a step</p>
            <p>If you use <code className="font-mono">${"{random_email}"}</code> twice in the same step, both resolve to the same generated value. Each new step generates fresh values.</p>
          </div>
        </Section>

        {/* ── Intent Schema ── */}
        <Section id="intent-schema" title="Intent Schema">
          <p className="text-sm text-muted-foreground">
            A <strong>StepIntent</strong> is a JSON object that tells the executor exactly what to do —
            bypassing AI generation entirely. Set it in the <em>Custom Code</em> field of any test step.
          </p>

          <Card>
            <CardHeader className="pb-2">
              <CardTitle className="text-sm flex items-center gap-2">
                <Code2 className="h-4 w-4" />
                StepIntent structure
              </CardTitle>
            </CardHeader>
            <CardContent className="space-y-3">
              <CodeBlock code={INTENT_FULL} />
              <div className="rounded-lg border divide-y">
                <Param name="step_number" type="integer" required>The step number this intent targets.</Param>
                <Param name="description" type="string">Human-readable description (used in logs).</Param>
                <Param name="actions" type="ActionIntent[]" required>One or more actions to execute in sequence.</Param>
              </div>
            </CardContent>
          </Card>
        </Section>

        {/* ── Action Types ── */}
        <Section id="action-types" title="Action Types">
          <p className="text-sm text-muted-foreground">
            Each action in the <code className="text-xs bg-muted px-1 py-0.5 rounded">actions</code> array
            has an <code className="text-xs bg-muted px-1 py-0.5 rounded">action</code> field picking from these types:
          </p>

          <div className="rounded-lg border divide-y text-sm">
            <div className="grid grid-cols-4 px-3 py-2 bg-muted/40 text-xs font-semibold text-muted-foreground">
              <span>Action</span><span>Target</span><span>Value</span><span>Description</span>
            </div>
            {[
              ["navigate", "—", "URL string", "Navigate to a URL (absolute or relative)"],
              ["input", "required", "Text to type", "Type text into a field. Clears field first."],
              ["click", "required", "—", "Click an element."],
              ["select", "required", "Option text or value", "Select a dropdown option."],
              ["check", "required", "—", "Check a checkbox."],
              ["uncheck", "required", "—", "Uncheck a checkbox."],
              ["wait_for_text", "required", "Text to wait for", "Wait until text appears on page."],
              ["wait_for_navigation", "—", "—", "Wait for page navigation to complete."],
              ["assert_visible", "required", "—", "Assert element is visible on page."],
              ["assert_text", "required", "Expected text", "Assert element contains text."],
              ["assert_value", "required", "Expected value", "Assert input field has value."],
              ["extract_value", "required", "Variable name", "Extract element value into a variable."],
            ].map(([action, target, value, desc]) => (
              <div key={action} className="grid grid-cols-4 px-3 py-2 text-xs">
                <code className="font-mono text-foreground font-semibold">{action}</code>
                <span className={target === "required" ? "text-destructive" : "text-muted-foreground"}>{target}</span>
                <span className="text-muted-foreground">{value}</span>
                <span className="text-muted-foreground">{desc}</span>
              </div>
            ))}
          </div>
        </Section>

        {/* ── Target Descriptor ── */}
        <Section id="target-descriptor" title="Target Descriptor">
          <p className="text-sm text-muted-foreground">
            The <code className="text-xs bg-muted px-1 py-0.5 rounded">target</code> object describes how to find
            an element. The resolver tries fields in priority order — provide the most specific one you know.
          </p>

          <div className="rounded-lg border divide-y">
            <div className="grid grid-cols-3 px-3 py-2 bg-muted/40 text-xs font-semibold text-muted-foreground">
              <span>Field</span><span>Priority</span><span>Example</span>
            </div>
            {[
              ["test_id", "1 — highest", '{"test_id": "submit-btn"}'],
              ["role + name", "2", '{"role": "button", "name": "Sign In"}'],
              ["label", "3", '{"label": "Password"}'],
              ["placeholder", "4", '{"placeholder": "Enter email"}'],
              ["text", "5", '{"text": "Forgot password?"}'],
              ["css", "6 — fallback", '{"css": "input[type=\'password\']"}'],
            ].map(([field, priority, example]) => (
              <div key={field} className="grid grid-cols-3 px-3 py-2 text-xs">
                <code className="font-mono text-foreground">{field}</code>
                <span className="text-muted-foreground">{priority}</span>
                <code className="font-mono text-muted-foreground">{example}</code>
              </div>
            ))}
          </div>

          <div className="space-y-1 text-sm">
            <p className="font-medium text-sm">Advanced scoping</p>
            <div className="rounded-lg border divide-y">
              <Param name="shadow_host" type="string">CSS selector for shadow DOM host. Scopes search inside shadow root.</Param>
              <Param name="iframe" type="string">Frame name, URL pattern, or CSS selector for an iframe. Scopes search inside iframe.</Param>
            </div>
          </div>
        </Section>

        {/* ── Examples ── */}
        <Section id="examples" title="Examples">
          <div className="space-y-6">
            <div>
              <p className="text-sm font-medium mb-2">Navigate to a URL</p>
              <CodeBlock code={INTENT_NAVIGATE} />
            </div>
            <div>
              <p className="text-sm font-medium mb-2">Navigate and wait for page to load (SPA)</p>
              <CodeBlock code={INTENT_NAVIGATE_WAIT} />
            </div>
            <div>
              <p className="text-sm font-medium mb-2">Click by role and name</p>
              <CodeBlock code={INTENT_CLICK_ROLE} />
            </div>
            <div>
              <p className="text-sm font-medium mb-2">Multi-action step (click + select)</p>
              <CodeBlock code={INTENT_MULTI} />
            </div>
            <div>
              <p className="text-sm font-medium mb-2">Assert visible and assert value</p>
              <CodeBlock code={INTENT_ASSERT} />
            </div>
            <div>
              <p className="text-sm font-medium mb-2">Sign-up form with random data</p>
              <CodeBlock code={INTENT_SIGNUP} />
            </div>
          </div>
        </Section>
      </div>
    </div>
  );
}
