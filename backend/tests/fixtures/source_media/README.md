# Source media meter fixtures

These files contain two seconds of locally generated silence, with no user
recording or external media. The WAV contains 16,000 mono PCM16 samples at
8,000 Hz. The compressed files were produced by the local FFmpeg encoder
from `anullsrc=r=8000:cl=mono`, with `-t 2` and:

| File | Encoding options |
| --- | --- |
| `silence.mp3` | `-c:a libmp3lame -b:a 32k -write_xing 1` |
| `silence.m4a` | `-c:a aac -b:a 32k` |
| `silence.ogg` | `-c:a libvorbis -ar 8000 -q:a 0` |
| `silence.webm` | `-c:a libopus -ar 48000 -b:a 16k` |
| `silence-live.webm` | Same Opus settings, plus `-live 1` |

The live WebM intentionally has no duration metadata. Source's Symphonia
estimator returns its URL byte budget in that case, even though its audio
packets can be decoded. Ordinary codec/container timing may include encoder
padding. FFmpeg is only a fixture producer; it is not required by the product
or by these tests. WebM UIDs and encoder tags may differ on regeneration.

The pinned Source estimator returns 20, 20, 22, 20 and 21 tokens for the WAV,
MP3, M4A, Ogg and ordinary WebM files respectively. M4A uses Source's selected
track timing, including encoder padding. The live file's complete data URL
costs 977 tokens. These goldens were measured with the unmodified
`codex_utils_audio::estimate_audio_token_count` at commit
`822e58cc3d666166c7446c5b1ea2e52f5d09594c`; FFprobe's presentation duration is
not the token-estimate oracle.

The local generation commands, encoder version and SHA256 identities are
retained in `output/source-media-meter-20261017/fixture-provenance.json`.
