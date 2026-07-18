import { Writable } from "node:stream";

export const responseMetadata = new WeakMap();

globalThis.awslambda = {
  streamifyResponse(streamingHandler) {
    return streamingHandler;
  },
  HttpResponseStream: {
    from(responseStream, metadata) {
      responseMetadata.set(responseStream, metadata);
      return responseStream;
    },
  },
};

const IMPORT_TIME_ENV_NAMES = [
  "MAX_REQUEST_BODY_BYTES",
  "PROXY_MAX_RETRIES",
  "PROXY_RETRY_BACKOFF_BASE",
  "SECRET_CACHE_MAX_STALE_SECONDS",
  "SECRET_CACHE_RETRY_SECONDS",
  "SECRET_CACHE_TTL_SECONDS",
];
const importTimeEnvironment = new Map(
  IMPORT_TIME_ENV_NAMES.map((name) => [name, process.env[name]]),
);
for (const name of IMPORT_TIME_ENV_NAMES) {
  delete process.env[name];
}

let proxyModule;
try {
  proxyModule = await import(
    new URL("../../lambda/inference-streaming-proxy/index.mjs", import.meta.url),
  );
} finally {
  for (const [name, value] of importTimeEnvironment) {
    if (value === undefined) {
      delete process.env[name];
    } else {
      process.env[name] = value;
    }
  }
}

export const { __test } = proxyModule;

export class CollectingWritable extends Writable {
  constructor({ delayMs = 0, highWaterMark = 16 * 1024 } = {}) {
    super({ highWaterMark });
    this.chunks = [];
    this.delayMs = delayMs;
    this.maxWritableLength = 0;
  }

  write(chunk, encoding, callback) {
    const accepted = super.write(chunk, encoding, callback);
    this.maxWritableLength = Math.max(
      this.maxWritableLength,
      this.writableLength,
    );
    return accepted;
  }

  _write(chunk, _encoding, callback) {
    this.chunks.push(Buffer.from(chunk));
    if (this.delayMs > 0) {
      setTimeout(callback, this.delayMs);
      return;
    }
    callback();
  }

  buffer() {
    return Buffer.concat(this.chunks);
  }

  text() {
    return this.buffer().toString("utf8");
  }
}
