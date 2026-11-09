/** Decode user-selected audio into a local 24 kHz PCM WAV (mono or stereo, under 30 s). */
export const notificationSoundWav = async (bytes: number[]): Promise<number[]> => {
  const context = new OfflineAudioContext(2, 1, 24000);
  const audio = await context.decodeAudioData(new Uint8Array(bytes).buffer);
  if (![1, 2].includes(audio.numberOfChannels) || audio.length === 0 || audio.duration >= 30) {
    throw new Error("通知声音必须短于 30 秒，并且为单声道或立体声音频。");
  }
  const channels = audio.numberOfChannels;
  const frameBytes = channels * 2;
  const result = new ArrayBuffer(44 + audio.length * frameBytes);
  const view = new DataView(result);
  const text = (offset: number, value: string) => { for (let index = 0; index < value.length; index++) view.setUint8(offset + index, value.charCodeAt(index)); };
  text(0, "RIFF"); view.setUint32(4, result.byteLength - 8, true); text(8, "WAVEfmt ");
  view.setUint32(16, 16, true); view.setUint16(20, 1, true); view.setUint16(22, channels, true);
  view.setUint32(24, 24000, true); view.setUint32(28, 24000 * frameBytes, true);
  view.setUint16(32, frameBytes, true); view.setUint16(34, 16, true);
  text(36, "data"); view.setUint32(40, audio.length * frameBytes, true);
  for (let channel = 0; channel < channels; channel++) {
    const samples = audio.getChannelData(channel);
    for (let index = 0; index < audio.length; index++) {
      const sample = Math.max(-1, Math.min(1, samples[index]));
      view.setInt16(44 + index * frameBytes + channel * 2, Math.round(sample * (sample < 0 ? 32768 : 32767)), true);
    }
  }
  return [...new Uint8Array(result)];
};
