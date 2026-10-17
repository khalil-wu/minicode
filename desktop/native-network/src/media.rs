//! Meter media with the pinned Source algorithms, independently of network policy.
use serde::Deserialize;

#[derive(Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct AudioTokensRequest {
    pub audio_url: String,
}

#[derive(Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct OriginalImageBytesRequest {
    pub image_url: String,
}

pub fn audio_tokens(audio_url: &str) -> usize {
    codex_utils_audio::estimate_audio_token_count(audio_url)
}

pub fn original_image_bytes(image_url: &str) -> Option<i64> {
    original_image::estimate(image_url)
}

mod original_image {
    use base64::Engine;
    use base64::engine::general_purpose::STANDARD as BASE64_STANDARD;
    use codex_utils_cache::{BlockingLruCache, sha1_digest};
    use codex_utils_string::approx_bytes_for_tokens;
    use std::num::NonZeroUsize;
    use std::sync::{Arc, LazyLock, OnceLock};

    include!("source_original_image.rs");

    pub(super) fn estimate(image_url: &str) -> Option<i64> {
        estimate_original_image_bytes(image_url)
    }
}

#[cfg(test)]
mod tests {
    use super::{audio_tokens, original_image_bytes};
    use base64::Engine;
    use base64::engine::general_purpose::STANDARD;
    use image::{ImageBuffer, ImageFormat, Rgb};
    use std::io::Cursor;

    fn wav_url() -> String {
        let mut wav = Vec::new();
        wav.extend_from_slice(b"RIFF");
        wav.extend_from_slice(&32036u32.to_le_bytes());
        wav.extend_from_slice(b"WAVEfmt ");
        wav.extend_from_slice(&16u32.to_le_bytes());
        wav.extend_from_slice(&1u16.to_le_bytes());
        wav.extend_from_slice(&1u16.to_le_bytes());
        wav.extend_from_slice(&8000u32.to_le_bytes());
        wav.extend_from_slice(&16000u32.to_le_bytes());
        wav.extend_from_slice(&2u16.to_le_bytes());
        wav.extend_from_slice(&16u16.to_le_bytes());
        wav.extend_from_slice(b"data");
        wav.extend_from_slice(&32000u32.to_le_bytes());
        wav.resize(32044, 0);
        format!("data:audio/wav;base64,{}", STANDARD.encode(wav))
    }

    #[tokio::test(flavor = "multi_thread")]
    async fn audio_uses_source_duration_and_whole_url_budget() {
        assert_eq!(audio_tokens(&wav_url()), 20);
        for url in ["data:audio/wav;base64,%%%", "data:audio/flac;base64,YXVkaW8=", "https://example.test/audio.mp3"] {
            assert_eq!(audio_tokens(url), url.len().div_ceil(4));
        }
    }

    #[tokio::test(flavor = "multi_thread")]
    async fn original_image_preserves_source_formats_and_patch_budget() {
        for format in [ImageFormat::Png, ImageFormat::Jpeg, ImageFormat::Gif, ImageFormat::WebP] {
            let image = ImageBuffer::<Rgb<u8>, Vec<u8>>::from_pixel(65, 33, Rgb([3, 9, 15]));
            let mut encoded = Cursor::new(Vec::new());
            image.write_to(&mut encoded, format).unwrap();
            let url = format!("DATA:IMAGE/{};BASE64,{}", format.extensions_str()[0], STANDARD.encode(encoded.into_inner()));
            assert_eq!(original_image_bytes(&url), Some(24));
        }
        let image = ImageBuffer::<Rgb<u8>, Vec<u8>>::from_pixel(3201, 3201, Rgb([3, 9, 15]));
        let mut encoded = Cursor::new(Vec::new());
        image.write_to(&mut encoded, ImageFormat::Png).unwrap();
        let url = format!("data:image/png;base64,{}", STANDARD.encode(encoded.into_inner()));
        assert_eq!(original_image_bytes(&url), Some(40000));
    }

    #[tokio::test(flavor = "multi_thread")]
    async fn original_image_rejects_wrong_modality_and_corrupt_pixels() {
        assert_eq!(original_image_bytes("data:audio/wav;base64,YXVkaW8="), None);
        assert_eq!(original_image_bytes("data:image/png;base64,%%%"), None);
        assert_eq!(original_image_bytes("https://example.test/a.png"), None);
        let image = ImageBuffer::<Rgb<u8>, Vec<u8>>::from_pixel(65, 33, Rgb([3, 9, 15]));
        let mut encoded = Cursor::new(Vec::new());
        image.write_to(&mut encoded, ImageFormat::Png).unwrap();
        let broken = &encoded.get_ref()[..33];
        let url = format!("data:image/png;base64,{}", STANDARD.encode(broken));
        assert_eq!(original_image_bytes(&url), None);
    }
}
