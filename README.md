# Voyager live spectrum + sonification

```
VRT IQ (other machine) ─► vrt_tuner --tracking ─► vrt_spectrum ... --pub-port 50500 ─┬─► stdout / gnuplot (as before)
                                                                                     └─► vgr_relay.py ─► http://localhost:8090 (OBS browser source)
```

## 1. vrt_spectrum patch

`vrt_spectrum-overlap-pub.patch` applies to vrt-iq-tools `ea68153` (`git apply`). It adds two options:

- `--overlap K`: advance num-bins/K samples per FFT, with or without `--wola`. The `--integration-time` can then go down to (1/bin size)/K. With `K=1`, output is byte-identical to the unpatched tool. I tested plain `--db`, `--gnuplot --fftmax` and `--wola`, 250 integrations each.
- `--pub-port P`: every integration is also published as a 3-part ZMQ message. This works alongside any stdout mode.
  1. JSON header: `t`, `rf_freq` (including the fractional Hz), `f0`/`df` (sky frequency of bin 0 and bin step), `num_bins`, `seq` (spectra since start), `integration_time`, `tau`, `averaging` (`none`, `running` for `--tau inf`, or `exponential`), `overlap`, `wola`, `source`, and `tracker` / `dt` blocks when those contexts are in the stream.
  2. float32 averaged spectrum (linear, `--poly`-corrected).
  3. float32 instantaneous spectrum.

`vrt_spectrum` exits when the incoming stream is interrupted and restarted (e.g. a packet counter reset). Its existing `--continue` option keeps it running, so it picks the stream up again by itself.

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

`vrt_tuner` sends 10000-sample packets, which at 10 kHz is one per second. With `--overlap 4`, `vrt_spectrum` therefore emits its four spectra in a burst once per second. The relay releases them at their data timestamps plus a constant delay (the largest lag of the last 5 s, about 0.75 s), so the page updates evenly every 0.25 s. `--no-pace` turns this off.

- Open `http://localhost:8090/` in a browser. Keep `localhost`: AudioWorklet needs a secure context. Over a plain IP address the page falls back to the older ScriptProcessor.
- **OBS:** add a Browser source with that URL, about 1280×540, and tick **Control audio via OBS**. The audio then appears as the source's own channel in the mixer.
- In a normal browser, click once to enable audio.

URL parameters (all optional):

| param | meaning | default |
|---|---|---|
| `lo` | starting audio LO, sky frequency in Hz; pitch = f_sky − lo (1:1 Doppler) | automatic |
| `low`, `high` | pitch range for the centre of the audio window, Hz (see Pitch below) | 300, 1800 |
| `retune=0` | never retune: the LO stays where it starts | retune on |
| `woff` | centre of the sonified window, Hz from band centre (fixed bin) | 0 |
| `win` | half width of the sonified window, Hz | 150 |
| `gamma`, `knee`, `zmax`, `hiss` | contrast curve, see below | 4, 2, 8, 1 |
| `tbw` | bandwidth of the above-noise part of each bin, Hz (smaller = steadier tone) | 0.2 |
| `vol` | 0..1 | 0.5 |
| `span`, `center` | displayed span / centre offset, Hz | full band |
| `ymin`, `ymax` | fixed y range, dB | auto |
| `inst=1` | also draw the instantaneous spectrum | off |
| `theme=light` | white background, dark text and grid, trace `#9400d3` | dark |
| `line` | trace colour as hex without `#`, e.g. `line=9400d3` | theme |
| `bg` | background, e.g. `transparent` | theme |
| `status=0` | hide the status line at the bottom (LO, pitch, elevation, warnings) and the shaded audio window; both are operator aids | shown |
| `stale` | seconds without spectra after which the sound fades out over 1.5 s; it fades back in when spectra return | 5 |
| `nodata=0` | don't show the "no data since …" note on the plot while spectra are missing | shown |

The title shows how much data is in the displayed spectrum. `vrt_spectrum` takes a plain mean of all spectra since it started until that covers τ, then switches to an exponential average:

- `average of 2.3 minutes`: during the first τ, and always with `--tau inf`, which never forgets.
- `average of the last ~5.0 minutes (exponential)`: once τ is reached.
- `no averaging`: without `--tau`.

The count restarts when `vrt_spectrum` restarts. Elevation and the other technical details are in the footer.

Keys: `m` mute, `+`/`-` volume, `c` centre the sonified window on the current peak (then put that `woff` in the URL).

## 3. Sonification

The sound is made from the 60 s averaged spectrum only, with no peak detector and no synthetic tone.

1. Each bin in the window is turned into a z-score: its excess over the median floor, in units of the averaging noise (MAD).
2. Each bin becomes a narrow-band noise partial, about one bin wide, at audio frequency f_sky − lo. Its power is `hiss + clamp(z − knee, 0, zmax − knee)^gamma`.
3. **Off source:** every bin sits near the floor, so the result is band-limited hiss.
4. **On source:** the carrier bin rises out of the hiss as a tone and fades with τ after you slew away.

Off-source loudness is normalised, so it stays the same for any window size or contrast setting.

Because averaging happens in the Doppler-corrected frame and playback in the sky frame, the pitch drifts exactly as the real Doppler does.

**Pitch.** `woff` chooses *which bin* is sonified; the LO sets the pitch: pitch = sky frequency − LO. Put `woff` on the carrier first: press `c` once on source, read `woff=` in the status line, put it in the URL and reload once before going live. In the May 2025 recording the carrier is at `woff=251`.

The LO is fixed, so the pitch drifts 1:1 with the Doppler. The tracker's Doppler rate is shown in the status line; in your rehearsal it was −0.39 Hz/s, or −1.4 kHz per hour. To keep it audible, the page **retunes automatically**:

- When the centre of the audio window drops below `low` (300 Hz), the LO steps once so the centre sounds at `high` (1800 Hz) again. When it rises above `high`, the LO steps back to `low`.
- Between steps nothing changes, so what you hear drifting is the real Doppler. A step is a single audible jump, as if an operator retuned the receiver.
- Without `lo` in the URL, the first LO puts the centre at `high` when the Doppler rate is negative (at `low` when it is positive).
- The status line shows the LO, the pitch range and how many retunes have happened.

On 18 Nov from Horizons the drift is about −0.18 Hz/s at 09:16, −0.28 at 10:16 and −0.33 at 10:46. Starting at 1800 Hz around 09:16, the centre is near 950 Hz at 10:16:06, and the first retune comes around 10:50. Pass `lo` if you want to fix the starting point exactly; `retune=0` turns retuning off.


## 4. Light-day overlay

`overlay.html` is served by the relay at `http://localhost:8090/overlay`. Use that URL as a second OBS browser source.

- **Time:** taken from the spectra's own timestamps, so the overlay always shows the same second as the spectrum page. Both show the time of the data, about 2 s behind real time. If no spectra arrive for 5 s, the overlay falls back to the computer clock; `?clock=local` forces that.
- **Rows:** current time, distance in light-hours, km still to go (after the crossing: km beyond one light-day), and the countdown.
- **Distance:** a JPL Horizons table for Dwingeloo (25-m) is built in, hourly for 17–19 Nov 2026 while Voyager is up. Between rows it interpolates using both distance and range rate, which is good to better than 1 km. The night gaps (Voyager below the horizon) are bridged with the same interpolation, good to a few hundred km there. Outside 17 Nov 09:00 – 19 Nov 20:00 it extrapolates in a straight line.

**Which "distance".** Horizons' `delta` is the light-time range: from where Voyager was when the signal left, to Dwingeloo now. The overlay shows the instantaneous distance, which is longer by how far Voyager moved during that day. That offset (1,461,640 km) is set so the instantaneous distance is exactly one light-day at **18 Nov 10:16:06 UTC**, the agreed moment. It matches Voyager's speed in the same table (16.9177 km/s) times one day to within about 50 km (≈2 s). The countdown target is solved from the same model, so the countdown, the clock and the km figure reach zero together.

The other definition, "the signal arriving now has travelled one day", is exactly where Horizons' `delta` reaches one light-day: **19 Nov ≈ 02:16:02 UTC**. Voyager is below the horizon then, and the table has a night gap there, so that time is good to roughly ±20 s.

| param | meaning | default |
|---|---|---|
| `at` | rehearsal: start the clock at this UTC time, e.g. `at=2026-11-18T10:15:00Z` (runs on the computer clock from there) | off |
| `dec`, `kmdec` | decimals of light-hours / km | 6, 1 |
| `theme=light`, `bg`, `local=0` | as before | |
| `ws` | relay address when the overlay is opened as a file | `localhost:8090` |

**Cross-check on the day:** the spectrum page's status line shows the tracker's own distance from one light-day and its range rate, live from the stream. These need the rebuilt `vrt_spectrum`. The tracker may use either definition; they always differ by about 1.46 million km, so it is obvious which one it uses.

## Test files

- `synthetic_sonification.mp3`: the page's own audio code, rendered offline from a synthetic run (details in the chat message).
- `vgr1_recording_sonified_tone500.mp3`: first 5 minutes of your 14 May 2025 recording (`--tau 300 --bin-size 1 --wola`), starting at 500 Hz (made with the old `tone=500`; the same start is now `lo=8420257902`).
- `live_test.png`: screenshot of the page during the live test.
