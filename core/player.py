"""VLC-based audio playback for internet radio streams.

Knows nothing about tkinter, matplotlib, or the Radio Browser API - just
wraps a VLC instance/player and exposes play/stop/volume plus two
callbacks for state changes (on_error, on_playing). Those callbacks fire
on VLC's own internal thread, so a caller that touches UI state from them
must marshal back to the main thread itself (e.g. via root.after).
"""

try:
    import vlc
except (ImportError, OSError):
    vlc = None


class RadioPlayer:
    def __init__(self, on_error=None, on_playing=None):
        self._on_error = on_error
        self._on_playing = on_playing

        self.instance = None
        self.player = None

        if vlc is not None:
            try:
                # --quiet suppresses libVLC's verbose console logging (the
                # ts demux / discontinuity noise); real failures are still
                # caught via the event manager below.
                self.instance = vlc.Instance("--no-video", "--quiet")
                self.player = self.instance.media_player_new()
                events = self.player.event_manager()
                if self._on_error:
                    events.event_attach(vlc.EventType.MediaPlayerEncounteredError, self._on_error)
                if self._on_playing:
                    events.event_attach(vlc.EventType.MediaPlayerPlaying, self._on_playing)
            except Exception:
                self.instance = None
                self.player = None

    @property
    def is_available(self):
        return self.player is not None

    def play(self, url, volume=80):
        if not self.is_available:
            raise RuntimeError("VLC engine not loaded. Install VLC and restart.")
        media = self.instance.media_new(url)
        self.player.set_media(media)
        self.player.audio_set_volume(int(volume))
        self.player.play()

    def stop(self):
        if self.is_available:
            self.player.stop()

    def set_volume(self, volume):
        if self.is_available:
            self.player.audio_set_volume(int(volume))
