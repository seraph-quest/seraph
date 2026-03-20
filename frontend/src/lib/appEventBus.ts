type EventHandler<T = unknown> = (payload: T) => void;

class AppEventBus {
  private listeners = new Map<string, Set<EventHandler>>();

  on<T = unknown>(event: string, handler: EventHandler<T>): void {
    const next = this.listeners.get(event) ?? new Set<EventHandler>();
    next.add(handler as EventHandler);
    this.listeners.set(event, next);
  }

  off<T = unknown>(event: string, handler: EventHandler<T>): void {
    const next = this.listeners.get(event);
    if (!next) {
      return;
    }
    next.delete(handler as EventHandler);
    if (next.size === 0) {
      this.listeners.delete(event);
    }
  }

  emit<T = unknown>(event: string, payload?: T): void {
    const handlers = Array.from(this.listeners.get(event) ?? []);
    for (const handler of handlers) {
      handler(payload);
    }
  }
}

export const appEventBus = new AppEventBus();
