/** cellblock TypeScript SDK. Uses global fetch, so it runs on Node >= 22.18 with no dependencies. */

export class CellblockError extends Error {
  readonly status: number;
  constructor(status: number, message: string) {
    super(`HTTP ${status}: ${message}`);
    this.name = "CellblockError";
    this.status = status;
  }
}

export interface ExecResult {
  execId?: string;
  stdout: string;
  stderr: string;
  exitCode: number | null;
  timedOut: boolean;
}

export interface StreamHandlers {
  onStdout?: (chunk: string) => void;
  onStderr?: (chunk: string) => void;
}

export interface ExecOptions extends StreamHandlers {
  timeout?: number;
}

export interface CreateOptions {
  image?: string;
  snapshotId?: string;
  cpu?: number;
  memoryMb?: number;
  pids?: number;
  network?: boolean;
  workspaceMb?: number;
  ttlSeconds?: number;
}

export interface ClientOptions {
  baseUrl?: string;
  apiKey?: string;
}

type SseEvent = { event: string; data: Record<string, unknown> };

function envOr(value: string | undefined, name: string): string {
  const resolved = value ?? process.env[name];
  if (!resolved) throw new Error(`set ${name} or pass it explicitly`);
  return resolved;
}

function parseBlock(block: string): SseEvent | null {
  let event = "message";
  const data: string[] = [];
  for (const line of block.split("\n")) {
    if (line.startsWith("event:")) event = line.slice(6).trim();
    else if (line.startsWith("data:")) data.push(line.slice(5).trimStart());
  }
  return data.length ? { event, data: JSON.parse(data.join("\n")) } : null;
}

async function* parseSse(body: ReadableStream<Uint8Array>): AsyncGenerator<SseEvent> {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let end: number;
    while ((end = buffer.indexOf("\n\n")) !== -1) {
      const parsed = parseBlock(buffer.slice(0, end));
      buffer = buffer.slice(end + 2);
      if (parsed) yield parsed;
    }
  }
}

export class Cellblock {
  private readonly baseUrl: string;
  private readonly apiKey: string;

  constructor(options: ClientOptions = {}) {
    this.baseUrl = (options.baseUrl ?? process.env.CELLBLOCK_URL ?? "http://127.0.0.1:4300").replace(/\/$/, "");
    this.apiKey = envOr(options.apiKey, "CELLBLOCK_API_KEY");
  }

  async send(method: string, path: string, init: { body?: unknown; raw?: Uint8Array } = {}): Promise<Response> {
    const headers: Record<string, string> = { Authorization: `Bearer ${this.apiKey}` };
    let body: BodyInit | undefined;
    if (init.raw) {
      headers["Content-Type"] = "application/octet-stream";
      body = init.raw as BodyInit;
    } else if (init.body !== undefined) {
      headers["Content-Type"] = "application/json";
      body = JSON.stringify(init.body);
    }
    const response = await fetch(this.baseUrl + path, { method, headers, body });
    if (response.status >= 400) {
      const text = await response.text();
      let message = text;
      try {
        message = JSON.parse(text).error ?? text;
      } catch {
        // keep raw text
      }
      throw new CellblockError(response.status, message);
    }
    return response;
  }

  async json<T = any>(method: string, path: string, body?: unknown): Promise<T> {
    return (await this.send(method, path, { body })).json() as Promise<T>;
  }

  /** Yields SSE events from a POST endpoint. */
  async *stream(path: string, body: unknown): AsyncGenerator<SseEvent> {
    const response = await this.send("POST", path, { body });
    if (!response.body) throw new Error("server sent no stream body");
    yield* parseSse(response.body);
  }

  async raw(method: string, path: string, init: { body?: Uint8Array } = {}): Promise<Uint8Array> {
    return new Uint8Array(await (await this.send(method, path, { raw: init.body })).arrayBuffer());
  }
}

export class Files {
  private readonly sandbox: Sandbox;

  constructor(sandbox: Sandbox) {
    this.sandbox = sandbox;
  }

  async write(path: string, data: string | Uint8Array): Promise<void> {
    const bytes = typeof data === "string" ? new TextEncoder().encode(data) : data;
    await this.sandbox.api.raw("PUT", `${this.sandbox.path}/files/content?path=${encodeURIComponent(path)}`, { body: bytes });
  }

  async read(path: string): Promise<Uint8Array> {
    return this.sandbox.api.raw("GET", `${this.sandbox.path}/files/content?path=${encodeURIComponent(path)}`);
  }

  async readText(path: string): Promise<string> {
    return new TextDecoder().decode(await this.read(path));
  }

  async list(path = "/workspace"): Promise<Array<{ name: string; type: string; size: number }>> {
    return this.sandbox.api.json("GET", `${this.sandbox.path}/files?path=${encodeURIComponent(path)}`);
  }
}

export class Sandbox {
  readonly files: Files;
  readonly api: Cellblock;
  private info: Record<string, any>;

  private constructor(api: Cellblock, info: Record<string, any>) {
    this.api = api;
    this.info = info;
    this.files = new Files(this);
  }

  /** Create a sandbox. It is owned by the caller until `delete()`. */
  static async create(options: CreateOptions = {}, client: Cellblock = new Cellblock()): Promise<Sandbox> {
    const body = {
      image: options.image, snapshot_id: options.snapshotId, cpu: options.cpu, memory_mb: options.memoryMb,
      pids: options.pids, network: options.network, workspace_mb: options.workspaceMb, ttl_seconds: options.ttlSeconds,
    };
    return new Sandbox(client, await client.json("POST", "/v1/sandboxes", body));
  }

  static async connect(sandboxId: string, client: Cellblock = new Cellblock()): Promise<Sandbox> {
    return new Sandbox(client, await client.json("GET", `/v1/sandboxes/${sandboxId}`));
  }

  get id(): string {
    return this.info.id;
  }

  get status(): string {
    return this.info.status;
  }

  get path(): string {
    return `/v1/sandboxes/${this.id}`;
  }

  /** Run a shell string or an argv array. Pass handlers to stream output as it arrives. */
  async exec(command: string | string[], options: ExecOptions = {}): Promise<ExecResult> {
    const timeout = options.timeout ?? 30;
    const body = Array.isArray(command) ? { argv: command, timeout_s: timeout } : { command, timeout_s: timeout };
    return this.runStreamOrJson(`${this.path}/exec`, body, options);
  }

  async runCode(code: string, options: ExecOptions & { language?: "python" | "node" } = {}): Promise<ExecResult> {
    const body = { language: options.language ?? "python", code, timeout_s: options.timeout ?? 30 };
    return this.runStreamOrJson(`${this.path}/code`, body, options);
  }

  private async runStreamOrJson(path: string, body: Record<string, unknown>, handlers: StreamHandlers): Promise<ExecResult> {
    if (!handlers.onStdout && !handlers.onStderr) {
      const r = await this.api.json("POST", path, body);
      return { execId: r.exec_id, stdout: r.stdout, stderr: r.stderr, exitCode: r.exit_code, timedOut: r.timed_out };
    }
    const result: ExecResult = { stdout: "", stderr: "", exitCode: null, timedOut: false };
    for await (const { event, data } of this.api.stream(`${path}/stream`, body)) {
      if (event === "started") result.execId = data.exec_id as string;
      else if (event === "stdout" || event === "stderr") {
        const chunk = data.data as string;
        result[event] += chunk;
        (event === "stdout" ? handlers.onStdout : handlers.onStderr)?.(chunk);
      } else if (event === "exit") {
        result.exitCode = data.exit_code as number | null;
        result.timedOut = data.timed_out as boolean;
      }
    }
    return result;
  }

  async killExec(execId: string): Promise<void> {
    await this.api.json("POST", `${this.path}/execs/${execId}/kill`);
  }

  async execs(): Promise<any[]> {
    return this.api.json("GET", `${this.path}/execs`);
  }

  async pause(): Promise<void> {
    this.info = await this.api.json("POST", `${this.path}/pause`);
  }

  async resume(): Promise<void> {
    this.info = await this.api.json("POST", `${this.path}/resume`);
  }

  /** Save /workspace as a template. Pass its `id` as `snapshotId` to `Sandbox.create`. */
  async snapshot(name: string): Promise<{ id: string; name: string }> {
    return this.api.json("POST", `${this.path}/snapshots`, { name });
  }

  async delete(): Promise<void> {
    await this.api.send("DELETE", this.path).catch((err) => {
      if (!(err instanceof CellblockError && err.status === 404)) throw err;
    });
  }
}
