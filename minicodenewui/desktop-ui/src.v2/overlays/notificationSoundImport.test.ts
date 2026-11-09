import { afterEach, expect, it, vi } from "vitest";
import { notificationSoundWav } from "./notificationSoundImport";

afterEach(() => vi.unstubAllGlobals());

it("resamples through the native decoder and serializes stereo PCM without changing channel order", async () => {
  const channels = [new Float32Array([0, 0.5, -1]), new Float32Array([1, -0.5, 0])];
  vi.stubGlobal("OfflineAudioContext", class {
    constructor(channelCount: number, length: number, rate: number) { expect([channelCount, length, rate]).toEqual([2, 1, 24000]); }
    async decodeAudioData(bytes: ArrayBuffer) {
      expect([...new Uint8Array(bytes)]).toEqual([1, 2, 3]);
      return { numberOfChannels: 2, length: 3, duration: 3 / 24000, getChannelData: (index: number) => channels[index] };
    }
  });
  const wav = await notificationSoundWav([1, 2, 3]);
  const view = new DataView(new Uint8Array(wav).buffer);
  expect(String.fromCharCode(...wav.slice(0, 4))).toBe("RIFF");
  expect(view.getUint16(22, true)).toBe(2);
  expect(view.getUint32(24, true)).toBe(24000);
  expect(Array.from({ length: 6 }, (_, index) => view.getInt16(44 + index * 2, true))).toEqual([0, 32767, 16384, -16384, -32768, 0]);
});

it("rejects audio at the Source thirty-second duration boundary", async () => {
  vi.stubGlobal("OfflineAudioContext", class {
    async decodeAudioData() { return { numberOfChannels: 1, length: 720000, duration: 30 }; }
  });
  await expect(notificationSoundWav([0])).rejects.toThrow("30");
});
