export const MAX_NATIVE_IMAGE_BYTES = (5 * 1024 * 1024 * 3) / 4;
// MiniCode refuses to process source images above 20 MiB before decoding
// or compression. Enforce the same boundary so a huge bitmap cannot consume
// unbounded renderer memory merely because its compressed output might fit.
export const MAX_IMAGE_SOURCE_BYTES = 20 * 1024 * 1024;
export const MAX_NATIVE_IMAGE_WIDTH = 2000;
export const MAX_NATIVE_IMAGE_HEIGHT = 2000;

const canvasBlob = (canvas: HTMLCanvasElement, type: string, quality?: number): Promise<Blob | null> => (
  new Promise((resolve) => canvas.toBlob(resolve, type, quality))
);

const outputName = (name: string, mediaType: string): string => {
  const extension = mediaType === "image/png" ? "png" : "jpg";
  const stem = name.replace(/\.[^.]+$/, "") || "image";
  return `${stem}.${extension}`;
};

const fileFromBlob = (source: File, blob: Blob, mediaType: string): File => (
  new File([blob], outputName(source.name, mediaType), {
    type: mediaType,
    lastModified: source.lastModified,
  })
);

export const prepareNativeImageFile = async (file: File): Promise<File> => {
  if (!file.type.startsWith("image/")) return file;
  if (file.size > MAX_IMAGE_SOURCE_BYTES) {
    throw new Error("图片超过 20 MB，无法处理；请先缩小图片。");
  }
  if (typeof createImageBitmap !== "function") {
    if (file.size <= MAX_NATIVE_IMAGE_BYTES) return file;
    throw new Error("图片超过 3.75 MB，当前环境无法自动压缩；请先缩小图片。");
  }

  let bitmap: ImageBitmap;
  try {
    bitmap = await createImageBitmap(file);
  } catch {
    if (file.size <= MAX_NATIVE_IMAGE_BYTES) return file;
    throw new Error("无法读取并压缩这张图片；请改用 PNG、JPEG、GIF 或 WebP。");
  }

  try {
    const initialScale = Math.min(
      1,
      MAX_NATIVE_IMAGE_WIDTH / Math.max(1, bitmap.width),
      MAX_NATIVE_IMAGE_HEIGHT / Math.max(1, bitmap.height),
    );
    if (initialScale === 1 && file.size <= MAX_NATIVE_IMAGE_BYTES) return file;

    const canvas = document.createElement("canvas");
    const context = canvas.getContext("2d");
    if (!context) throw new Error("当前环境无法创建图片压缩画布。");

    let width = Math.max(1, Math.round(bitmap.width * initialScale));
    let height = Math.max(1, Math.round(bitmap.height * initialScale));
    const originalIsPng = file.type === "image/png";

    for (let attempt = 0; attempt < 8; attempt += 1) {
      canvas.width = width;
      canvas.height = height;
      context.clearRect(0, 0, width, height);
      context.drawImage(bitmap, 0, 0, width, height);

      if (originalIsPng && attempt === 0) {
        const png = await canvasBlob(canvas, "image/png");
        if (png && png.size <= MAX_NATIVE_IMAGE_BYTES) return fileFromBlob(file, png, "image/png");
      }

      // JPEG is the same lossy fallback MiniCode uses after lossless PNG
      // compression cannot fit the provider envelope.
      const quality = Math.max(0.45, 0.9 - attempt * 0.07);
      const jpeg = await canvasBlob(canvas, "image/jpeg", quality);
      if (jpeg && jpeg.size <= MAX_NATIVE_IMAGE_BYTES) return fileFromBlob(file, jpeg, "image/jpeg");

      width = Math.max(1, Math.round(width * 0.82));
      height = Math.max(1, Math.round(height * 0.82));
    }
  } finally {
    bitmap.close?.();
  }

  throw new Error("图片压缩后仍超过 3.75 MB；请先降低分辨率后重试。");
};
