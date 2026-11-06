# Voyager live spectrum + sonification

```
VRT IQ (other machine) ─► vrt_tuner --tracking ─► vrt_spectrum ... --pub-port 50500 ─┬─► stdout / gnuplot (as before)
                                                                                     └─► vgr_relay.py ─► http://localhost:8090 (OBS browser source)
```

## 1. vrt_spectrum patch

`vrt_spectrum-overlap-pub.patch` applies to vrt-iq-tools `ea68153` (`git apply`). It adds two options:

- `--overlap K`: advance num-bins/K samples per FFT, with or without `--wola`. The `--integration-time` can then go down to (1/bin size)/K. With `K=1`, output is byte-identical to the unpatched tool. I tested plain `--db`, `--gnuplot --fftmax` and `--wola`, 250 integrations each.
- `--pub-port P`: every integration is also published as a 3-part ZMQ message. This works alongside any stdout mode.
  1. JSON header: `t`, `rf_freq` (including the fractional Hz), `f0`/`df` (sky frequency of bin 0 and bin step), `num_bins`, `integration_time`, `tau`, `overlap`, `wola`, `source`, and `tracker` / `dt` blocks when those contexts are in the stream.
  2. float32 averaged spectrum (linear, `--poly`-corrected).
  3. float32 instantaneous spectrum.

Example for the stream (1 Hz bins, 4 updates/s, 60 s average), keeping gnuplot as a backup:

```
vrt_spectrum <your ports/options> --bin-size 1 --wola --overlap 4 --integration-time 0.25 --tau 60 \
             --gnuplot --fftmax --pub-port 50500 | gnuplot
```

## 2. Relay and page

```
pip3 install pyzmq aiohttp
python3 vgr_relay.py --zmq tcp://localhost:50500 --http-port 8090
```

- Open `http://localhost:8090/` in a browser. Keep `localhost`: AudioWorklet needs a secure context. Over a plain IP address the page falls back to the older ScriptProcessor.
- **OBS:** add a Browser source with that URL, about 1280×540, and tick **Control audio via OBS**. The audio then appears as the source's own channel in the mixer.
- In a normal browser, click once to enable audio.

URL parameters (all optional):

| param | meaning | default |
|---|---|---|
| `lo` | audio LO, sky frequency in Hz; tone = f_sky − lo (fixed: 1:1 Doppler) | auto, printed bottom-left |
| `tone` | without `lo`: pick lo at page load so the window centre (`woff`) sounds at this pitch | 1000 |
| `woff` | centre of the sonified window, Hz from band centre (fixed bin) | 0 |
| `win` | half width of the sonified window, Hz | 150 |
| `gamma`, `knee`, `zmax`, `hiss` | contrast curve, see below | 4, 2, 8, 1 |
| `tbw` | bandwidth of the above-noise part of each bin, Hz (smaller = steadier tone) | 0.2 |
| `vol` | 0..1 | 0.5 |
| `span`, `center` | displayed span / centre offset, Hz | full band |
| `ymin`, `ymax` | fixed y range, dB | auto |
| `inst=1` | also draw the instantaneous spectrum | off |
| `bg` | background, e.g. `transparent` | black |

Keys: `m` mute, `+`/`-` volume, `c` centre the sonified window on the current peak (then put that `woff` in the URL).

## 3. Sonification

The sound is made from the 60 s averaged spectrum only, with no peak detector and no synthetic tone.

1. Each bin in the window is turned into a z-score: its excess over the median floor, in units of the averaging noise (MAD).
2. Each bin becomes a narrow-band noise partial, about one bin wide, at audio frequency f_sky − lo. Its power is `hiss + clamp(z − knee, 0, zmax − knee)^gamma`.
3. **Off source:** every bin sits near the floor, so the result is band-limited hiss.
4. **On source:** the carrier bin rises out of the hiss as a tone and fades with τ after you slew away.

Off-source loudness is normalised, so it stays the same for any window size or contrast setting.

Because averaging happens in the Doppler-corrected frame and playback in the sky frame, the pitch drifts exactly as the real Doppler does.

**Pitch.** `woff` only chooses *which bin* is sonified, and `lo` (or `tone`, at page load) sets the pitch. Put `woff` on the carrier first (press `c` once on source), then set the pitch. In the May 2025 recording the carrier is at `woff=251`, so `?woff=251&tone=500` starts at 500 Hz. With 1:1 Doppler the pitch can't stay put: it drifts at the tracker's Doppler rate, which the status line shows. In the recording that was −0.39 Hz/s, so 500 Hz becomes about 75 Hz after 18 minutes.

**Choosing `lo` for the day:** set it explicitly, because `tone` re-picks lo on every page reload. Use lo = (tracker frequency + Doppler at the moment you choose) + woff − the pitch you want then. For example, ~1 kHz at 10:16:06 gives roughly 2.3 kHz at 09:16 and 0.4 kHz at 10:46.

For 18 Nov the drift should be roughly −0.3 to −0.4 Hz/s: Earth rotation, partly offset by Earth's orbital acceleration. Your recording showed −0.38 to −0.41 Hz/s. The page warns when the window leaves the audio range.

## sigmf_to_vrt --vrt fix

`sigmf_to_vrt-vrt-overflow.patch`: with `--vrt`, packets are `fread` into `samples[VRT_SAMPLES_PER_PACKET]` (40000 bytes). A full 10000-sample data packet is 10007 words = 40028 bytes, so the read overflows the stack by 28 bytes. Linux glibc builds abort with `*** buffer overflow detected ***`; on macOS it probably just overwrites the next variable silently. The patch reads into `buffer[VRT_DATA_PACKET_SIZE]` instead, with a size check.

## Test files

- `synthetic_sonification.mp3`: the page's own audio code, rendered offline from a synthetic run (details in the chat message).
- `vgr1_recording_sonified_tone500.mp3`: first 5 minutes of your 14 May 2025 recording (`--tau 300 --bin-size 1 --wola`), `?woff=251&tone=500`.
- `live_test.png`: screenshot of the page during the live test.
