"""Audio quality verification: declared bitrate + Spek-style spectral analysis.

A file claiming to be 320kbps (or lossless) can secretly be a transcode of a
low-bitrate source. The giveaway - the thing you'd spot in Spek - is the
frequency cutoff: lossy encoders discard the top of the spectrum, and the
lower the bitrate the lower the shelf. Roughly (44.1kHz material):

    ~16.0 kHz  -> 128 kbps class
    ~18.0 kHz  -> 192 kbps class
    ~19.3 kHz  -> 256 kbps class
    ~20.0+ kHz -> 320 kbps class / genuine lossless

We decode a slice of the file to PCM with the bundled ffmpeg, average the
power spectrum over many FFT windows, and find where the energy falls off
into the noise floor.
"""
from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass

import numpy as np
from mutagen import File as MutagenFile

LOSSLESS_EXTS = {"flac", "wav", "ape", "aiff", "alac"}
LOSSY_EXTS = {"mp3", "m4a", "aac", "ogg", "opus", "wma"}

# Spectral cutoff (Hz) above which content is considered 320kbps-class
CUTOFF_320_HZ = 19_000
# Below this cutoff we consider the file a low-bitrate transcode regardless
# of what its header claims
CUTOFF_FLOOR_HZ = 15_500


class VerificationError(Exception):
    pass


@dataclass
class QualityReport:
    codec: str
    is_lossless: bool
    declared_bitrate_kbps: int
    sample_rate: int
    duration_sec: float
    spectral_cutoff_hz: int | None = None   # None if spectral check skipped
    estimated_class: str = ""               # e.g. "320kbps-class", "~128kbps transcode"
    passes_320: bool = False
    notes: str = ""

    def summary(self) -> str:
        parts = [self.codec.upper()]
        if self.is_lossless:
            parts.append("lossless")
        elif self.declared_bitrate_kbps:
            parts.append(f"{self.declared_bitrate_kbps}kbps")
        if self.spectral_cutoff_hz is not None:
            parts.append(f"cutoff {self.spectral_cutoff_hz / 1000:.1f}kHz")
        if self.estimated_class:
            parts.append(self.estimated_class)
        return ", ".join(parts)


def _ffmpeg_exe() -> str:
    import imageio_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()


def probe_declared_quality(path: str) -> tuple[str, bool, int, int, float]:
    """Read codec/bitrate/sample-rate from the file headers via mutagen."""
    audio = MutagenFile(path)
    if audio is None or audio.info is None:
        raise VerificationError("Unrecognized or corrupt audio file")

    codec = type(audio).__name__.lower()          # MP3 -> "mp3", FLAC -> "flac"
    ext = path.rsplit(".", 1)[-1].lower() if "." in path else codec
    is_lossless = ext in LOSSLESS_EXTS or codec in LOSSLESS_EXTS
    bitrate = int(getattr(audio.info, "bitrate", 0) or 0) // 1000
    sample_rate = int(getattr(audio.info, "sample_rate", 44100) or 44100)
    duration = float(getattr(audio.info, "length", 0.0) or 0.0)
    return ext, is_lossless, bitrate, sample_rate, duration


def measure_spectral_cutoff(path: str, sample_rate: int, duration_sec: float,
                            analyze_sec: float = 40.0) -> int:
    """Decode a middle slice of the file and estimate the frequency cutoff.

    Returns the highest frequency (Hz) that still carries signal energy.
    """
    # Analyze the middle of the track - intros/outros are often quiet
    start = max(0.0, duration_sec / 2 - analyze_sec / 2) if duration_sec else 0.0

    cmd = [
        _ffmpeg_exe(), "-v", "error",
        "-ss", f"{start:.2f}", "-t", f"{analyze_sec:.2f}",
        "-i", path,
        "-ac", "1", "-ar", str(sample_rate),
        "-f", "f32le", "-acodec", "pcm_f32le", "-",
    ]
    creationflags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    proc = subprocess.run(cmd, capture_output=True, timeout=120,
                          creationflags=creationflags)
    if proc.returncode != 0 or len(proc.stdout) < 4 * sample_rate:
        raise VerificationError(
            f"ffmpeg could not decode file: {proc.stderr.decode(errors='replace')[:200]}")

    samples = np.frombuffer(proc.stdout, dtype=np.float32)

    # Averaged periodogram (Welch-style): 8192-sample Hann windows, 50% overlap
    n_fft = 8192
    hop = n_fft // 2
    window = np.hanning(n_fft)
    n_frames = max(1, (len(samples) - n_fft) // hop)
    psd = np.zeros(n_fft // 2 + 1)
    frames_used = 0
    for i in range(n_frames):
        frame = samples[i * hop: i * hop + n_fft]
        if len(frame) < n_fft:
            break
        # Skip near-silent frames; they only flatten the average
        if np.max(np.abs(frame)) < 1e-4:
            continue
        spectrum = np.abs(np.fft.rfft(frame * window)) ** 2
        psd += spectrum
        frames_used += 1
    if frames_used < 8:
        raise VerificationError("Not enough audible signal to analyze spectrum")
    psd /= frames_used

    freqs = np.fft.rfftfreq(n_fft, d=1.0 / sample_rate)
    db = 10 * np.log10(psd + 1e-20)

    # Smooth with a ~100Hz moving average to suppress single-bin spikes
    kernel = max(3, int(100 / (freqs[1] - freqs[0])))
    db_smooth = np.convolve(db, np.ones(kernel) / kernel, mode="same")

    # Lossy encoders leave a hard "cliff" at their lowpass frequency: the
    # level drops tens of dB within a few hundred Hz, down to digital
    # silence. Genuine full-bandwidth audio decays gradually. So instead of
    # an absolute threshold we scan for a sharp drop.
    nyquist = float(freqs[-1])

    def band_median(lo: float, hi: float) -> float | None:
        sel = db_smooth[(freqs >= lo) & (freqs < hi)]
        return float(np.median(sel)) if len(sel) else None

    f = 10_000.0
    while f < nyquist - 1500:
        below = band_median(f - 800, f)
        above_level = band_median(f + 300, f + 1300)
        if below is not None and above_level is not None and below - above_level >= 25.0:
            # Cliff detected near f: refine to the first bin that falls
            # well below the pre-cliff level
            region = np.where((freqs >= f) & (db_smooth < below - 15.0))[0]
            return int(freqs[region[0]]) if len(region) else int(f)
        f += 250.0

    # No cliff anywhere: full-bandwidth content
    return int(nyquist)


def classify_cutoff(cutoff_hz: int) -> str:
    if cutoff_hz >= CUTOFF_320_HZ:
        return "320kbps-class or better"
    if cutoff_hz >= 18_000:
        return "~256kbps transcode"
    if cutoff_hz >= 17_000:
        return "~192kbps transcode"
    if cutoff_hz >= CUTOFF_FLOOR_HZ:
        return "~160kbps transcode"
    return "~128kbps-or-worse transcode"


def verify_file(path: str, spectral: bool = True) -> QualityReport:
    """Full verification: header bitrate + optional spectral double-check.

    ``passes_320`` is True when the file is (a) lossless or declared >=320kbps
    AND (b) the spectral cutoff confirms 320kbps-class content (when enabled).
    """
    codec, is_lossless, bitrate, sample_rate, duration = probe_declared_quality(path)

    report = QualityReport(
        codec=codec, is_lossless=is_lossless,
        declared_bitrate_kbps=bitrate,
        sample_rate=sample_rate, duration_sec=duration,
    )

    declared_ok = is_lossless or bitrate >= 315   # small tolerance for VBR ~320

    if spectral:
        try:
            cutoff = measure_spectral_cutoff(path, sample_rate, duration)
            report.spectral_cutoff_hz = cutoff
            report.estimated_class = classify_cutoff(cutoff)
            spectral_ok = cutoff >= CUTOFF_320_HZ
        except VerificationError as exc:
            report.notes = f"spectral check skipped: {exc}"
            spectral_ok = True   # don't fail files we simply couldn't analyze
    else:
        spectral_ok = True

    report.passes_320 = declared_ok and spectral_ok
    if declared_ok and not spectral_ok:
        report.notes = (f"header claims "
                        f"{'lossless' if is_lossless else f'{bitrate}kbps'} "
                        f"but spectrum says {report.estimated_class} - likely fake")
    return report
