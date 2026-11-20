/** Serialize OS events. Resuming validates identity, never resumes a turn. */
interface LifecycleOptions {
  admission(open: boolean): void;
  prepare(): Promise<void>;
  stop(): Promise<void>;
  resume(): Promise<void>;
  changed(ready: boolean): void;
  warn(): void;
}

export class RuntimeLifecycle {
  private readonly options: LifecycleOptions;
  private queue: Promise<void> = Promise.resolve();
  private suspended = false;
  private exiting = false;
  private revision = 0;

  constructor(options: LifecycleOptions) { this.options = options; }

  private enqueue(work: () => Promise<void>): Promise<void> {
    this.queue = this.queue.then(work).catch(() => { this.options.warn(); });
    return this.queue;
  }

  suspend(): Promise<void> {
    if (this.suspended || this.exiting) return this.queue;
    this.suspended = true;
    this.revision++;
    this.options.admission(false);
    this.options.changed(false);
    return this.enqueue(async () => {
      try { await this.options.prepare(); }
      catch { await this.options.stop(); }
    });
  }

  resume(): Promise<void> {
    if (!this.suspended || this.exiting) return this.queue;
    this.suspended = false;
    const revision = ++this.revision;
    return this.enqueue(async () => {
      try { await this.options.resume(); }
      finally {
        if (!this.exiting && !this.suspended && revision === this.revision) {
          this.options.admission(true);
          this.options.changed(true);
        }
      }
    });
  }

  shutdown(): Promise<void> {
    if (this.exiting) return this.queue;
    this.exiting = true;
    this.revision++;
    this.options.admission(false);
    this.options.changed(false);
    return this.enqueue(async () => {
      try { await this.options.prepare(); }
      catch { await this.options.stop(); }
    });
  }
}
