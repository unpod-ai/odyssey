export class OdysseyAPIError extends Error {
  constructor(
    public readonly status: number,
    public readonly path: string,
    public readonly body: string,
  ) {
    super(`${status} from ${path}: ${body}`);
    this.name = "OdysseyAPIError";
  }
}

export class OdysseyAPINotFoundError extends OdysseyAPIError {
  constructor(path: string, body: string) {
    super(404, path, body);
    this.name = "OdysseyAPINotFoundError";
  }
}

export function raiseForStatus(status: number, body: string, path: string): never {
  if (status === 404) {
    throw new OdysseyAPINotFoundError(path, body);
  }
  throw new OdysseyAPIError(status, path, body);
}
