<p align="center">
  <img src="assets/icon.png" width="96" alt="SpiritSeeker icon">
</p>

<h1 align="center">SpiritSeeker</h1>

<p align="center">
  Paste a Spotify playlist link &rarr; get the tracks from Soulseek as verified
  320kbps+/lossless files, tagged with MusicBrainz metadata and cover art.
</p>

<p align="center">
  <em>&#x1F916; AI disclosure: this application was created with AI assistance
  (Anthropic's Claude), from architecture through implementation and testing.</em>
</p>

---

## What it does

1. **Fetches your playlist — or a single song** — straight from Spotify's
   public pages. Paste either a playlist link or a track link; no Spotify
   account, no API keys, nothing to register.
2. **Searches Soulseek** for every track. A Soulseek account is created
   automatically on first run (the network registers accounts on first login).
   Prefer your own account? *Account & sharing...* lets you log in with your
   existing Soulseek credentials and share folders back to the network for
   uploads — some peers only serve users who share.
3. **Downloads the best copy**: FLAC/lossless preferred, then MP3 320kbps.
   Sources are ranked by quality, matching filename, duration, free upload
   slots and peer speed. If one peer stalls, the next candidate is tried.
4. **Verifies quality like Spek does**: every download is decoded and its
   frequency spectrum analyzed. A "320kbps" file that was actually transcoded
   from a 128kbps source has a telltale ~16kHz cutoff shelf — those fakes are
   deleted and the next source is tried. Fake "lossless" transcodes are caught
   the same way.
5. **Tags the files** using the free MusicBrainz database (title, artist,
   album, year, track number) and embeds cover art from the Cover Art Archive.
   Falls back to the Spotify metadata when MusicBrainz has no match.
6. **Saves everything flat** as `Artist - Title.ext` in your chosen folder,
   skipping tracks you already have.

Tracks that can't be found on Soulseek (or whose peers never start sending)
are given up on after 8 minutes and marked failed, so a rare song never
stalls the rest of the playlist. The timeout only counts searching and
queue waiting — a download that's still receiving data is never cut off,
however slow the peer. Tune all of this in **Settings...**: track timeout,
stall timeout, and how many sources to try per track.

Searches are paced (about 30 per 220 seconds) because the Soulseek server
silently drops searches from clients that fire too many too fast — without
this, long playlists suddenly report "no sources found" for everything. If
the server rate-limits us anyway, SpiritSeeker notices the empty-result
streak, cools down for a few minutes, and picks up where it left off.

Right-click any track for the useful stuff: **Play**, **Open folder
location**, **Copy file path**, **Copy "Artist - Title"**, **Retry
download** / **Download again**, and **Skip download** to leave songs out
of a run. Double-click plays the file. Dark mode is on by default
(toggle it in Settings).

Tracks download **two at a time** by default (up to 4, see Settings), each
with a live progress bar and transfer rate in the list, and failed tracks
automatically get one more pass at the end of the run. When everything's
done you get a Windows notification and a sound.

## Chat, rooms, and browsing (the Nicotine+ essentials)

The **Chat...** button opens a full Soulseek chat client sharing the same
connection as your downloads:

- **Private messages**: start conversations by username, receive messages
  (unread count shows on the Chat button while the window is closed).
- **Chat rooms**: browse the public room list, join/leave rooms, and chat.
- **Browse users**: fetch any user's entire shared-file tree and
  double-click files to download them straight to your save folder —
  great for grabbing a whole album once one good track led you to a
  well-stocked sharer.

The connection stays up in the background, so chat keeps working while
playlists download and vice versa.

## Quality rules

- **Default (strict)**: only lossless files or MP3s at 320kbps that *pass*
  spectral verification are kept.
- **"Allow lower quality"** checkbox: takes the best available copy when no
  320kbps+/lossless source exists.
- **Spectral verification** can be toggled off if you're in a hurry.

## Getting started

### Option A: download the .exe

Grab `SpiritSeeker.exe` from the [releases page](../../releases), run it,
paste a playlist link, pick a folder, hit **Download all**. That's it.

> Windows SmartScreen may warn about an unsigned executable — choose
> "More info" &rarr; "Run anyway", or build it yourself (option C).

### Option B: run from source

```bat
git clone https://github.com/<you>/spiritseeker
cd spiritseeker
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python run.py
```

### Option C: build the .exe yourself

```bat
build.bat
```

The exe lands in `dist\SpiritSeeker.exe`.

## Playlists longer than 100 tracks

Spotify's public pages only expose the first 100 tracks of a playlist. For
longer playlists, export a CSV with
[Exportify](https://exportify.net) or
[chosic's Spotify Playlist Exporter](https://www.chosic.com/spotify-playlist-exporter/)
and use the **Import CSV...** button.

## How the fake-320 detection works

Lossy encoders throw away the top of the frequency spectrum; the lower the
bitrate, the lower the cutoff (roughly 16kHz at 128kbps, 19kHz at 256kbps,
20kHz+ at 320kbps). Re-encoding a 128kbps file at 320kbps doesn't bring the
lost content back — the file *says* 320 but the spectrum still ends at 16kHz.

SpiritSeeker decodes a 40-second slice of every download (bundled ffmpeg),
averages the power spectrum over ~340 FFT windows, and scans for the sharp
"cliff" a lossy encoder leaves behind. Cliff below 19kHz &rarr; the file is
rejected and the next source is tried. It's the same judgement you'd make
eyeballing the file in [Spek](https://spek.cc), automated.

## Notes

- **No API keys anywhere**: Spotify is read from its public embed pages,
  MusicBrainz and Cover Art Archive are free APIs, and Soulseek just needs a
  username/password the app generates for you (shown in the status bar;
  regenerate anytime with *New identity*).
- Config lives in `%APPDATA%\SpiritSeeker\config.json`.
- Soulseek sharing works better with an open/UPnP-mapped listening port; the
  app requests a UPnP mapping automatically (port 61000 by default). Using a
  VPN with port forwarding (e.g. PIA)? Enter the forwarded port under
  *Account & sharing... &rarr; Connection*. If the port is already taken
  (say, by a second running copy of SpiritSeeker), the app logs a warning
  and falls back to a nearby free port instead of failing.
- Windows only (tested on Windows 10/11 and Server 2025). The Python code is
  cross-platform apart from packaging; PRs welcome.

## Legal

SpiritSeeker is a tool for searching and downloading files shared by peers on
the Soulseek network. Downloading copyrighted material you don't have the
rights to may be illegal in your jurisdiction. Use it for music you're
entitled to download. The authors take no responsibility for how you use it.

## License

[MIT](LICENSE)
