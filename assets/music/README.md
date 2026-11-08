# Background-music library

Drop audio files in this folder and they show up in the new-job form under "Background music".

- Formats: mp3, m4a, wav, aac, ogg, flac.
- Use only music you have a licence for, such as the YouTube Audio Library, a licence-checked
  free library, or your own recordings. Audio files are not committed to git (see `.gitignore`).

Optional `tracks.json` tags each file. Keys are file names:

```json
{
  "calm-piano.mp3": {
    "name": "Calm piano",
    "moods": ["calm", "serious"],
    "instrumental": true,
    "licence": "CC BY 4.0, source link here"
  }
}
```

- `moods` is one or more of: calm, upbeat, tense, emotional, serious, funny. Auto mode picks a
  track whose mood matches each clip, and uses any track if none matches.
- `instrumental: true` tracks are preferred. Vocals compete with the speech.
- A file without an entry counts as calm and not instrumental.

The music sits quietly under the voice and ducks while someone talks.
